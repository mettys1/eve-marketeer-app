// Shared helpers for esi-jobs/ Cloud Run Jobs — pulls the refresh token from Secret
// Manager (written there by esi-oauth-service/, see that folder's README) instead of
// a local .credentials.json file, since these run as Cloud Run Jobs with no local
// filesystem worth persisting.
'use strict';
const { SecretManagerServiceClient } = require('@google-cloud/secret-manager');

const SSO_BASE = 'https://login.eveonline.com';
const ESI_BASE = 'https://esi.evetech.net/latest';

const GCP_PROJECT_ID = process.env.GCP_PROJECT_ID || 'eve-jita-scanner-21359';
const CREDENTIALS_SECRET_NAME = process.env.CREDENTIALS_SECRET_NAME || 'esi-credentials';
const CLIENT_ID = process.env.EVE_SSO_CLIENT_ID;
const CLIENT_SECRET = process.env.EVE_SSO_CLIENT_SECRET;

async function loadCredentials() {
  if (!CLIENT_ID || !CLIENT_SECRET) {
    throw new Error('EVE_SSO_CLIENT_ID / EVE_SSO_CLIENT_SECRET env vars are missing on this job.');
  }
  const client = new SecretManagerServiceClient();
  const name = `projects/${GCP_PROJECT_ID}/secrets/${CREDENTIALS_SECRET_NAME}/versions/latest`;
  let version;
  try {
    [version] = await client.accessSecretVersion({ name });
  } catch (err) {
    throw new Error(
      `Couldn't read Secret Manager secret "${CREDENTIALS_SECRET_NAME}" (${err.message}). ` +
      'Has anyone logged in via the esi-oauth-service URL yet? See esi-oauth-service/README.md.'
    );
  }
  return JSON.parse(version.payload.data.toString('utf8'));
}

// Fixed 2026-09-03 — all three esi-jobs Cloud Run Jobs failed back-to-back with an
// unhelpful "SyntaxError: Unexpected token '<' ... is not valid JSON" from the old
// `await res.json()` call below. Root cause: EVE SSO (or something in front of it —
// a gateway/WAF) occasionally returns an HTML error page instead of a JSON body, and
// blindly calling res.json() on that just throws a useless parse error with no status
// code or body visible in the logs. Now: read the body as text first, so a bad
// response is always diagnosable (status + snippet in the thrown error), and retry a
// few times with backoff for the cases that look transient (non-JSON body, or a 5xx
// JSON error) — a genuine 4xx (bad refresh token, bad client credentials) still fails
// immediately, since retrying that would never succeed.
async function refreshAccessToken(refreshToken, { maxAttempts = 3, baseDelayMs = 2000 } = {}) {
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(`${SSO_BASE}/v2/oauth/token`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Host: 'login.eveonline.com',
        Authorization: `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString('base64')}`,
      },
      body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refreshToken }),
    });
    const bodyText = await res.text();

    let json;
    let parseError = null;
    try {
      json = JSON.parse(bodyText);
    } catch (err) {
      parseError = err;
    }

    if (parseError) {
      const err = new Error(`Token refresh returned non-JSON response (status ${res.status}): ${bodyText.slice(0, 300)}`);
      if (attempt >= maxAttempts) throw err;
      const delay = baseDelayMs * 2 ** (attempt - 1);
      console.log(`refreshAccessToken: attempt ${attempt} got a non-JSON response (status ${res.status}) — retrying in ${delay}ms...`);
      await sleep(delay);
      continue;
    }

    if (!res.ok) {
      const err = new Error(`Token refresh failed (status ${res.status}): ${JSON.stringify(json)}`);
      if (attempt >= maxAttempts || res.status < 500) throw err; // 4xx = not worth retrying (bad token/creds)
      const delay = baseDelayMs * 2 ** (attempt - 1);
      console.log(`refreshAccessToken: attempt ${attempt} failed (status ${res.status}) — retrying in ${delay}ms...`);
      await sleep(delay);
      continue;
    }

    return json.access_token;
  }
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

// Added 2026-09-04, after eve-jita-poller AND esi-perimeter-poller both failed
// back-to-back-to-back on the exact same root cause: a transient BigQuery
// insertAll/query error (503 "Service is unavailable" / 500 "internalError"),
// always right at the very last step of a run that had already done all the
// real (expensive) work — pulling ESI data, resolving names, etc. Google's own
// error message says it outright: "Retrying the job with back-off as described
// in the BigQuery SLA should solve the problem." This wraps any async BigQuery
// call with a few retries + exponential backoff instead of losing the whole
// run to one transient blip. Used by every esi-jobs/job_*.js write path;
// poller/poller.js has its own local copy (separate deploy, doesn't import
// this file).
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function isRetryableBqError(err) {
  const code = err && err.code;
  if (code === 500 || code === 503) return true;
  const reason = err && err.errors && err.errors[0] && err.errors[0].reason;
  return ['backendError', 'internalError', 'rateLimitExceeded'].includes(reason);
}

async function retryable(fn, { label = 'BigQuery operation', maxAttempts = 4, baseDelayMs = 2000 } = {}) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (attempt >= maxAttempts || !isRetryableBqError(err)) throw err;
      const delay = baseDelayMs * 2 ** (attempt - 1);
      console.log(`${label}: attempt ${attempt} failed (${err.message}) — retrying in ${delay}ms...`);
      await sleep(delay);
    }
  }
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

module.exports = { GCP_PROJECT_ID, ESI_BASE, loadCredentials, refreshAccessToken, fetchAllPages, resolveTypeNames, retryable, sleep };
