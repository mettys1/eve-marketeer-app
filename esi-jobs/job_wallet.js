#!/usr/bin/env node
// Cloud Run Job: pulls Matej's wallet transactions (actual fills — buy/sell, price,
// quantity, timestamp) and wallet journal (fees, escrow, SCC surcharge, everything)
// from ESI and writes them to BigQuery. This is the real ledger — unlike `my_orders`
// (a snapshot of currently-open orders), these are permanent historical records, so
// staleness/fill-velocity analysis should lean on this instead of `issued` timestamps
// once repricing is a regular thing (repricing resets `issued`, but never touches the
// transaction history).
//
// Needs the esi-wallet.read_character_wallet.v1 scope — added 2026-08-26. If this job
// fails with a 403 mentioning scope, re-visit the /login link (see
// esi-oauth-service/README.md) to re-authorize with the new scope.
//
// Trigger manually: gcloud run jobs execute esi-wallet-poller --region=europe-west1
'use strict';
const { BigQuery } = require('@google-cloud/bigquery');
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames } = require('./lib');

const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';

// /wallet/transactions/ paginates via a `from_id` cursor (oldest transaction_id seen
// so far), NOT the page-number scheme the other ESI endpoints use — ESI returns up to
// 2500 rows per call, newest first, and stops once a call returns 0 rows.
async function fetchAllTransactions(characterId, accessToken) {
  const base = `${ESI_BASE}/characters/${characterId}/wallet/transactions/`;
  let all = [];
  let fromId = null;
  for (;;) {
    const url = fromId ? `${base}?from_id=${fromId}` : base;
    const res = await fetch(url, { headers: { Authorization: `Bearer ${accessToken}` } });
    if (!res.ok) throw new Error(`ESI wallet/transactions failed (${res.status}): ${await res.text()}`);
    const batch = await res.json();
    if (batch.length === 0) break;
    all = all.concat(batch);
    fromId = Math.min(...batch.map((t) => t.transaction_id)) - 1;
    if (batch.length < 2500) break; // last page
  }
  return all;
}

async function main() {
  const creds = await loadCredentials();
  const accessToken = await refreshAccessToken(creds.refresh_token);

  console.log(`Fetching wallet transactions for ${creds.character_name} (character_id ${creds.character_id})...`);
  const transactions = await fetchAllTransactions(creds.character_id, accessToken);
  console.log(`Got ${transactions.length} transactions.`);

  console.log(`Fetching wallet journal...`);
  const journal = await fetchAllPages(`${ESI_BASE}/characters/${creds.character_id}/wallet/journal/`, accessToken);
  console.log(`Got ${journal.length} journal entries.`);

  const typeNames = await resolveTypeNames(transactions.map((t) => t.type_id));

  const bq = new BigQuery({ projectId: GCP_PROJECT_ID });
  const pulledAt = new Date().toISOString();

  const txRows = transactions.map((t) => ({
    pulled_at: pulledAt,
    transaction_id: t.transaction_id,
    date: t.date,
    type_id: t.type_id,
    item_name: typeNames[t.type_id] || null,
    quantity: t.quantity,
    unit_price: t.unit_price,
    is_buy: t.is_buy,
    is_personal: t.is_personal,
    location_id: t.location_id,
    client_id: t.client_id,
    journal_ref_id: t.journal_ref_id,
  }));

  const journalRows = journal.map((j) => ({
    pulled_at: pulledAt,
    journal_id: j.id,
    date: j.date,
    ref_type: j.ref_type,
    amount: j.amount != null ? j.amount : null,
    balance: j.balance != null ? j.balance : null,
    description: j.description || null,
    reason: j.reason || null,
    context_id: j.context_id != null ? j.context_id : null,
    context_id_type: j.context_id_type || null,
    first_party_id: j.first_party_id != null ? j.first_party_id : null,
    second_party_id: j.second_party_id != null ? j.second_party_id : null,
    tax: j.tax != null ? j.tax : null,
    tax_receiver_id: j.tax_receiver_id != null ? j.tax_receiver_id : null,
  }));

  // Both tables are append-only historical ledgers keyed by transaction_id/journal_id —
  // dedupe with a MERGE instead of a plain insert, since re-running this job will
  // re-fetch overlapping recent history every time (ESI has no "since last pull" filter
  // for these endpoints).
  // Explicit types for the ARRAY<STRUCT<...>> param — without this, the client infers
  // each field's type from the JS values it happens to see (e.g. an integer-looking
  // unit_price on one row vs. a decimal like 30.73 on another), and guesses wrong,
  // which fails the whole query. Spelling out the STRUCT shape here matches the actual
  // BigQuery column types in bigquery/schema.sql, so this can't happen.
  if (txRows.length > 0) {
    await bq.query({
      query: `
        MERGE \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\` T
        USING UNNEST(@rows) S
        ON T.transaction_id = S.transaction_id
        WHEN NOT MATCHED THEN INSERT ROW`,
      params: { rows: txRows },
      types: {
        rows: [{
          pulled_at: 'TIMESTAMP',
          transaction_id: 'INT64',
          date: 'TIMESTAMP',
          type_id: 'INT64',
          item_name: 'STRING',
          quantity: 'INT64',
          unit_price: 'FLOAT64',
          is_buy: 'BOOL',
          is_personal: 'BOOL',
          location_id: 'INT64',
          client_id: 'INT64',
          journal_ref_id: 'INT64',
        }],
      },
    });
  }
  if (journalRows.length > 0) {
    await bq.query({
      query: `
        MERGE \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\` T
        USING UNNEST(@rows) S
        ON T.journal_id = S.journal_id
        WHEN NOT MATCHED THEN INSERT ROW`,
      params: { rows: journalRows },
      types: {
        rows: [{
          pulled_at: 'TIMESTAMP',
          journal_id: 'INT64',
          date: 'TIMESTAMP',
          ref_type: 'STRING',
          amount: 'FLOAT64',
          balance: 'FLOAT64',
          description: 'STRING',
          reason: 'STRING',
          context_id: 'INT64',
          context_id_type: 'STRING',
          first_party_id: 'INT64',
          second_party_id: 'INT64',
          tax: 'FLOAT64',
          tax_receiver_id: 'INT64',
        }],
      },
    });
  }
  console.log(`Merged ${txRows.length} transactions, ${journalRows.length} journal entries into BigQuery.`);
}

main().catch((err) => { console.error(err); process.exit(1); });
