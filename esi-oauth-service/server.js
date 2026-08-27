#!/usr/bin/env node
//
// esi-oauth-service — a tiny always-on Cloud Run web service that does ONE job: be
// the OAuth callback target for EVE SSO, so the whole login flow can happen entirely
// in GCP with no local machine involved. Visit its URL, log in, and it stores the
// resulting refresh token in Secret Manager — from there, esi-jobs/ (Cloud Run Jobs)
// pick it up to pull your orders/market data on a schedule, same pattern as the
// existing poller/ Cloud Run Job.
//
// Why this exists: `esi-auth/`'s local scripts need a browser and a script running on
// the SAME machine (OAuth redirects to http://localhost:8765) — Matej wanted no local
// step at all, so this replaces that with a real public HTTPS endpoint.
//
// Env vars (set via `gcloud run deploy --set-env-vars` / `--set-secrets`, see
// deploy_esi.sh):
//   EVE_SSO_CLIENT_ID       — from developers.eveonline.com
//   EVE_SSO_CLIENT_SECRET   — same (Confidential app — see esi-auth/README.md history)
//   GCP_PROJECT_ID          — defaults to eve-jita-scanner-21359
//   CREDENTIALS_SECRET_NAME — Secret Manager secret name to write to (default esi-credentials)

'use strict';
const http = require('http');
const crypto = require('crypto');
const { URL } = require('url');
const { Firestore } = require('@google-cloud/firestore');
const { SecretManagerServiceClient } = require('@google-cloud/secret-manager');
const { BigQuery } = require('@google-cloud/bigquery');

const PORT = process.env.PORT || 8080;
const CLIENT_ID = process.env.EVE_SSO_CLIENT_ID;
const CLIENT_SECRET = process.env.EVE_SSO_CLIENT_SECRET;
const GCP_PROJECT_ID = process.env.GCP_PROJECT_ID || 'eve-jita-scanner-21359';
const CREDENTIALS_SECRET_NAME = process.env.CREDENTIALS_SECRET_NAME || 'esi-credentials';
const LOGIN_KEY = process.env.LOGIN_KEY || '';
// Reuses LOGIN_KEY as the guard for /report too, unless a separate REPORT_KEY is set —
// one secret to keep track of is simpler, and both endpoints are equally "only Matej
// should hit this" in sensitivity.
const REPORT_KEY = process.env.REPORT_KEY || LOGIN_KEY;
const BQ_DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
// esi-wallet.read_character_wallet.v1 added 2026-08-26 for wallet_transactions/journal
// (esi-jobs/job_wallet.js) — real fill/fee history for staleness analysis, since
// repricing resets an order's `issued` timestamp but never touches the ledger. Anyone
// already logged in from before this needs to re-visit /login to pick up the new scope
// (EVE SSO won't silently widen an existing grant) — also re-tick the matching
// checkbox on the app in developers.eveonline.com if it's not already enabled there.
const SCOPES = 'esi-markets.read_character_orders.v1 esi-markets.structure_markets.v1 esi-wallet.read_character_wallet.v1';
const SSO_BASE = 'https://login.eveonline.com';
const STATE_COLLECTION = 'oauth_pending';
const STATE_TTL_MS = 10 * 60 * 1000; // PKCE flow should complete within 10 minutes

const firestore = new Firestore({ projectId: GCP_PROJECT_ID });
const secretClient = new SecretManagerServiceClient();
const bigquery = new BigQuery({ projectId: GCP_PROJECT_ID });

// Named, read-only reports Claude can pull via GET /report?key=...&name=...
// Deliberately an ALLOWLIST of fixed queries, not arbitrary SQL passthrough — this
// endpoint is unauthenticated apart from the shared key, so it should only ever be
// able to run the exact same SELECTs that already live in bigquery/*.sql, nothing
// Claude (or anyone with the URL) could use to write/delete data.
const REPORTS = {
  my_orders_analysis: `
    WITH latest_orders AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
        FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders\`
      ) WHERE rn = 1
    ),
    perimeter_book AS (
      SELECT type_id,
        MAX(IF(is_buy_order, price, NULL)) AS best_buy,
        MIN(IF(NOT is_buy_order, price, NULL)) AS best_sell,
        COUNTIF(is_buy_order) AS buy_orders,
        COUNTIF(NOT is_buy_order) AS sell_orders
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.perimeter_orders_raw\`
      WHERE scan_date = (SELECT MAX(scan_date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.perimeter_orders_raw\`)
      GROUP BY type_id
    ),
    jita_book AS (
      SELECT type_id,
        MAX(IF(is_buy_order, price, NULL)) AS best_buy,
        MIN(IF(NOT is_buy_order, price, NULL)) AS best_sell,
        COUNTIF(is_buy_order) AS buy_orders,
        COUNTIF(NOT is_buy_order) AS sell_orders
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_orders_raw\`
      WHERE scan_date = (SELECT MAX(scan_date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_orders_raw\`)
        AND location_id = 60003760
      GROUP BY type_id
    )
    SELECT o.location_name, o.item_name, o.type_id, o.is_buy_order, o.price AS my_price,
      o.volume_remain, o.volume_total, o.issued,
      p.best_buy AS perim_best_buy, p.best_sell AS perim_best_sell,
      p.buy_orders AS perim_buy_ct, p.sell_orders AS perim_sell_ct,
      j.best_buy AS jita_best_buy, j.best_sell AS jita_best_sell,
      j.buy_orders AS jita_buy_ct, j.sell_orders AS jita_sell_ct
    FROM latest_orders o
    LEFT JOIN perimeter_book p USING (type_id)
    LEFT JOIN jita_book j USING (type_id)
    ORDER BY o.location_name, o.item_name`,

  stale_orders: `
    WITH latest_orders AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
        FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders\`
      ) WHERE rn = 1
    )
    SELECT location_name, item_name, type_id, is_buy_order, price, volume_remain, volume_total,
      ROUND(SAFE_DIVIDE(volume_total - volume_remain, volume_total) * 100, 1) AS fill_pct,
      DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) AS order_age_days,
      ROUND(price * volume_remain, 0) AS isk_locked,
      CASE
        WHEN DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) >= 3
         AND SAFE_DIVIDE(volume_total - volume_remain, volume_total) < 0.10
        THEN 'STALE'
        WHEN DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) >= 1
         AND SAFE_DIVIDE(volume_total - volume_remain, volume_total) < 0.10
        THEN 'watch'
        ELSE 'moving'
      END AS status
    FROM latest_orders
    ORDER BY isk_locked DESC`,

  jita_top_of_book: `
    SELECT * FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_snapshots\`
    WHERE scan_date = (SELECT MAX(scan_date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_snapshots\`)
    ORDER BY region_margin_pct DESC
    LIMIT 500`,

  // Added 2026-08-26 — jita_top_of_book's raw 500 rows blow past what WebFetch's
  // summarizer can hold without truncating, and most of those rows are unusable anyway
  // (near-zero-volume niche items whose "margin" is a reference-price artifact, or
  // peanuts-per-unit items where a good % margin is still irrelevant ISK). This does the
  // filtering/scoring in SQL instead of hoping the summarizer keeps enough rows:
  //   - drops Skill Injectors by name (known loss-makers this account, per Matej)
  //   - unit price floor (avoid peanuts-per-unit items like Guristas Tungsten Charge L)
  //   - liquidity floor on both order counts and 14-day observed volume
  //   - sane margin band (excludes near-0% dead trades AND the >1000% reference-price
  //     artifacts that show up on illiquid niche items)
  // Ranked by profit-per-unit (capital growth), not margin %, per Matej's own standing
  // instruction to the eve-jita-scanner skill (see its SKILL.md).
  capital_deployment_candidates: `
    WITH latest AS (
      SELECT * FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_snapshots\`
      WHERE scan_date = (SELECT MAX(scan_date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.market_snapshots\`)
    )
    SELECT
      item_name, type_id,
      station_buy_avg5, station_sell_avg5,
      station_buy_orders, station_sell_orders,
      station_buy_volume, station_sell_volume,
      avg_daily_volume_14d,
      station_margin_pct,
      ROUND(station_sell_avg5 * (1 - 0.0139 - 0.03375) - station_buy_avg5 * 1.0139, 2) AS profit_per_unit_new_order
    FROM latest
    WHERE item_name NOT LIKE '%Skill Injector%'
      AND station_sell_avg5 >= 5000
      AND station_buy_orders >= 5 AND station_sell_orders >= 5
      AND avg_daily_volume_14d >= 30
      AND station_margin_pct BETWEEN 4 AND 60
    ORDER BY profit_per_unit_new_order DESC
    LIMIT 40`,

  // Added 2026-08-26 right after esi-wallet-poller's first successful run — a quick
  // sanity check that transactions/journal actually landed (row counts + date range)
  // before building anything more elaborate (real fill-velocity vs. `issued`-based
  // staleness) on top of these tables.
  wallet_summary: `
    SELECT
      (SELECT COUNT(*) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\`) AS tx_count,
      (SELECT MIN(date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\`) AS tx_earliest,
      (SELECT MAX(date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\`) AS tx_latest,
      (SELECT COUNT(*) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`) AS journal_count,
      (SELECT MIN(date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`) AS journal_earliest,
      (SELECT MAX(date) FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`) AS journal_latest`,

  // Added 2026-08-26 — Matej asked: jsou-li vyděláváme, roste-li kapitál, kolik ISK
  // proteklo od minulého refreshe. `balance` in wallet_journal is the running WALLET
  // CASH balance after each entry — it does NOT include ISK currently locked as escrow
  // in open buy orders, so pure balance deltas understate capital while buy orders are
  // open. total_capital_estimate below adds back that escrow (from the latest my_orders
  // snapshot) to correct for it; it does NOT add the market value of open sell listings
  // (that ISK is still "yours" as inventory, just not cash — shown separately as
  // listed_sell_value for context, not folded into the capital total to avoid
  // double-counting cost basis already reflected in past buy transactions).
  wallet_capital: `
    WITH latest_balance AS (
      SELECT balance AS current_balance, date AS as_of
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      ORDER BY date DESC LIMIT 1
    ),
    balance_24h_ago AS (
      SELECT balance FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      WHERE date <= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
      ORDER BY date DESC LIMIT 1
    ),
    balance_7d_ago AS (
      SELECT balance FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      WHERE date <= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
      ORDER BY date DESC LIMIT 1
    ),
    cashflow_24h AS (
      SELECT
        SUM(amount) AS net_cashflow,
        SUM(ABS(amount)) AS gross_flow,
        COUNT(*) AS entry_count
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      WHERE date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    ),
    cashflow_7d AS (
      SELECT SUM(amount) AS net_cashflow, SUM(ABS(amount)) AS gross_flow
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      WHERE date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    ),
    latest_orders AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
        FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.my_orders\`
      ) WHERE rn = 1
    ),
    escrow AS (
      SELECT SUM(price * volume_remain) AS locked_in_buy_orders
      FROM latest_orders WHERE is_buy_order
    ),
    sell_listings AS (
      SELECT SUM(price * volume_remain) AS listed_sell_value
      FROM latest_orders WHERE NOT is_buy_order
    )
    SELECT
      lb.current_balance, lb.as_of,
      b24.balance AS balance_24h_ago, lb.current_balance - b24.balance AS wallet_change_24h,
      b7.balance AS balance_7d_ago, lb.current_balance - b7.balance AS wallet_change_7d,
      c24.net_cashflow AS net_cashflow_24h, c24.gross_flow AS gross_flow_24h, c24.entry_count AS entries_24h,
      c7.net_cashflow AS net_cashflow_7d, c7.gross_flow AS gross_flow_7d,
      esc.locked_in_buy_orders, sl.listed_sell_value,
      lb.current_balance + esc.locked_in_buy_orders AS total_capital_estimate,
      (lb.current_balance + esc.locked_in_buy_orders)
        - (b24.balance + esc.locked_in_buy_orders) AS capital_change_24h_approx
    FROM latest_balance lb, balance_24h_ago b24, balance_7d_ago b7,
         cashflow_24h c24, cashflow_7d c7, escrow esc, sell_listings sl`,

  // Diagnostic companion to wallet_capital — groups raw cash flow by ESI's actual
  // ref_type strings so the breakdown (fees vs. tax vs. trades vs. escrow moves) can be
  // built with the real names instead of guessed ones.
  wallet_breakdown_by_type: `
    SELECT ref_type, COUNT(*) AS entries, SUM(amount) AS net_amount, SUM(ABS(amount)) AS gross_amount
    FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
    WHERE date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    GROUP BY ref_type
    ORDER BY gross_amount DESC`,

  // Added 2026-08-26 after Matej correctly called out that netting wallet_journal's
  // market_transaction amounts over an arbitrary short window (e.g. 7 days) isn't real
  // profit — if that window happens to contain only sells and no buy fills (as the 7-day
  // one did: market_transaction's gross_amount == net_amount there, meaning zero negative
  // entries), the result is just gross sell revenue with the cost of goods sold entirely
  // excluded. This uses wallet_transactions (which has full history now, not a rolling
  // window) to total actual buy spend vs. sell revenue, netted against the matching fees
  // from wallet_journal over the SAME date range.
  //
  // Caveat that still applies and can't be fixed without full inventory/lot accounting:
  // this assumes ending inventory (unsold stock + ISK reserved in currently-open buy-order
  // escrow that hasn't resulted in a fill yet) is ~zero. It isn't — Matej has real open
  // buy and sell orders — so this is a cash-based approximation, not a true P&L. It's a
  // much better one than the 7-day window, though, since it can't hit the "zero buys in
  // the window" artifact once the history is long enough to contain both sides.
  trading_pnl_full_history: `
    WITH tx_totals AS (
      SELECT
        MIN(date) AS earliest, MAX(date) AS latest,
        SUM(IF(is_buy, quantity * unit_price, 0)) AS total_buy_spend,
        SUM(IF(NOT is_buy, quantity * unit_price, 0)) AS total_sell_revenue,
        COUNTIF(is_buy) AS buy_tx_count,
        COUNTIF(NOT is_buy) AS sell_tx_count
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\`
    ),
    fees AS (
      SELECT
        SUM(IF(ref_type = 'brokers_fee', -amount, 0)) AS broker_fees,
        SUM(IF(ref_type = 'transaction_tax', -amount, 0)) AS sales_tax,
        SUM(IF(ref_type = 'market_provider_tax', -amount, 0)) AS scc_surcharge
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`, tx_totals
      WHERE date BETWEEN tx_totals.earliest AND tx_totals.latest
    )
    SELECT
      t.earliest, t.latest, t.buy_tx_count, t.sell_tx_count,
      t.total_buy_spend, t.total_sell_revenue,
      f.broker_fees, f.sales_tax, f.scc_surcharge,
      (t.total_sell_revenue - t.total_buy_spend - f.broker_fees - f.sales_tax - f.scc_surcharge)
        AS net_cash_pnl_approx
    FROM tx_totals t, fees f`,

  // Added 2026-08-26 — Matej wants day-to-day tracking rather than one lifetime-to-date
  // number, partly because trading_pnl_full_history mixes in escalation-loot sales
  // (zero cost basis, inflates "profit" but isn't repeatable trading skill) alongside
  // actual station trading. This doesn't separate loot from trades (no item-source flag
  // exists in ESI's transaction data), but per-day granularity at least makes a loot
  // windfall visible as a one-day spike instead of buried in a 26-day total.
  trading_pnl_daily: `
    WITH daily_tx AS (
      SELECT DATE(date) AS day,
        SUM(IF(is_buy, quantity * unit_price, 0)) AS buy_spend,
        SUM(IF(NOT is_buy, quantity * unit_price, 0)) AS sell_revenue,
        COUNTIF(is_buy) AS buy_count,
        COUNTIF(NOT is_buy) AS sell_count
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_transactions\`
      GROUP BY day
    ),
    daily_fees AS (
      SELECT DATE(date) AS day,
        SUM(IF(ref_type = 'brokers_fee', -amount, 0)) AS broker_fees,
        SUM(IF(ref_type = 'transaction_tax', -amount, 0)) AS sales_tax,
        SUM(IF(ref_type = 'market_provider_tax', -amount, 0)) AS scc_surcharge
      FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      GROUP BY day
    ),
    daily_balance AS (
      SELECT day, balance FROM (
        SELECT DATE(date) AS day, balance,
          ROW_NUMBER() OVER (PARTITION BY DATE(date) ORDER BY date DESC) AS rn
        FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.wallet_journal\`
      ) WHERE rn = 1
    )
    SELECT
      t.day, t.buy_count, t.sell_count, t.buy_spend, t.sell_revenue,
      IFNULL(f.broker_fees, 0) AS broker_fees, IFNULL(f.sales_tax, 0) AS sales_tax,
      IFNULL(f.scc_surcharge, 0) AS scc_surcharge,
      (t.sell_revenue - t.buy_spend - IFNULL(f.broker_fees, 0) - IFNULL(f.sales_tax, 0) - IFNULL(f.scc_surcharge, 0))
        AS net_cash_pnl,
      b.balance AS balance_eod
    FROM daily_tx t
    LEFT JOIN daily_fees f USING (day)
    LEFT JOIN daily_balance b USING (day)
    ORDER BY t.day DESC
    LIMIT 30`,
};

async function handleReport(req, reqUrl, res) {
  if (REPORT_KEY && reqUrl.searchParams.get('key') !== REPORT_KEY) {
    res.writeHead(403, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'missing or wrong ?key=' }));
    return;
  }
  const name = reqUrl.searchParams.get('name');
  const sql = REPORTS[name];
  if (!sql) {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `unknown report name`, available: Object.keys(REPORTS) }));
    return;
  }
  try {
    // useQueryCache: false — without this, BigQuery serves its cached result for this
    // exact literal SQL string for up to 24h regardless of the underlying tables having
    // changed since. Discovered 2026-08-26: two confirmed-successful poller runs in a
    // row (my_orders, perimeter) produced byte-identical /report output — new orders
    // placed in-game never appeared. WebFetch cache-busting (?_cb=...) had no effect
    // because the cache is server-side on BigQuery's end, keyed on the SQL text itself,
    // not on this endpoint's URL.
    const [rows] = await bigquery.query({ query: sql, useQueryCache: false });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ name, row_count: rows.length, rows }));
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: err.message }));
  }
}

function base64url(buf) {
  return buf.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function html(body) {
  return `<!doctype html><meta charset="utf-8"><title>EVE ESI login</title>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:64px auto;line-height:1.6">${body}</body>`;
}

async function handleLogin(req, reqUrl, res) {
  if (!CLIENT_ID || !CLIENT_SECRET) {
    res.writeHead(500, { 'Content-Type': 'text/html' });
    res.end(html('<h1>Not configured</h1><p>EVE_SSO_CLIENT_ID / EVE_SSO_CLIENT_SECRET env vars are missing on this service.</p>'));
    return;
  }
  // This service allows unauthenticated invocations (so a plain click works, no
  // gcloud auth needed) — LOGIN_KEY is a lightweight guard against a stranger who
  // stumbles onto the URL triggering a login that would overwrite Matej's stored
  // credentials with their own. Not a substitute for keeping the URL private, just
  // defense in depth.
  if (LOGIN_KEY && reqUrl.searchParams.get('key') !== LOGIN_KEY) {
    res.writeHead(403, { 'Content-Type': 'text/html' });
    res.end(html('<h1>Missing or wrong key</h1><p>Append <code>?key=...</code> with the value set in LOGIN_KEY.</p>'));
    return;
  }
  const codeVerifier = base64url(crypto.randomBytes(32));
  const codeChallenge = base64url(crypto.createHash('sha256').update(codeVerifier).digest());
  const state = base64url(crypto.randomBytes(16));

  await firestore.collection(STATE_COLLECTION).doc(state).set({
    code_verifier: codeVerifier,
    created_at: Firestore.Timestamp.now(),
  });

  // Cloud Run terminates TLS and forwards the original host; req.headers.host is the
  // public hostname, so this always builds the right redirect_uri without needing to
  // hardcode the service's own URL (which isn't known before the first deploy anyway).
  const redirectUri = `https://${req.headers.host}/callback`;

  const authUrl = new URL(`${SSO_BASE}/v2/oauth/authorize/`);
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('redirect_uri', redirectUri);
  authUrl.searchParams.set('client_id', CLIENT_ID);
  authUrl.searchParams.set('scope', SCOPES);
  authUrl.searchParams.set('state', state);
  authUrl.searchParams.set('code_challenge', codeChallenge);
  authUrl.searchParams.set('code_challenge_method', 'S256');

  res.writeHead(302, { Location: authUrl.toString() });
  res.end();
}

async function handleCallback(req, reqUrl, res) {
  const state = reqUrl.searchParams.get('state');
  const code = reqUrl.searchParams.get('code');
  if (!state || !code) {
    res.writeHead(400, { 'Content-Type': 'text/html' });
    res.end(html('<h1>Missing code or state</h1><p>Start over from <a href="/login">/login</a>.</p>'));
    return;
  }

  const stateDoc = await firestore.collection(STATE_COLLECTION).doc(state).get();
  if (!stateDoc.exists) {
    res.writeHead(400, { 'Content-Type': 'text/html' });
    res.end(html('<h1>Unknown or expired state</h1><p>Start over from <a href="/login">/login</a> — PKCE states expire after 10 minutes.</p>'));
    return;
  }
  const { code_verifier: codeVerifier } = stateDoc.data();
  await stateDoc.ref.delete(); // one-time use

  const redirectUri = `https://${req.headers.host}/callback`;
  const tokenRes = await fetch(`${SSO_BASE}/v2/oauth/token`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Host: 'login.eveonline.com',
      Authorization: `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString('base64')}`,
    },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code,
      code_verifier: codeVerifier,
    }),
  });
  const tokenJson = await tokenRes.json();
  if (!tokenRes.ok) {
    res.writeHead(500, { 'Content-Type': 'text/html' });
    res.end(html(`<h1>Token exchange failed</h1><pre>${JSON.stringify(tokenJson, null, 2)}</pre>`));
    return;
  }

  const payload = JSON.parse(Buffer.from(tokenJson.access_token.split('.')[1], 'base64').toString());
  const characterId = Number(String(payload.sub).split(':').pop());
  const characterName = payload.name;

  const parent = `projects/${GCP_PROJECT_ID}/secrets/${CREDENTIALS_SECRET_NAME}`;
  const payloadData = Buffer.from(JSON.stringify({
    character_id: characterId,
    character_name: characterName,
    refresh_token: tokenJson.refresh_token,
    updated_at: new Date().toISOString(),
  }));
  try {
    await secretClient.addSecretVersion({ parent, payload: { data: payloadData } });
  } catch (err) {
    if (err.code === 5 /* NOT_FOUND */) {
      // First run — secret doesn't exist yet. deploy_esi.sh creates it ahead of time
      // normally; this fallback creates it on the fly so a fresh setup still works.
      await secretClient.createSecret({
        parent: `projects/${GCP_PROJECT_ID}`,
        secretId: CREDENTIALS_SECRET_NAME,
        secret: { replication: { automatic: {} } },
      });
      await secretClient.addSecretVersion({ parent, payload: { data: payloadData } });
    } else {
      throw err;
    }
  }

  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(html(`<h1>Logged in as ${characterName}</h1><p>Refresh token saved to Secret Manager (<code>${CREDENTIALS_SECRET_NAME}</code>). You can close this tab — the esi-jobs Cloud Run Jobs will pick it up from here.</p>`));
}

const server = http.createServer(async (req, res) => {
  try {
    const reqUrl = new URL(req.url, `https://${req.headers.host}`);
    if (reqUrl.pathname === '/' || reqUrl.pathname === '/login') {
      await handleLogin(req, reqUrl, res);
    } else if (reqUrl.pathname === '/callback') {
      await handleCallback(req, reqUrl, res);
    } else if (reqUrl.pathname === '/report') {
      await handleReport(req, reqUrl, res);
    } else if (reqUrl.pathname === '/healthz') {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('ok');
    } else {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found');
    }
  } catch (err) {
    console.error(err);
    res.writeHead(500, { 'Content-Type': 'text/html' });
    res.end(html(`<h1>Something went wrong</h1><pre>${err.message}</pre>`));
  }
});

server.listen(PORT, () => console.log(`esi-oauth-service listening on :${PORT}`));
