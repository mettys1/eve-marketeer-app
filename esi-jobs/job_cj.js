#!/usr/bin/env node
// Cloud Run Job: esi-cj-poller — C-J6MT (Goon Keepstar) market import pipeline, data side.
// Added 2026-10-11. Meant to run AFTER the main refresh (run_eval.py / refresh_all.sh),
// triggered by `python run_cj.py`, which then does the analysis in Python.
//
// What one run does (CJ_MODE=full, default):
//   1. Pull the full C-J structure order book (authenticated, needs a character with
//      market access to the Keepstar)                         -> cj_orders_raw
//   2. Make sure every type in that book has static info cached (name, group, category,
//      packaged_volume for the m3 shipping formula)            -> type_info
//   3. For C-J types in CJ_CATEGORY_IDS (default 7 = Module): Jita (The Forge) order books.
//      If the main eve-jita-poller scan is fresh (<= CJ_JITA_REUSE_HOURS), types it already
//      covered are reused from market_orders_raw and only the missing ones are fetched;
//      otherwise everything is fetched                          -> cj_jita_orders_raw
//   4. Delve region market history (14d avg volume) for the same types, at most once per
//      type per day — this is only the FALLBACK demand estimate (Delve = 1DQ + C-J + ...,
//      so it overstates C-J demand); the primary estimate comes from snapshot diffs of
//      cj_orders_raw, computed in Python                        -> cj_delve_history
//   5. One bookkeeping row                                      -> cj_runs
//
// CJ_MODE=orders_only does just step 1 (+5) — cheap, for an optional hourly schedule that
// makes the snapshot-diff demand estimate much more accurate (orders that sell out
// completely between two snapshots are otherwise only counted as "disappeared").
//
// Env: CJ_STRUCTURE_ID (required), CJ_CREDENTIALS_SECRET_NAME (default: esi-credentials),
//      CJ_CATEGORY_IDS (default "7"), CJ_JITA_REUSE_HOURS (default 2), CJ_MODE,
//      CJ_CONCURRENCY (default 8), CJ_HISTORY_DAYS (default 14)
'use strict';
const { BigQuery } = require('@google-cloud/bigquery');
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, retryable, sleep } = require('./lib');

const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const STRUCTURE_ID = Number(process.env.CJ_STRUCTURE_ID || 0);
const SECRET_NAME = process.env.CJ_CREDENTIALS_SECRET_NAME || process.env.CREDENTIALS_SECRET_NAME || 'esi-credentials';
const CATEGORY_IDS = (process.env.CJ_CATEGORY_IDS || '7').split(',').map((s) => Number(s.trim())).filter(Boolean);
const JITA_REUSE_HOURS = Number(process.env.CJ_JITA_REUSE_HOURS || 2);
const MODE = process.env.CJ_MODE || 'full';
const CONCURRENCY = Number(process.env.CJ_CONCURRENCY || 8);
const HISTORY_DAYS = Number(process.env.CJ_HISTORY_DAYS || 14);

const FORGE_REGION_ID = 10000002;
const DELVE_REGION_ID = 10000060;

const T = (name) => `${GCP_PROJECT_ID}.${BQ_DATASET}.${name}`;

// --- public ESI GET with retry (error-limit / rate-limit aware) ------------------------
async function esiGet(url, attempt = 1) {
  const res = await fetch(url, { headers: { 'User-Agent': 'eve-marketeer-app cj-poller' } });
  if (res.ok) return { data: await res.json(), pages: Number(res.headers.get('x-pages')) || 1 };
  if (res.status === 404) return { data: [], pages: 1 };
  const retryable = res.status === 420 || res.status === 429 || res.status >= 500;
  if (retryable && attempt < 6) {
    const retryAfter = Number(res.headers.get('retry-after')) || 0;
    const errReset = Number(res.headers.get('x-esi-error-limit-reset')) || 0;
    const delay = Math.max(retryAfter, res.status === 420 ? errReset : 0) * 1000 || 1000 * 2 ** attempt;
    await sleep(delay);
    return esiGet(url, attempt + 1);
  }
  throw new Error(`ESI ${res.status} for ${url}: ${(await res.text()).slice(0, 200)}`);
}

async function mapWithConcurrency(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

async function insertRows(bq, table, rows) {
  if (!rows.length) { console.log(`  nothing to write -> ${table}`); return; }
  for (let i = 0; i < rows.length; i += 5000) {
    const batch = rows.slice(i, i + 5000);
    await retryable(() => bq.dataset(BQ_DATASET).table(table).insert(batch), { label: `${table} insert @${i}` });
  }
  console.log(`  wrote ${rows.length} rows -> ${table}`);
}

async function query(bq, sql, params = {}) {
  const [rows] = await retryable(() => bq.query({ query: sql, params }), { label: 'query' });
  return rows;
}

// --- step 1 -------------------------------------------------------------------------------
async function fetchCjOrders() {
  const creds = await loadCredentials(SECRET_NAME);
  console.log(`Using character ${creds.character_name || creds.character_id} from secret "${SECRET_NAME}".`);
  const accessToken = await refreshAccessToken(creds.refresh_token);
  try {
    return await fetchAllPages(`${ESI_BASE}/markets/structures/${STRUCTURE_ID}/`, accessToken);
  } catch (err) {
    const msg = String(err.message);
    if (msg.includes('403')) {
      throw new Error(
        `${msg}\n403 = character "${creds.character_name}" can't read the C-J market. Either the token ` +
        'lacks esi-markets.structure_markets.v1, or the character has no market/docking access to ' +
        `structure ${STRUCTURE_ID}. Log in with a character that has access via ` +
        'esi-oauth-service /login?key=...&target=cj (writes esi-credentials-cj) and set ' +
        'CJ_CREDENTIALS_SECRET_NAME=esi-credentials-cj on this job (deploy_cj.sh).'
      );
    }
    if (msg.includes('404')) {
      throw new Error(`${msg}\n404 = CJ_STRUCTURE_ID ${STRUCTURE_ID} is wrong or the structure has no market.`);
    }
    throw err;
  }
}

// --- step 2 -------------------------------------------------------------------------------
async function ensureTypeInfo(bq, typeIds) {
  if (!typeIds.length) return new Map();
  const known = await query(bq, `SELECT type_id, category_id FROM \`${T('type_info')}\` WHERE type_id IN UNNEST(@ids)`, { ids: typeIds });
  const info = new Map(known.map((r) => [Number(r.type_id), Number(r.category_id)]));
  const missing = typeIds.filter((id) => !info.has(id));
  console.log(`type_info: ${info.size} cached, ${missing.length} to fetch.`);
  if (!missing.length) return info;

  const groupCategory = new Map();
  const now = new Date().toISOString();
  const rows = (await mapWithConcurrency(missing, CONCURRENCY, async (typeId) => {
    const { data: t } = await esiGet(`${ESI_BASE}/universe/types/${typeId}/?datasource=tranquility&language=en`);
    if (!t || !t.group_id) return null;
    if (!groupCategory.has(t.group_id)) {
      // store the promise so concurrent workers don't all fetch the same group
      groupCategory.set(t.group_id, esiGet(`${ESI_BASE}/universe/groups/${t.group_id}/?datasource=tranquility`).then((r) => r.data.category_id));
    }
    const categoryId = await groupCategory.get(t.group_id);
    return {
      type_id: typeId, name: t.name, group_id: t.group_id, category_id: categoryId,
      volume: t.volume ?? null, packaged_volume: t.packaged_volume ?? t.volume ?? null,
      market_group_id: t.market_group_id ?? null, published: !!t.published, fetched_at: now,
    };
  })).filter(Boolean);
  await insertRows(bq, 'type_info', rows);
  for (const r of rows) info.set(r.type_id, r.category_id);
  return info;
}

// --- step 3 -------------------------------------------------------------------------------
async function fetchJitaBooks(bq, typeIds, runAt) {
  if (!typeIds.length) return { reusedScanAt: null, fetched: 0 };
  let reusedScanAt = null;
  let covered = new Set();
  const [latest] = await query(bq, `
    SELECT MAX(scanned_at) AS s FROM \`${T('market_orders_raw')}\`
    WHERE scan_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)`);
  if (latest && latest.s) {
    const s = new Date(latest.s.value || latest.s);
    const ageH = (Date.now() - s.getTime()) / 3.6e6;
    if (ageH <= JITA_REUSE_HOURS) {
      reusedScanAt = s.toISOString();
      const rows = await query(bq, `
        SELECT DISTINCT type_id FROM \`${T('market_orders_raw')}\`
        WHERE scan_date = DATE(@s) AND scanned_at = TIMESTAMP(@s) AND type_id IN UNNEST(@ids)`,
        { s: reusedScanAt, ids: typeIds });
      covered = new Set(rows.map((r) => Number(r.type_id)));
      console.log(`Main Jita scan ${reusedScanAt} is ${ageH.toFixed(1)}h old -> reusing ${covered.size} types.`);
    } else {
      console.log(`Main Jita scan is ${ageH.toFixed(1)}h old (> ${JITA_REUSE_HOURS}h) -> fetching all Jita books myself.`);
    }
  }
  const toFetch = typeIds.filter((id) => !covered.has(id));
  console.log(`Fetching Jita order books for ${toFetch.length} types...`);
  const scanDate = runAt.slice(0, 10);
  const rows = [];
  await mapWithConcurrency(toFetch, CONCURRENCY, async (typeId) => {
    let page = 1;
    let pages = 1;
    do {
      const r = await esiGet(`${ESI_BASE}/markets/${FORGE_REGION_ID}/orders/?datasource=tranquility&order_type=all&type_id=${typeId}&page=${page}`);
      pages = r.pages;
      for (const o of r.data) {
        rows.push({
          scanned_at: runAt, scan_date: scanDate, type_id: typeId, order_id: o.order_id,
          is_buy_order: o.is_buy_order, price: o.price, volume_remain: o.volume_remain,
          location_id: o.location_id, system_id: o.system_id, min_volume: o.min_volume, range: o.range,
        });
      }
      page++;
    } while (page <= pages);
  });
  await insertRows(bq, 'cj_jita_orders_raw', rows);
  return { reusedScanAt, fetched: toFetch.length };
}

// --- step 4 -------------------------------------------------------------------------------
async function fetchDelveHistory(bq, typeIds) {
  if (!typeIds.length) return 0;
  const done = await query(bq, `
    SELECT DISTINCT type_id FROM \`${T('cj_delve_history')}\`
    WHERE fetched_date = CURRENT_DATE() AND type_id IN UNNEST(@ids)`, { ids: typeIds });
  const doneSet = new Set(done.map((r) => Number(r.type_id)));
  const toFetch = typeIds.filter((id) => !doneSet.has(id));
  console.log(`Delve history: ${doneSet.size} already fetched today, ${toFetch.length} to fetch.`);
  const today = new Date().toISOString().slice(0, 10);
  const cutoff = new Date(Date.now() - HISTORY_DAYS * 86400e3).toISOString().slice(0, 10);
  const rows = (await mapWithConcurrency(toFetch, Math.min(CONCURRENCY, 6), async (typeId) => {
    const { data } = await esiGet(`${ESI_BASE}/markets/${DELVE_REGION_ID}/history/?datasource=tranquility&type_id=${typeId}`);
    const recent = (data || []).filter((h) => h.date >= cutoff);
    const vol = recent.reduce((s, h) => s + h.volume, 0);
    const isk = recent.reduce((s, h) => s + h.volume * h.average, 0);
    return {
      fetched_date: today, type_id: typeId,
      avg_daily_volume: vol / HISTORY_DAYS,
      avg_price: vol > 0 ? isk / vol : null,
      days_with_trades: recent.length, window_days: HISTORY_DAYS,
    };
  })).filter(Boolean);
  await insertRows(bq, 'cj_delve_history', rows);
  return toFetch.length;
}

async function main() {
  if (!STRUCTURE_ID) throw new Error('CJ_STRUCTURE_ID env var is missing — set it in deploy_cj.sh.');
  const bq = new BigQuery({ projectId: GCP_PROJECT_ID });
  const runAt = new Date().toISOString();
  const scanDate = runAt.slice(0, 10);

  console.log(`[1] C-J structure ${STRUCTURE_ID} market (mode=${MODE})...`);
  const orders = await fetchCjOrders();
  console.log(`  got ${orders.length} orders.`);
  await insertRows(bq, 'cj_orders_raw', orders.map((o) => ({
    scanned_at: runAt, scan_date: scanDate, structure_id: STRUCTURE_ID, type_id: o.type_id,
    order_id: o.order_id, is_buy_order: o.is_buy_order, price: o.price, volume_remain: o.volume_remain,
    volume_total: o.volume_total, min_volume: o.min_volume, range: o.range, duration: o.duration, issued: o.issued,
  })));

  const run = {
    run_at: runAt, structure_id: STRUCTURE_ID, mode: MODE, cj_orders: orders.length,
    cj_types: null, selected_types: null, jita_reused_scan_at: null, jita_fetched_types: null,
    delve_fetched_types: null, category_ids: CATEGORY_IDS.join(','),
  };

  if (MODE === 'full') {
    const allTypes = [...new Set(orders.map((o) => o.type_id))];
    console.log(`[2] type_info for ${allTypes.length} types...`);
    const cat = await ensureTypeInfo(bq, allTypes);
    const selected = allTypes.filter((id) => CATEGORY_IDS.includes(cat.get(id)));
    console.log(`  ${selected.length} types in categories [${CATEGORY_IDS}].`);

    console.log('[3] Jita order books...');
    const jita = await fetchJitaBooks(bq, selected, runAt);

    console.log('[4] Delve history (fallback demand)...');
    const delveFetched = await fetchDelveHistory(bq, selected);

    Object.assign(run, {
      cj_types: allTypes.length, selected_types: selected.length,
      jita_reused_scan_at: jita.reusedScanAt, jita_fetched_types: jita.fetched, delve_fetched_types: delveFetched,
    });
  }

  await insertRows(bq, 'cj_runs', [run]);
  console.log('done.');
}

main().catch((err) => { console.error(err); process.exit(1); });
