#!/usr/bin/env bash
#
# deploy_cj.sh — C-J6MT import pipeline (added 2026-10-11). Same pattern as deploy_esi.sh:
# run from Cloud Shell (or anything with gcloud authenticated to the project), from the
# repo root. Idempotent-ish — safe to re-run.
#
# Deploys:
#   - BigQuery tables from bigquery/schema_cj.sql
#   - Cloud Run JOB  esi-cj-poller    (CJ_MODE=full — triggered by `python run_cj.py`)
#   - Cloud Run JOB  esi-cj-snapshot  (CJ_MODE=orders_only — C-J order book only, cheap)
#   - OPTIONAL (ENABLE_HOURLY=1): Cloud Scheduler hourly trigger for esi-cj-snapshot.
#     Strongly recommended: the C-J demand estimate is built from snapshot diffs, and with
#     only one snapshot a day every order that sells out completely in between is just
#     "disappeared" (can't tell sold from cancelled). Cost: one tiny job run per hour.
#   - OPTIONAL (REDEPLOY_OAUTH=1): rebuild esi-oauth-service to get /login?target=cj
#     (only needed if the trading character in esi-credentials has NO C-J access).
#
# Usage:
#   export EVE_SSO_CLIENT_ID=... EVE_SSO_CLIENT_SECRET=...
#   export CJ_STRUCTURE_ID=...          # C-J Keepstar structure_id (see docs/cj-pipeline.md)
#   # optional:
#   export CJ_CREDENTIALS_SECRET_NAME=esi-credentials-cj   # if using a separate C-J character
#   export ENABLE_HOURLY=1 REDEPLOY_OAUTH=1 LOGIN_KEY=...
#   bash deploy_cj.sh

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
AR_REPO="eve-jita-poller"
BQ_DATASET="eve_jita_scanner"
JOBS_SA="esi-jobs-sa"
JOBS_SA_EMAIL="${JOBS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
CREDENTIALS_SECRET_NAME="esi-credentials"

EVE_SSO_CLIENT_ID="${EVE_SSO_CLIENT_ID:?Set EVE_SSO_CLIENT_ID}"
EVE_SSO_CLIENT_SECRET="${EVE_SSO_CLIENT_SECRET:?Set EVE_SSO_CLIENT_SECRET}"
CJ_STRUCTURE_ID="${CJ_STRUCTURE_ID:?Set CJ_STRUCTURE_ID (C-J Keepstar structure_id)}"
CJ_CREDENTIALS_SECRET_NAME="${CJ_CREDENTIALS_SECRET_NAME:-$CREDENTIALS_SECRET_NAME}"
CJ_CATEGORY_IDS="${CJ_CATEGORY_IDS:-7}"   # 7 = Module (incl. rigs). Must match config.CJ_CATEGORY_IDS
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "== 1: BigQuery tables =="
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" < "$REPO_ROOT/bigquery/schema_cj.sql"

echo "== 2: build + push esi-jobs image (now includes job_cj.js) =="
JOBS_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/esi-jobs:latest"
gcloud builds submit "$REPO_ROOT/esi-jobs" --tag "$JOBS_IMAGE" --project="$PROJECT_ID"

# "^|^" = gcloud alternate list delimiter, so a comma inside CJ_CATEGORY_IDS ("7,8") survives.
BASE_ENV="^|^EVE_SSO_CLIENT_ID=${EVE_SSO_CLIENT_ID}|EVE_SSO_CLIENT_SECRET=${EVE_SSO_CLIENT_SECRET}|GCP_PROJECT_ID=${PROJECT_ID}|BQ_DATASET=${BQ_DATASET}|CREDENTIALS_SECRET_NAME=${CREDENTIALS_SECRET_NAME}|CJ_STRUCTURE_ID=${CJ_STRUCTURE_ID}|CJ_CREDENTIALS_SECRET_NAME=${CJ_CREDENTIALS_SECRET_NAME}|CJ_CATEGORY_IDS=${CJ_CATEGORY_IDS}|CJ_SYSTEM_NAME=${CJ_SYSTEM_NAME:-C-J6MT}"

deploy_job () {
  local name="$1" mode="$2" timeout="$3"
  local env="${BASE_ENV}|CJ_MODE=${mode}"
  gcloud run jobs create "$name" \
    --image="$JOBS_IMAGE" --command="node" --args="job_cj.js" \
    --region="$REGION" --project="$PROJECT_ID" \
    --service-account="$JOBS_SA_EMAIL" --set-env-vars="$env" \
    --memory=1Gi --max-retries=1 --task-timeout="$timeout" \
  || gcloud run jobs update "$name" \
    --image="$JOBS_IMAGE" --command="node" --args="job_cj.js" \
    --region="$REGION" --project="$PROJECT_ID" \
    --service-account="$JOBS_SA_EMAIL" --set-env-vars="$env" \
    --memory=1Gi --max-retries=1 --task-timeout="$timeout"
}

echo "== 3: Cloud Run jobs =="
deploy_job esi-cj-poller   full        3600
deploy_job esi-cj-snapshot orders_only 600

if [ "${ENABLE_HOURLY:-0}" = "1" ]; then
  echo "== 4: hourly Cloud Scheduler trigger for esi-cj-snapshot =="
  gcloud services enable cloudscheduler.googleapis.com --project="$PROJECT_ID"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${JOBS_SA_EMAIL}" --role="roles/run.invoker" > /dev/null
  URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/esi-cj-snapshot:run"
  gcloud scheduler jobs create http esi-cj-snapshot-hourly \
    --location="$REGION" --project="$PROJECT_ID" --schedule="7 * * * *" \
    --uri="$URI" --http-method=POST --oauth-service-account-email="$JOBS_SA_EMAIL" \
  || gcloud scheduler jobs update http esi-cj-snapshot-hourly \
    --location="$REGION" --project="$PROJECT_ID" --schedule="7 * * * *" \
    --uri="$URI" --http-method=POST --oauth-service-account-email="$JOBS_SA_EMAIL"
fi

if [ "${REDEPLOY_OAUTH:-0}" = "1" ]; then
  echo "== 5: rebuild esi-oauth-service (adds /login?target=cj) =="
  LOGIN_KEY="${LOGIN_KEY:?Set LOGIN_KEY (same value as in deploy_esi.sh)}"
  OAUTH_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/esi-oauth-service:latest"
  gcloud builds submit "$REPO_ROOT/esi-oauth-service" --tag "$OAUTH_IMAGE" --project="$PROJECT_ID"
  gcloud run deploy esi-oauth-service --image="$OAUTH_IMAGE" --region="$REGION" --project="$PROJECT_ID"
  SERVICE_URL=$(gcloud run services describe esi-oauth-service --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')
  echo "Log in with the C-J character:  ${SERVICE_URL}/login?key=${LOGIN_KEY}&target=cj"
  echo "Then re-run this script with CJ_CREDENTIALS_SECRET_NAME=esi-credentials-cj."
fi

echo ""
echo "Done. Test: gcloud run jobs execute esi-cj-poller --region=$REGION --project=$PROJECT_ID --wait"
echo "Daily:      python run_eval.py && python run_cj.py"
