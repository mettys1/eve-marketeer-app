#!/usr/bin/env node
//
// One-time EVE SSO login (OAuth2 Authorization Code + PKCE, public/native client —
// no client secret needed or stored anywhere). Run this once on your own machine:
//
//   node esi-auth/get_refresh_token.js
//
// It prints a login URL — open it, log in with your EVE account, approve the scope,
// and you'll be redirected back to a localhost page this script is listening on.
// The resulting refresh token is saved to esi-auth/.credentials.json (gitignored —
// never commit it, it's equivalent to a long-lived login for read-only order data).
//
// Prereq (do this first, one time, in the browser):
//   1. https://developers.eveonline.com/applications → Create New Application
//   2. Connection Type: "Authentication & API Access"
//   3. Application Type: "Public Client (SSO Native/Mobile)" — NOT "Confidential" —
//      public clients use PKCE instead of a secret, so nothing sensitive ever needs
//      to be stored in this repo or on disk besides the refresh token itself.
//   4. Scopes: esi-markets.read_character_orders.v1 (your own orders) and
//      esi-markets.structure_markets.v1 (full order book of player-owned citadels you
//      can dock at, e.g. Perimeter's "0.0% Neutral States Market HQ" — unlike Jita's
//      NPC station, a citadel's market isn't visible without this authenticated call).
//      (add esi-wallet.read_character_wallet.v1 too if you also want wallet balance
//      pulled automatically later — costs nothing to request now, can ignore it)
//   5. Callback URL: http://localhost:8765/callback
//   6. Save. Copy the "Client ID" shown — paste it into CLIENT_ID below.

'use strict';
const http = require('http');
const crypto = require('crypto');
const { URL } = require('url');

const CLIENT_ID = process.env.EVE_SSO_CLIENT_ID || 'PASTE_YOUR_CLIENT_ID_HERE';
const CALLBACK_PORT = 8765;
const REDIRECT_URI = `http://localhost:${CALLBACK_PORT}/callback`;
const SCOPES = 'esi-markets.read_character_orders.v1 esi-markets.structure_markets.v1';
const SSO_BASE = 'https://login.eveonline.com';
const CREDS_PATH = require('path').join(__dirname, '.credentials.json');

function base64url(buf) {
  return buf.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function main() {
  if (CLIENT_ID === 'PASTE_YOUR_CLIENT_ID_HERE') {
    console.error('Edit esi-auth/get_refresh_token.js and set CLIENT_ID to the Client ID from');
    console.error('developers.eveonline.com/applications (or set EVE_SSO_CLIENT_ID env var).');
    process.exit(1);
  }

  const codeVerifier = base64url(crypto.randomBytes(32));
  const codeChallenge = base64url(crypto.createHash('sha256').update(codeVerifier).digest());
  const state = base64url(crypto.randomBytes(16));

  const authUrl = new URL(`${SSO_BASE}/v2/oauth/authorize/`);
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
  authUrl.searchParams.set('client_id', CLIENT_ID);
  authUrl.searchParams.set('scope', SCOPES);
  authUrl.searchParams.set('state', state);
  authUrl.searchParams.set('code_challenge', codeChallenge);
  authUrl.searchParams.set('code_challenge_method', 'S256');

  const server = http.createServer(async (req, res) => {
    const reqUrl = new URL(req.url, REDIRECT_URI);
    if (reqUrl.pathname !== '/callback') {
      res.writeHead(404); res.end(); return;
    }
    const returnedState = reqUrl.searchParams.get('state');
    const code = reqUrl.searchParams.get('code');
    if (returnedState !== state || !code) {
      res.writeHead(400, { 'Content-Type': 'text/plain' });
      res.end('State mismatch or missing code — try again from a fresh run of this script.');
      server.close();
      return;
    }

    try {
      const authHeaders = {
        'Content-Type': 'application/x-www-form-urlencoded',
        Host: 'login.eveonline.com',
      };
      // If your app registration issued a Client Secret (i.e. it came out as a
      // Confidential application rather than Public/native), send it via HTTP Basic
      // Auth alongside PKCE — set EVE_SSO_CLIENT_SECRET in your shell, never hardcode
      // it here or commit it anywhere.
      const usingBasicAuth = Boolean(process.env.EVE_SSO_CLIENT_SECRET);
      if (usingBasicAuth) {
        authHeaders.Authorization = `Basic ${Buffer.from(`${CLIENT_ID}:${process.env.EVE_SSO_CLIENT_SECRET}`).toString('base64')}`;
      }
      // EVE SSO rejects the request if client_id is present both in the Authorization
      // header AND the body — include it in exactly one place.
      const bodyParams = { grant_type: 'authorization_code', code, code_verifier: codeVerifier };
      if (!usingBasicAuth) bodyParams.client_id = CLIENT_ID;
      const tokenRes = await fetch(`${SSO_BASE}/v2/oauth/token`, {
        method: 'POST',
        headers: authHeaders,
        body: new URLSearchParams(bodyParams),
      });
      const tokenJson = await tokenRes.json();
      if (!tokenRes.ok) throw new Error(`Token exchange failed: ${JSON.stringify(tokenJson)}`);

      // Decode the JWT access token's payload to pull character id/name — no
      // signature verification needed here, we trust the endpoint we just called.
      const payload = JSON.parse(Buffer.from(tokenJson.access_token.split('.')[1], 'base64').toString());
      const characterId = Number(String(payload.sub).split(':').pop());
      const characterName = payload.name;

      const fs = require('fs');
      fs.writeFileSync(
        CREDS_PATH,
        JSON.stringify({ character_id: characterId, character_name: characterName, refresh_token: tokenJson.refresh_token }, null, 2)
      );

      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end(`Logged in as ${characterName}. Refresh token saved to esi-auth/.credentials.json — you can close this tab.`);
      console.log(`\nSaved credentials for ${characterName} (character_id ${characterId}) to ${CREDS_PATH}`);
      console.log('Run: node esi-auth/fetch_my_orders.js — any time you want fresh order data.');
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' });
      res.end(`Something went wrong: ${err.message}`);
      console.error(err);
    } finally {
      server.close();
    }
  });

  server.listen(CALLBACK_PORT, () => {
    console.log('Open this URL in your browser and log in with the character whose orders you want to track:\n');
    console.log(authUrl.toString());
    console.log(`\nWaiting for the callback on http://localhost:${CALLBACK_PORT}/callback ...`);
  });
}

main();
