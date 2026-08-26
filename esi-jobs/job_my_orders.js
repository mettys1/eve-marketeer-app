#!/usr/bin/env node
// Cloud Run Job: pulls Matej's own live open orders from ESI and writes them to
// BigQuery `eve_jita_scanner.my_orders`. Credentials come from Secret Manager (see
// lib.js) — set up once via esi-oauth-service, not run locally. Trigger manually:
//   gcloud run jobs execute esi-my-orders-poller --region=europe-west1
'use strict';
const { BigQuery } = require('@google-cloud/bigquery');
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames } = require('./lib');

const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const KNOWN_LOCATIONS = {
  60003760: 'Jita IV - Moon 4 - Caldari Navy Assembly Plant',
  1044752365771: 'Perimeter - 0.0% Neutral States Market HQ',
};

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
  if (rows.length > 0) {
    await bq.dataset(BQ_DATASET).table('my_orders').insert(rows);
  }
  console.log(`Wrote ${rows.length} rows to BigQuery ${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders`);
}

main().catch((err) => { console.error(err); process.exit(1); });
