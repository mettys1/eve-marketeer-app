#!/usr/bin/env node
//
// Fetch YOUR character's live open market orders straight from ESI — replaces the
// manual in-game screenshot. Requires esi-auth/.credentials.json (run
// get_refresh_token.js once first).
//
//   node esi-auth/fetch_my_orders.js [out.csv]
//
// Writes a CSV (default: my_orders.csv, next to this script) with one row per open
// order: order_id, type_id, item_name, is_buy_order, price, volume_remain,
// volume_total, location_id, location_name, region_id, range, min_volume, duration,
// issued. Upload that CSV back into the conversation with Claude the same way you do
// recompute_top_of_book.csv — it'll cross-reference against fresh market prices and
// tell you what to reprice or cancel.
//
// Also appends every row to BigQuery (`eve_jita_scanner.my_orders`), same project as
// the rest of the pipeline — needs `npm install` once in this folder (for
// @google-cloud/bigquery) and `gcloud auth application-default login` once on this
// machine (ADC — separate from the ESI OAuth login above, this is your own GCP login).
// Set SKIP_BIGQUERY=1 to skip that and only write the CSV.

'use strict';
const fs = require('fs');
const path = require('path');

const SSO_BASE = 'https://login.eveonline.com';
const ESI_BASE = 'https://esi.evetech.net/latest';
const CREDS_PATH = path.join(__dirname, '.credentials.json');
const GCP_PROJECT_ID = 'eve-jita-scanner-21359';
const BQ_DATASET = 'eve_jita_scanner';

// A couple of well-known structure/station names worth hardcoding since ESI can't
// resolve player-owned citadels by ID without an extra authenticated call per
// structure (and this script only needs read access to orders, not structures).
const KNOWN_LOCATIONS = {
  60003760: 'Jita IV - Moon 4 - Caldari Navy Assembly Plant',
  1044752365771: 'Perimeter - 0.0% Neutral States Market HQ',
};

async function refreshAccessToken(clientId, refreshToken) {
  const headers = { 'Content-Type': 'application/x-www-form-urlencoded', Host: 'login.eveonline.com' };
  // Set EVE_SSO_CLIENT_SECRET if your app registration is Confidential (issued a
  // Client Secret) rather than Public/native — never hardcode the secret here.
  const usingBasicAuth = Boolean(process.env.EVE_SSO_CLIENT_SECRET);
  if (usingBasicAuth) {
    headers.Authorization = `Basic ${Buffer.from(`${clientId}:${process.env.EVE_SSO_CLIENT_SECRET}`).toString('base64')}`;
  }
  // EVE SSO rejects the request if client_id is present both in the Authorization
  // header AND the body — include it in exactly one place.
  const bodyParams = { grant_type: 'refresh_token', refresh_token: refreshToken };
  if (!usingBasicAuth) bodyParams.client_id = clientId;
  const res = await fetch(`${SSO_BASE}/v2/oauth/token`, {
    method: 'POST',
    headers,
    body: new URLSearchParams(bodyParams),
  });
  const json = await res.json();
  if (!res.ok) throw new Error(`Token refresh failed: ${JSON.stringify(json)}`);
  return json.access_token;
}

async function fetchAllPages(url, accessToken) {
  const first = await fetch(`${url}?page=1`, { headers: { Authorization: `Bearer ${accessToken}` } });
  if (!first.ok) throw new Error(`ESI request failed (${first.status}): ${await first.text()}`);
  const pages = Number(first.headers.get('x-pages') || '1');
  let all = await first.json();
  for (let p = 2; p <= pages; p++) {
    const res = await fetch(`${url}?page=${p}`, { headers: { Authorization: `Bearer ${accessToken}` } });
    if (!res.ok) throw new Error(`ESI request failed (${res.status}): ${await res.text()}`);
    all = all.concat(await res.json());
  }
  return all;
}

async function resolveTypeNames(typeIds) {
  const uniqueIds = [...new Set(typeIds)];
  const names = {};
  // ESI /universe/names/ takes max 1000 ids per call, unauthenticated.
  for (let i = 0; i < uniqueIds.length; i += 1000) {
    const batch = uniqueIds.slice(i, i + 1000);
    const res = await fetch(`${ESI_BASE}/universe/names/?datasource=tranquility`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(batch),
    });
    if (!res.ok) throw new Error(`Name resolution failed (${res.status}): ${await res.text()}`);
    for (const row of await res.json()) names[row.id] = row.name;
  }
  return names;
}

function toCsvValue(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function main() {
  if (!fs.existsSync(CREDS_PATH)) {
    console.error(`No ${CREDS_PATH} found — run "node esi-auth/get_refresh_token.js" first.`);
    process.exit(1);
  }
  const clientId = process.env.EVE_SSO_CLIENT_ID;
  if (!clientId) {
    console.error('Set EVE_SSO_CLIENT_ID (same Client ID used in get_refresh_token.js) before running this.');
    console.error('  e.g. EVE_SSO_CLIENT_ID=your_client_id node esi-auth/fetch_my_orders.js');
    process.exit(1);
  }
  const creds = JSON.parse(fs.readFileSync(CREDS_PATH, 'utf8'));
  const accessToken = await refreshAccessToken(clientId, creds.refresh_token);

  console.log(`Fetching open orders for ${creds.character_name} (character_id ${creds.character_id})...`);
  const orders = await fetchAllPages(`${ESI_BASE}/characters/${creds.character_id}/orders/`, accessToken);
  console.log(`Got ${orders.length} open orders.`);

  const typeNames = await resolveTypeNames(orders.map((o) => o.type_id));

  const outPath = path.resolve(process.argv[2] || path.join(__dirname, '..', 'my_orders.csv'));
  const header = [
    'order_id', 'type_id', 'item_name', 'is_buy_order', 'price', 'volume_remain',
    'volume_total', 'location_id', 'location_name', 'region_id', 'range', 'min_volume',
    'duration', 'issued',
  ];
  const lines = [header.join(',')];
  for (const o of orders) {
    lines.push([
      o.order_id, o.type_id, toCsvValue(typeNames[o.type_id] || ''), o.is_buy_order,
      o.price, o.volume_remain, o.volume_total, o.location_id,
      toCsvValue(KNOWN_LOCATIONS[o.location_id] || ''), o.region_id, o.range,
      o.min_volume, o.duration, o.issued,
    ].join(','));
  }
  fs.writeFileSync(outPath, lines.join('\n') + '\n');
  console.log(`Wrote ${outPath}`);
  console.log('Upload this CSV into the conversation with Claude for reprice/cancel evaluation.');

  if (process.env.SKIP_BIGQUERY) {
    console.log('SKIP_BIGQUERY set — not writing to BigQuery.');
    return;
  }
  try {
    const { BigQuery } = require('@google-cloud/bigquery');
    const bq = new BigQuery({ projectId: GCP_PROJECT_ID });
    const scannedAt = new Date();
    const scanDate = scannedAt.toISOString().slice(0, 10);
    const rows = orders.map((o) => ({
      scanned_at: scannedAt.toISOString(),
      scan_date: scanDate,
      order_id: o.order_id,
      type_id: o.type_id,
      item_name: typeNames[o.type_id] || null,
      is_buy_order: o.is_buy_order,
      price: o.price,
      volume_remain: o.volume_remain,
      volume_total: o.volume_total,
      location_id: o.location_id,
      location_name: KNOWN_LOCATIONS[o.location_id] || null,
      region_id: o.region_id,
      range: o.range,
      min_volume: o.min_volume,
      duration: o.duration,
      issued: o.issued,
    }));
    await bq.dataset(BQ_DATASET).table('my_orders').insert(rows);
    console.log(`Wrote ${rows.length} rows to BigQuery ${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders`);
  } catch (err) {
    console.error('BigQuery write failed (CSV above was still written fine) —', err.message);
    console.error('If this is your first run: "npm install" in esi-auth/, and');
    console.error('"gcloud auth application-default login" once on this machine.');
  }
}

main().catch((err) => { console.error(err); process.exit(1); });
