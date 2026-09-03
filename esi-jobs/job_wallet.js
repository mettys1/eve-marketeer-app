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
const { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames, retryable } = require('./lib');

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
  // For a TIMESTAMP field nested inside an ARRAY<STRUCT<...>> query parameter, the
  // client's value-encoder for struct fields doesn't apply the same auto-conversion a
  // top-level TIMESTAMP param gets — a raw ISO string silently comes through as NULL
  // (hence "Required field pulled_at cannot be null" even though every row set it).
  // Wrapping with bigquery.timestamp(...) forces the correct encoding either way.
  const ts = (v) => (v == null ? null : bq.timestamp(v));

  const txRows = transactions.map((t) => ({
    pulled_at: ts(pulledAt),
    transaction_id: t.transaction_id,
    date: ts(t.date),
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
    pulled_at: ts(pulledAt),
    journal_id: j.id,
    date: ts(j.date),
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
  // NOTE: `WHEN NOT MATCHED THEN INSERT ROW` looks like it should star-expand S's
  // columns, but with a query-parameter-driven `USING UNNEST(@rows) S` it doesn't
  // reliably do that — BigQuery threw "Inserted row has wrong column count; Has 1,
  // expected 12", meaning it saw S as a single STRUCT value, not 12 named columns.
  // Fix: spell out the INSERT column list and VALUES explicitly instead of ROW.
  //
  // Also batched (CHUNK rows per MERGE call) as a defensive measure against BigQuery's
  // per-request size limits now that this pulls the character's full trade history.
  const CHUNK = 2000;
  function chunks(arr, size) {
    const out = [];
    for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
    return out;
  }

  const TX_TYPES = {
    pulled_at: 'TIMESTAMP', transaction_id: 'INT64', date: 'TIMESTAMP',
    type_id: 'INT64', item_name: 'STRING', quantity: 'INT64', unit_price: 'FLOAT64',
    is_buy: 'BOOL', is_personal: 'BOOL', location_id: 'INT64', client_id: 'INT64',
    journal_ref_id: 'INT64',
  };
  const TX_COLS = Object.keys(TX_TYPES);
  const TX_MERGE_SQL = `
    MERGE \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\` T
    USING UNNEST(@rows) S
    ON T.transaction_id = S.transaction_id
    WHEN NOT MATCHED THEN
      INSERT (${TX_COLS.join(', ')})
      VALUES (${TX_COLS.map((c) => `S.${c}`).join(', ')})`;

  const JOURNAL_TYPES = {
    pulled_at: 'TIMESTAMP', journal_id: 'INT64', date: 'TIMESTAMP', ref_type: 'STRING',
    amount: 'FLOAT64', balance: 'FLOAT64', description: 'STRING', reason: 'STRING',
    context_id: 'INT64', context_id_type: 'STRING', first_party_id: 'INT64',
    second_party_id: 'INT64', tax: 'FLOAT64', tax_receiver_id: 'INT64',
  };
  const JOURNAL_COLS = Object.keys(JOURNAL_TYPES);
  const JOURNAL_MERGE_SQL = `
    MERGE \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\` T
    USING UNNEST(@rows) S
    ON T.journal_id = S.journal_id
    WHEN NOT MATCHED THEN
      INSERT (${JOURNAL_COLS.join(', ')})
      VALUES (${JOURNAL_COLS.map((c) => `S.${c}`).join(', ')})`;

  // Greppable prefix so a future failure's real message is easy to find in Cloud
  // Logging without guessing at gcloud's textPayload formatting/truncation.
  async function mergeChunked(label, sql, rows, types) {
    let done = 0;
    for (const batch of chunks(rows, CHUNK)) {
      try {
        await retryable(
          () => bq.query({ query: sql, params: { rows: batch }, types: { rows: [types] } }),
          { label: `${label} merge (batch after ${done} succeeded)` }
        );
        done += batch.length;
      } catch (err) {
        console.error(`WALLET_JOB_ERROR[${label}]: batch of ${batch.length} rows failed (after ${done} succeeded).`);
        console.error(`WALLET_JOB_ERROR[${label}] message: ${err && err.message}`);
        if (err && err.errors) console.error(`WALLET_JOB_ERROR[${label}] errors: ${JSON.stringify(err.errors)}`);
        throw err;
      }
    }
    console.log(`Merged ${done} ${label} rows into BigQuery.`);
  }

  if (txRows.length > 0) await mergeChunked('wallet_transactions', TX_MERGE_SQL, txRows, TX_TYPES);
  if (journalRows.length > 0) await mergeChunked('wallet_journal', JOURNAL_MERGE_SQL, journalRows, JOURNAL_TYPES);
  console.log(`Done. ${txRows.length} transactions, ${journalRows.length} journal entries processed.`);
}

main().catch((err) => { console.error(err); process.exit(1); });
