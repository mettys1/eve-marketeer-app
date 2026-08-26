# ESI login — all in GCP, no local machine needed

Replaces `esi-auth/`'s local OAuth flow (which needed a script running on the same
machine as your browser, because of the `localhost` callback) with a real Cloud Run
service that has its own public HTTPS URL. Once set up, the whole thing — login,
pulling your orders, pulling the Perimeter order book — runs entirely in GCP.

## One-time setup

Run from Cloud Shell (or anywhere `gcloud` is authenticated to this project):

```
EVE_SSO_CLIENT_ID=<id> \
EVE_SSO_CLIENT_SECRET=<secret> \
LOGIN_KEY=$(openssl rand -hex 16) \
bash deploy_esi.sh
```

(`LOGIN_KEY` is a random string only you'll know — it guards the login link so a
stranger who finds the Cloud Run URL can't trigger a login that overwrites your stored
credentials. Save the value the script prints, or generate and remember your own.)

This deploys the `esi-oauth-service` Cloud Run service and the `esi-my-orders-poller`
/ `esi-perimeter-poller` Cloud Run Jobs, and prints two things at the end you need:

1. **A Callback URL to paste into the CCP app settings** — go to
   [developers.eveonline.com/applications](https://developers.eveonline.com/applications),
   open your app, replace whatever Callback URL is there now (e.g.
   `http://localhost:8765/callback` from the old local flow — that one doesn't apply
   here) with the printed `https://esi-oauth-service-xxxxx.run.app/callback`. Save.
2. **A login link** (`https://.../login?key=...`) — open it in a browser, log in with
   the character whose orders you want to track, approve the scopes. A short success
   page confirms it and the refresh token lands in Secret Manager
   (`esi-credentials`) — the two Cloud Run Jobs read from there.

Scopes requested: `esi-markets.read_character_orders.v1` (your own orders) and
`esi-markets.structure_markets.v1` (Perimeter's full order book — a player-owned
citadel, so unlike Jita's NPC station its market needs this authenticated call). Both
must already be enabled on the app in the CCP portal — same requirement as before.

## Getting fresh data

Standardized 2026-08-26 to the exact same shape as the main Jita pipeline's
`refresh.sh` — one script per data source, each does "run the Cloud Run Job, then
pull a ready-to-upload CSV out of BigQuery":

```
bash refresh_my_orders.sh     # -> my_orders_latest.csv (your own open orders)
bash refresh_perimeter.sh     # -> perimeter_top_of_book.csv (Perimeter's full book,
                               #    recomputed to top-of-book/margin the same way
                               #    bigquery/recompute_top_of_book.sql does for Jita —
                               #    see bigquery/recompute_perimeter_top_of_book.sql)
```

Upload either CSV into the conversation with Claude, same as `recompute_top_of_book.csv`
always has been. Both write to BigQuery first (`eve_jita_scanner.my_orders` /
`.perimeter_orders_raw` — schema in `bigquery/schema.sql`) so the raw history is kept
regardless of whether you remember to upload the CSV that run.

## Manual-trigger only, on purpose

Same reasoning as the main poller (`SETUP_SCHEDULER=false` by default there): no Cloud
Scheduler wired up for these two jobs, so nothing pulls your order data on a timer.
Run them when you actually want fresh numbers. Ask if you want that automated later.

## Re-authorizing

If EVE SSO ever revokes the refresh token (password change, long inactivity, you
revoke it yourself in your EVE account settings), or you add a new scope, just visit
the `/login?key=...` link again — it overwrites the stored credentials with a fresh
login.

## `esi-auth/` (the old local scripts)

Still in the repo as a fallback if you ever want to run this from a local machine
instead (e.g. no `gcloud` handy) — see `esi-auth/README.md`. Not the primary path
anymore; this Cloud Run setup is.
