#!/usr/bin/env node
// Cloud Run Job: pulls the full Perimeter citadel order book from ESI and writes it
// to BigQuery `eve_jita_scanner.perimeter_orders_raw`. Credentials come from Secret
// Manager (see lib.js). Trigger manually:
//   gcloud run jobs execute esi-perimeter-poller --region=europe-west1
'use strict';
const { BigQuery } = require('@google-cloud/bigquery');
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames, retryable } = require('./lib');

const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const STRUCTURE_ID = 1044752365771; // Perimeter - 0.0% Neutral States Market HQ

async function main() {
  const creds = await loadCredentials();
  const accessToken = await refreshAccessToken(creds.refresh_token);

  console.log(`Fetching Perimeter structure market (structure_id ${STRUCTURE_ID})...`);
  let orders;
  try {
    orders = await fetchAllPages(`${ESI_BASE}/markets/structures/${STRUCTURE_ID}/`, accessToken);
  } catch (err) {
    if (String(err.message).includes('403')) {
      throw new Error(
        `${err.message}\nUsually means either the token lacks esi-markets.structure_markets.v1, ` +
        'or this character has never docked at the structure (ESI only allows market reads for ' +
        'structures your character can access in-game). Re-authorize via esi-oauth-service if needed.'
      );
    }
    throw err;
  }
  console.log(`Got ${orders.length} orders.`);

  const typeNames = await resolveTypeNames(orders.map((o) => o.type_id));

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
  for (let i = 0; i < rows.length; i += 5000) {
    const batch = rows.slice(i, i + 5000);
    await retryable(
      () => bq.dataset(BQ_DATASET).table('perimeter_orders_raw').insert(batch),
      { label: `perimeter_orders_raw insert (batch starting row ${i})` }
    );
  }
  console.log(`Wrote ${rows.length} rows to BigQuery ${GCP_PROJECT_ID}.${BQ_DATASET}.perimeter_orders_raw`);
}

main().catch((err) => { console.error(err); process.exit(1); });
