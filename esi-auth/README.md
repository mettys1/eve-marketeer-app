# ESI character orders — auto-pull instead of screenshots

Pulls your own open market orders (buy + sell, any station/structure) straight from
ESI, authenticated as you via EVE SSO. Replaces manually screenshotting the in-game
order list.

**I (Claude) can't do the login step myself** — it's an interactive browser OAuth
consent flow with EVE's SSO, no scriptable equivalent, same reason `deploy.sh`'s
Cloud Build connection step is manual. Everything below is commands you run yourself,
once for setup and then any time you want fresh order data.

## One-time setup

1. Register an application at
   [developers.eveonline.com/applications](https://developers.eveonline.com/applications)
   → **Create New Application**:
   - Connection Type: **Authentication & API Access**
   - Application Type: **Public Client (SSO Native/Mobile)** if that option is offered
     — a public client uses PKCE instead of a client secret, so there's no secret to
     ever store or leak. **If CCP's form issues you a Client Secret anyway** (it did
     for Matej 2026-08-26 — the option may not always be shown, or the app defaults to
     Confidential), that's fine too, the scripts here support both: just set
     `EVE_SSO_CLIENT_SECRET` as a local environment variable (never paste it into a
     chat, a commit, or anywhere in this repo — treat it like a password). Regenerate
     it from the app settings if you think it's ever been exposed.
   - Scopes: `esi-markets.read_character_orders.v1` (your own orders) and
     `esi-markets.structure_markets.v1` (full order book of citadels you can dock at,
     e.g. Perimeter's "0.0% Neutral States Market HQ" — a player-owned structure, so
     unlike Jita's NPC station its market isn't visible any other way). Both are
     under the `esi-markets` category — expand it and check the two specific ones,
     not the whole category.
   - Callback URL: `http://localhost:8765/callback`
   - Save, then copy the **Client ID** it shows you.

2. Run the login script once, with that Client ID (and Client Secret, if you got
   one — see above):
   ```
   EVE_SSO_CLIENT_ID=<id> EVE_SSO_CLIENT_SECRET=<secret> node esi-auth/get_refresh_token.js
   ```
   (Omit `EVE_SSO_CLIENT_SECRET=...` entirely if your app didn't issue one.) It prints
   a login URL — open it, log in with the character whose orders you want to track,
   approve the scopes. You'll land on a local success page and the script saves a
   refresh token to `esi-auth/.credentials.json` (already gitignored — never commit
   this file, it's a standing read-only login to that character).

## Getting fresh order data

```
EVE_SSO_CLIENT_ID=<id> EVE_SSO_CLIENT_SECRET=<secret> node esi-auth/fetch_my_orders.js
```

Writes `my_orders.csv` in the repo root with every currently open order (buy and
sell, wherever they're sitting — Jita, Perimeter, anywhere). Upload that CSV into the
conversation with Claude the same way you already do `recompute_top_of_book.csv` —
Claude cross-references it against fresh market prices and tells you what to
reprice, hold, or cancel.

Consider exporting `EVE_SSO_CLIENT_ID` (and `EVE_SSO_CLIENT_SECRET`, if you have one)
in your shell profile so you don't have to paste them every time. The Client ID isn't
sensitive, but the secret is — fine to keep in your own shell profile on your own
machine (that's normal), just never commit it to the repo or paste it into a chat.

## Perimeter citadel order book

```
EVE_SSO_CLIENT_ID=<id> EVE_SSO_CLIENT_SECRET=<secret> node esi-auth/fetch_perimeter_market.js
```

Pulls the *full* order book (everyone's orders, not just yours) of the Perimeter
citadel where your own buy orders actually sit — Jita 4-4's NPC-station market is
public and already covered by the daily BigQuery pipeline, but a player-owned
structure's market requires this authenticated per-structure call. Writes
`perimeter_orders_raw.csv` (every order) and `perimeter_top_of_book.csv` (one row per
item: best buy/sell + margin, same shape as `recompute_top_of_book.csv` — feed it to
`reports/generate_reports.py` the same way, or upload straight into the conversation).
Needs the `esi-markets.structure_markets.v1` scope from setup above — if your saved
`.credentials.json` predates adding that scope, re-run `get_refresh_token.js` first.
A 403 here usually means either the scope is missing, or this character has never
actually docked at the structure (ESI only allows market reads for structures your
character can access in-game).

## If you want wallet balance pulled automatically too

Add `esi-wallet.read_character_wallet.v1` to the scope in the application
registration (step 1) and to `SCOPES` in `get_refresh_token.js`, re-run the login
script to get a token with the wider scope, then a small addition to
`fetch_my_orders.js` (or a sibling script) can call
`GET /characters/{character_id}/wallet/` the same way orders are fetched above. Not
built yet — ask if you want it.
