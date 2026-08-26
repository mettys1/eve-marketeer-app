#!/usr/bin/env bash
#
# esi-oauth-service + esi-jobs — GCP deploy script, same pattern as deploy.sh: run it
# from Cloud Shell (or any shell with gcloud authenticated to this project), section by
# section the first time. Idempotent-ish — safe to re-run.
#
# What this deploys, entirely inside eve-jita-scanner-21359 (same project as the daily
# Jita poller — no separate project needed):
#   - Firestore (Native mode) — short-lived PKCE state during login, nothing else.
#   - Secret Manager secret `esi-credentials` — holds the refresh token once you log in.
#   - Cloud Run SERVICE `esi-oauth-service` — the login page / OAuth callback endpoint.
#     Unlike the poller's Cloud Run JOB, a service stays reachable at a stable URL.
#   - Cloud Run JOBS `esi-my-orders-poller` / `esi-perimeter-poller` — pull data using
#     the credentials the service stored, write straight to BigQuery. Manual-trigger
#     only, same as the main poller (SETUP_SCHEDULER-style choice — Matej controls when
#     these run).
#
# Prereqs: same as deploy.sh — gcloud CLI authenticated, `bash deploy.sh` already run
# once (project/APIs/Artifact Registry already exist).

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
AR_REPO="eve-jita-poller"       # reusing the same Artifact Registry repo as the main poller
BQ_DATASET="eve_jita_scanner"
OAUTH_SERVICE_NAME="esi-oauth-service"
OAUTH_SA="esi-oauth-service-sa"
JOBS_SA="esi-jobs-sa"
CREDENTIALS_SECRET_NAME="esi-credentials"

# ---- Things you fill in ----
EVE_SSO_CLIENT_ID="${EVE_SSO_CLIENT_ID:?Set EVE_SSO_CLIENT_ID env var before running (from developers.eveonline.com)}"
EVE_SSO_CLIENT_SECRET="${EVE_SSO_CLIENT_SECRET:?Set EVE_SSO_CLIENT_SECRET env var before running}"
# A random string only you know — appended as ?key=... to the login link so a random
# visitor who finds the Cloud Run URL can't trigger a login that overwrites your
# stored credentials. Generate one: `openssl rand -hex 16`
LOGIN_KEY="${LOGIN_KEY:?Set LOGIN_KEY env var before running — generate one with: openssl rand -hex 16}"

echo "== Step 1: enable Firestore + Secret Manager APIs =="
gcloud services enable firestore.googleapis.com secretmanager.googleapis.com --project="$PROJECT_ID"

echo "== Step 2: create Firestore database (Native mode) if it doesn't exist =="
# A project can only ever have ONE default Firestore database, and its location can't
# be changed later — if this already exists (e.g. from something else in this
# project), the command just errors "already exists", which is fine.
gcloud firestore databases create --location="$REGION" --project="$PROJECT_ID" \
  || echo "Firestore database already exists, continuing..."

echo "== Step 3: create the Secret Manager secret (empty placeholder — the service"
echo "           writes the actual token into it after you log in) =="
gcloud secrets create "$CREDENTIALS_SECRET_NAME" --project="$PROJECT_ID" --replication-policy=automatic \
  || echo "Secret already exists, continuing..."

echo "== Step 4: service account for esi-oauth-service =="
gcloud iam service-accounts create "$OAUTH_SA" --display-name="ESI OAuth service" --project="$PROJECT_ID" \
  || echo "Service account already exists, continuing..."
OAUTH_SA_EMAIL="${OAUTH_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
# datastore.user for Firestore (PKCE state); secretmanager.admin so it can both create
# the secret (first-run fallback in server.js) and add new versions on every login —
# scoped to this one project, which is personal/single-user, so project-wide is an
# acceptable tradeoff for keeping this script simple.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${OAUTH_SA_EMAIL}" --role="roles/datastore.user" > /dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${OAUTH_SA_EMAIL}" --role="roles/secretmanager.admin" > /dev/null

echo "== Step 5: service account for esi-jobs =="
gcloud iam service-accounts create "$JOBS_SA" --display-name="ESI jobs (my orders / Perimeter)" --project="$PROJECT_ID" \
  || echo "Service account already exists, continuing..."
JOBS_SA_EMAIL="${JOBS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${JOBS_SA_EMAIL}" --role="roles/secretmanager.secretAccessor" > /dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${JOBS_SA_EMAIL}" --role="roles/bigquery.dataEditor" > /dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${JOBS_SA_EMAIL}" --role="roles/bigquery.jobUser" > /dev/null

echo "== Step 6: build + push esi-oauth-service image =="
OAUTH_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/esi-oauth-service:latest"
gcloud builds submit "$(dirname "$0")/esi-oauth-service" --tag "$OAUTH_IMAGE" --project="$PROJECT_ID"

echo "== Step 7: deploy esi-oauth-service as a Cloud Run SERVICE =="
gcloud run deploy "$OAUTH_SERVICE_NAME" \
  --image="$OAUTH_IMAGE" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --service-account="$OAUTH_SA_EMAIL" \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=2 \
  --set-env-vars="EVE_SSO_CLIENT_ID=${EVE_SSO_CLIENT_ID},EVE_SSO_CLIENT_SECRET=${EVE_SSO_CLIENT_SECRET},GCP_PROJECT_ID=${PROJECT_ID},CREDENTIALS_SECRET_NAME=${CREDENTIALS_SECRET_NAME},LOGIN_KEY=${LOGIN_KEY}"

SERVICE_URL=$(gcloud run services describe "$OAUTH_SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')

echo ""
echo "== IMPORTANT — one manual step in the CCP developer portal =="
echo "Go to https://developers.eveonline.com/applications, open your app, and set the"
echo "Callback URL to exactly:"
echo "  ${SERVICE_URL}/callback"
echo "(Replace whatever's there now, e.g. http://localhost:8765/callback — that only"
echo "worked for the local flow, this service needs its own real URL.) Save."
echo ""
echo "Then log in any time by visiting:"
echo "  ${SERVICE_URL}/login?key=${LOGIN_KEY}"
echo ""

echo "== Step 8: build + push esi-jobs image =="
JOBS_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/esi-jobs:latest"
gcloud builds submit "$(dirname "$0")/esi-jobs" --tag "$JOBS_IMAGE" --project="$PROJECT_ID"

echo "== Step 9: create the two Cloud Run JOBS (or update, if this is a re-run) =="
JOB_ENV_VARS="EVE_SSO_CLIENT_ID=${EVE_SSO_CLIENT_ID},EVE_SSO_CLIENT_SECRET=${EVE_SSO_CLIENT_SECRET},GCP_PROJECT_ID=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},CREDENTIALS_SECRET_NAME=${CREDENTIALS_SECRET_NAME}"

gcloud run jobs create esi-my-orders-poller \
  --image="$JOBS_IMAGE" --command="node" --args="job_my_orders.js" \
  --region="$REGION" --project="$PROJECT_ID" \
  --service-account="$JOBS_SA_EMAIL" --set-env-vars="$JOB_ENV_VARS" \
  --max-retries=1 --task-timeout=600 \
  || gcloud run jobs update esi-my-orders-poller \
       --image="$JOBS_IMAGE" --command="node" --args="job_my_orders.js" \
       --region="$REGION" --project="$PROJECT_ID" \
       --service-account="$JOBS_SA_EMAIL" --set-env-vars="$JOB_ENV_VARS" \
       --max-retries=1 --task-timeout=600

gcloud run jobs create esi-perimeter-poller \
  --image="$JOBS_IMAGE" --command="node" --args="job_perimeter.js" \
  --region="$REGION" --project="$PROJECT_ID" \
  --service-account="$JOBS_SA_EMAIL" --set-env-vars="$JOB_ENV_VARS" \
  --max-retries=1 --task-timeout=600 \
  || gcloud run jobs update esi-perimeter-poller \
       --image="$JOBS_IMAGE" --command="node" --args="job_perimeter.js" \
       --region="$REGION" --project="$PROJECT_ID" \
       --service-account="$JOBS_SA_EMAIL" --set-env-vars="$JOB_ENV_VARS" \
       --max-retries=1 --task-timeout=600

echo ""
echo "Done."
echo "1. Set the Callback URL in CCP's app settings to ${SERVICE_URL}/callback (see above)."
echo "2. Log in once: ${SERVICE_URL}/login?key=${LOGIN_KEY}"
echo "3. Then any time: gcloud run jobs execute esi-my-orders-poller --region=$REGION"
echo "               and gcloud run jobs execute esi-perimeter-poller --region=$REGION"
echo "   Results land in BigQuery ${PROJECT_ID}.${BQ_DATASET}.my_orders / .perimeter_orders_raw"
