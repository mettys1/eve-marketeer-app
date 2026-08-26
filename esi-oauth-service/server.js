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

const PORT = process.env.PORT || 8080;
const CLIENT_ID = process.env.EVE_SSO_CLIENT_ID;
const CLIENT_SECRET = process.env.EVE_SSO_CLIENT_SECRET;
const GCP_PROJECT_ID = process.env.GCP_PROJECT_ID || 'eve-jita-scanner-21359';
const CREDENTIALS_SECRET_NAME = process.env.CREDENTIALS_SECRET_NAME || 'esi-credentials';
const LOGIN_KEY = process.env.LOGIN_KEY || '';
const SCOPES = 'esi-markets.read_character_orders.v1 esi-markets.structure_markets.v1';
const SSO_BASE = 'https://login.eveonline.com';
const STATE_COLLECTION = 'oauth_pending';
const STATE_TTL_MS = 10 * 60 * 1000; // PKCE flow should complete within 10 minutes

const firestore = new Firestore({ projectId: GCP_PROJECT_ID });
const secretClient = new SecretManagerServiceClient();

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
