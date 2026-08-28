#!/usr/bin/env node
// Cloud Run Job: pulls Matej's own live open orders from ESI and writes them to
// BigQuery `eve_jita_scanner.my_orders`. Credentials come from Secret Manager (see
// lib.js) — set up once via esi-oauth-service, not run locally. Trigger manually:
//   gcloud run jobs execute esi-my-orders-poller --region=europe-west1
//
// Fixed 2026-08-28 — two bugs found the same day, both from the same root cause
// (this job only ever INSERTs, never marks anything closed):
//
// 1. "Phantom order" bug: ESI's /characters/{id}/orders/ only returns CURRENTLY
//    open orders. Once an order fills or gets cancelled, ESI just stops returning
//    it — but this table is append-only, so `ROW_NUMBER() PARTITION BY order_id
//    ORDER BY scanned_at DESC` happily returns whatever the last-ever-seen row was,
//    forever, with no way to tell "still open" from "closed 3 days ago". Confirmed
//    this session on real orders Matej no longer has (Vexor, Platinum, Small Skill
//    Injector) that still showed up as "open" in every downstream query.
//    Fix: track closures explicitly. Before inserting this run's fetch, diff it
//    against the previously-known-open order_ids. Anything that dropped out gets
//    a synthetic closing row (is_open=false) written at today's scanned_at, so a
//    "latest row per order_id, WHERE is_open" query correctly excludes it.
//
// 2. is_buy_order NULL-for-false bug: ESI omits the is_buy_order field entirely on
//    sell orders (only present — and true — for buy orders), so `o.is_buy_order`
//    was landing in BigQuery as NULL instead of FALSE for every sell order. Broke
//    `WHERE is_buy_order` / `WHERE NOT is_buy_order` filters and wallet_capital's
//    listed_sell_value (NULL because the filter never matched). Fix: coerce
//    explicitly to a real boolean below.
'use strict';
const { BigQuery } = require('@google-cloud/bigquery');
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames } = require('./lib');

const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const KNOWN_LOCATIONS = {
  60003760: 'Jita IV - Moon 4 - Caldari Navy Assembly Plant',
  1044752365771: 'Perimeter - 0.0% Neutral States Market HQ',
};

// Latest row per order_id that we last believed was open. Legacy rows predate the
// is_open column and come back NULL — treat NULL the same as TRUE (we have no way
// to know retroactively when they closed, so "assume still open until proven
// otherwise by this diff" is the safest default, same as the old behavior).
async function fetchPreviouslyOpenOrderIds(bq) {
  const query = `
    SELECT order_id, type_id, item_name, is_buy_order, price, volume_remain,
           volume_total, location_id, location_name, region_id, \`range\`,
           min_volume, duration, issued
    FROM (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders\`
    )
    WHERE rn = 1 AND (is_open IS NULL OR is_open = TRUE)
  `;
  const [rows] = await bq.query({ query });
  return rows;
}

async function main() {
  const creds = await loadCredentials();
  const accessToken = await refreshAccessToken(creds.refresh_token);

  console.log(`Fetching open orders for ${creds.character_name} (character_id ${creds.character_id})...`);
  const orders = await fetchAllPages(`${ESI_BASE}/characters/${creds.character_id}/orders/`, accessToken);
  console.log(`Got ${orders.length} open orders.`);

  const typeNames = await resolveTypeNames(orders.map((o) => o.type_id));

  const bq = new BigQuery({ projectId: GCP_PROJECT_ID });
  const scannedAt = new Date();
  const scanDate = scannedAt.toISOString().slice(0, 10);

  const currentOrderIds = new Set(orders.map((o) => o.order_id));

  // Diff against what we last believed was open, BEFORE inserting this run's rows —
  // otherwise the "previously open" query would just see today's fresh rows too.
  const previouslyOpen = await fetchPreviouslyOpenOrderIds(bq);
  const justClosed = previouslyOpen.filter((o) => !currentOrderIds.has(o.order_id));
  if (justClosed.length > 0) {
    console.log(`${justClosed.length} order(s) no longer returned by ESI — marking closed: ${justClosed.map((o) => `${o.item_name} (${o.order_id})`).join(', ')}`);
  }

  const openRows = orders.map((o) => ({
    scanned_at: scannedAt.toISOString(),
    scan_date: scanDate,
    order_id: o.order_id,
    type_id: o.type_id,
    item_name: typeNames[o.type_id] || null,
    is_buy_order: o.is_buy_order === true, // fix #2 — was `o.is_buy_order` (undefined -> NULL for sells)
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
    is_open: true, // fix #1
  }));

  // Closing rows carry forward the last known price/volume/etc (we don't know the
  // final fill state — ESI doesn't tell us via this endpoint) but flip is_open to
  // false, at *this* scan's timestamp, so dedup-by-latest-scanned_at picks them up
  // as the current truth for that order_id from now on.
  const closingRows = justClosed.map((o) => ({
    scanned_at: scannedAt.toISOString(),
    scan_date: scanDate,
    order_id: o.order_id,
    type_id: o.type_id,
    item_name: o.item_name,
    is_buy_order: o.is_buy_order === true,
    price: o.price,
    volume_remain: o.volume_remain,
    volume_total: o.volume_total,
    location_id: o.location_id,
    location_name: o.location_name,
    region_id: o.region_id,
    range: o.range,
    min_volume: o.min_volume,
    duration: o.duration,
    issued: o.issued,
    is_open: false,
  }));

  const rows = [...openRows, ...closingRows];
  if (rows.length > 0) {
    await bq.dataset(BQ_DATASET).table('my_orders').insert(rows);
  }
  console.log(`Wrote ${openRows.length} open + ${closingRows.length} closed row(s) to BigQuery ${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders`);
}

main().catch((err) => { console.error(err); process.exit(1); });
