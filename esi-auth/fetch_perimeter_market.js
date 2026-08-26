#!/usr/bin/env node
//
// Full order book for the Perimeter citadel ("0.0% Neutral States Market HQ",
// structure_id 1044752365771 — where Matej's own buy orders actually sit) — a
// player-owned structure, so unlike Jita 4-4 (an NPC station, public via
// evetycoon/fuzzwork/unauthenticated ESI) its market is only visible through this
// authenticated call. Requires esi-markets.structure_markets.v1 on top of the orders
// scope — re-run get_refresh_token.js after adding it if your saved token predates it.
//
//   EVE_SSO_CLIENT_ID=<id> node esi-auth/fetch_perimeter_market.js
//
// Writes two files next to this script's parent (repo root):
//   perimeter_orders_raw.csv      — every order, one row each (like market_orders_raw)
//   perimeter_top_of_book.csv     — one row per type_id: best buy/sell + margin, same
//                                    fee formula and columns as recompute_top_of_book.csv,
//                                    so it can be fed straight to reports/generate_reports.py
//                                    or compared side by side with the Jita numbers.
//
// Also appends every raw order to BigQuery (`eve_jita_scanner.perimeter_orders_raw`),
// same project as the rest of the pipeline — needs `npm install` once in this folder
// (for @google-cloud/bigquery) and `gcloud auth application-default login` once on
// this machine (ADC — separate from the ESI OAuth login above, this is your own GCP
// login). Set SKIP_BIGQUERY=1 to skip that and only write the CSVs.

'use strict';
const fs = require('fs');
const path = require('path');

const SSO_BASE = 'https://login.eveonline.com';
const ESI_BASE = 'https://esi.evetech.net/latest';
const CREDS_PATH = path.join(__dirname, '.credentials.json');
const STRUCTURE_ID = 1044752365771; // Perimeter - 0.0% Neutral States Market HQ
const BROKER_FEE = 0.01382;
const SALES_TAX = 0.03375;
const GCP_PROJECT_ID = 'eve-jita-scanner-21359';
const BQ_DATASET = 'eve_jita_scanner';

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
  if (!first.ok) {
    const body = await first.text();
    if (first.status === 403) {
      throw new Error(
        `ESI request failed (403 Forbidden): ${body}\n` +
        'Usually means either (a) the token lacks esi-markets.structure_markets.v1 — re-run ' +
        'get_refresh_token.js after adding that scope in the app settings, or (b) this ' +
        "character has never docked at the structure — ESI only allows market reads for " +
        'structures your character can actually access in-game.'
      );
    }
    throw new Error(`ESI request failed (${first.status}): ${body}`);
  }
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

function csvVal(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function round(n, dp) {
  const f = 10 ** dp;
  return Math.round(n * f) / f;
}

async function main() {
  if (!fs.existsSync(CREDS_PATH)) {
    console.error(`No ${CREDS_PATH} found — run "node esi-auth/get_refresh_token.js" first.`);
    process.exit(1);
  }
  const clientId = process.env.EVE_SSO_CLIENT_ID;
  if (!clientId) {
    console.error('Set EVE_SSO_CLIENT_ID before running this (same Client ID as get_refresh_token.js).');
    process.exit(1);
  }
  const creds = JSON.parse(fs.readFileSync(CREDS_PATH, 'utf8'));
  const accessToken = await refreshAccessToken(clientId, creds.refresh_token);

  console.log(`Fetching Perimeter structure market (structure_id ${STRUCTURE_ID})...`);
  const orders = await fetchAllPages(`${ESI_BASE}/markets/structures/${STRUCTURE_ID}/`, accessToken);
  console.log(`Got ${orders.length} orders.`);

  const typeNames = await resolveTypeNames(orders.map((o) => o.type_id));

  // --- raw dump ---
  const repoRoot = path.join(__dirname, '..');
  const rawHeader = [
    'order_id', 'type_id', 'item_name', 'is_buy_order', 'price', 'volume_remain',
    'volume_total', 'min_volume', 'range', 'duration', 'issued',
  ];
  const rawLines = [rawHeader.join(',')];
  for (const o of orders) {
    rawLines.push([
      o.order_id, o.type_id, csvVal(typeNames[o.type_id] || ''), o.is_buy_order, o.price,
      o.volume_remain, o.volume_total, o.min_volume, o.range, o.duration, o.issued,
    ].join(','));
  }
  const rawPath = path.join(repoRoot, 'perimeter_orders_raw.csv');
  fs.writeFileSync(rawPath, rawLines.join('\n') + '\n');
  console.log(`Wrote ${rawPath}`);

  // --- top-of-book aggregate, same shape as recompute_top_of_book.csv ---
  const byType = new Map();
  for (const o of orders) {
    if (!byType.has(o.type_id)) byType.set(o.type_id, { buys: [], sells: [] });
    (o.is_buy_order ? byType.get(o.type_id).buys : byType.get(o.type_id).sells).push(o.price);
  }
  const aggHeader = [
    'type_id', 'item_name', 'buy_price', 'sell_price', 'buy_orders', 'sell_orders',
    'profit_per_unit', 'margin_pct',
  ];
  const aggRows = [];
  for (const [typeId, { buys, sells }] of byType) {
    if (buys.length === 0 || sells.length === 0) continue; // need both sides to price a flip
    const buyPrice = Math.max(...buys);
    const sellPrice = Math.min(...sells);
    const profitPerUnit = sellPrice * (1 - BROKER_FEE - SALES_TAX) - buyPrice * (1 + BROKER_FEE);
    const marginPct = (profitPerUnit / (buyPrice * (1 + BROKER_FEE))) * 100;
    aggRows.push({
      type_id: typeId, item_name: typeNames[typeId] || '', buy_price: round(buyPrice, 2),
      sell_price: round(sellPrice, 2), buy_orders: buys.length, sell_orders: sells.length,
      profit_per_unit: round(profitPerUnit, 2), margin_pct: round(marginPct, 3),
    });
  }
  aggRows.sort((a, b) => b.margin_pct - a.margin_pct);
  const aggLines = [aggHeader.join(',')];
  for (const r of aggRows) aggLines.push(aggHeader.map((h) => csvVal(r[h])).join(','));
  const aggPath = path.join(repoRoot, 'perimeter_top_of_book.csv');
  fs.writeFileSync(aggPath, aggLines.join('\n') + '\n');
  console.log(`Wrote ${aggPath} (${aggRows.length} type_ids with both a buy and a sell order)`);
  console.log('Upload either/both CSVs into the conversation with Claude.');

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
      type_id: o.type_id,
      item_name: typeNames[o.type_id] || null,
      order_id: o.order_id,
      is_buy_order: o.is_buy_order,
      price: o.price,
      volume_remain: o.volume_remain,
      volume_total: o.volume_total,
      min_volume: o.min_volume,
      range: o.range,
      duration: o.duration,
      issued: o.issued,
    }));
    // BigQuery's streaming insert caps out around 10k rows/request — batch just in case
    // the Perimeter book ever gets that large.
    for (let i = 0; i < rows.length; i += 5000) {
      await bq.dataset(BQ_DATASET).table('perimeter_orders_raw').insert(rows.slice(i, i + 5000));
    }
    console.log(`Wrote ${rows.length} rows to BigQuery ${GCP_PROJECT_ID}.${BQ_DATASET}.perimeter_orders_raw`);
  } catch (err) {
    console.error('BigQuery write failed (CSVs above were still written fine) —', err.message);
    console.error('If this is your first run: "npm install" in esi-auth/, and');
    console.error('"gcloud auth application-default login" once on this machine.');
  }
}

main().catch((err) => { console.error(err.message); process.exit(1); });
