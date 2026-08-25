#!/usr/bin/env bash
#
# eve-jita-poller — GCP deploy script.
#
# I (Claude) can't run this — no live GCP access from where I work. Run it yourself, step by
# step (it's split into numbered sections on purpose — go through it a section at a time the
# first run, don't just blindly `bash deploy.sh` end to end, since step 0 needs a manual
# console action first). Paste me any error output and I'll fix the script.
#
# Prereqs: gcloud CLI installed and `gcloud auth login` already done.
#          https://cloud.google.com/sdk/docs/install if you don't have it.

set -euo pipefail

# ---- Config — edit these ----
PROJECT_ID="eve-jita-scanner-21359"   # fixed to the project you already created — don't randomize
                                       # this again, or every retry spawns a brand-new GCP project
REGION="europe-west1"                    # pick whatever's closest to you
DATASET="eve_jita_scanner"
AR_REPO="eve-jita-poller"
JOB_NAME="eve-jita-poller"
POLLER_SA="eve-jita-poller-sa"
SCHEDULER_SA="eve-jita-scheduler-sa"
SCHEDULER_JOB="eve-jita-poller-trigger"
SCHEDULE_CRON="17 6 * * *"               # daily 06:17 — change as you like (5-field cron, UTC by default; add --time-zone to gcloud scheduler jobs create if you want local time)
SETUP_SCHEDULER="${SETUP_SCHEDULER:-true}"   # set to "false" (env var, not here) to skip Steps 11-12
                                              # and stay manual-only: SETUP_SCHEDULER=false bash deploy.sh
BILLING_ACCOUNT_ID="012A7C-D17981-530577"    # fixed to your "Matej" billing account — no more
                                              # interactive prompt on every re-run

# ============================================================================
# STEP 0 — MANUAL: create the project + enable billing.
# I'm deliberately not scripting this part — billing setup needs your payment details entered
# by you, not automated. Do this in the console first:
#   1. https://console.cloud.google.com/projectcreate  → create a project, note its Project ID
#      (or just let this script create it below with `gcloud projects create`)
#   2. https://console.cloud.google.com/billing → set up a billing account if you don't have one
#   3. Come back here, set PROJECT_ID above to match, then run the rest.
# ============================================================================

echo "== Step 1: create project (skip if you already made one — just fix PROJECT_ID above) =="
gcloud projects create "$PROJECT_ID" --name="EVE Jita Scanner" \
  || echo "Project already exists, continuing..."

echo "== Step 2: link billing =="
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT_ID"

echo "== Step 3: set active project =="
gcloud config set project "$PROJECT_ID"

echo "== Step 4: enable required APIs (takes a minute or two) =="
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com

echo "== Step 4b: make sure Cloud Build's service agent exists and can actually build =="
# On a brand-new project, enabling cloudbuild.googleapis.com doesn't always finish provisioning
# its service agent (or granting the default SAs their roles) fast enough for Step 7's
# 'gcloud builds submit' to succeed right away — it fails with a bare PERMISSION_DENIED.
# Force provisioning + grant the roles new projects no longer get automatically, then give the
# IAM change a few seconds to propagate.
gcloud beta services identity create --service=cloudbuild.googleapis.com --project="$PROJECT_ID" \
  || echo "Service identity already exists, continuing..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" > /dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/editor" > /dev/null
echo "Waiting ~60s for IAM propagation..."
sleep 60

echo "== Step 5: create BigQuery dataset + tables =="
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" < "$(dirname "$0")/bigquery/schema.sql"

echo "== Step 6: create Artifact Registry repo for the container image =="
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="eve-jita-poller container images" \
  || echo "Repo already exists, continuing..."

echo "== Step 7: build + push the poller image via Cloud Build =="
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/poller:latest"
gcloud builds submit "$(dirname "$0")/poller" --tag "$IMAGE"

echo "== Step 8: service account for the job (BigQuery write access) =="
gcloud iam service-accounts create "$POLLER_SA" --display-name="EVE Jita Poller" \
  || echo "Service account already exists, continuing..."
POLLER_SA_EMAIL="${POLLER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${POLLER_SA_EMAIL}" --role="roles/bigquery.dataEditor"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${POLLER_SA_EMAIL}" --role="roles/bigquery.jobUser"

echo "== Step 9: create the Cloud Run Job (or update it, if this is a re-run) =="
# ITEM_MODE=top_volume scans every type_id trading in the region (19k+ — Cloud Run's own first
# run told us), ranks by real volume, keeps the top TOP_N_ITEMS — see poller.js's header comment.
# --memory bumped to 2Gi after the first attempt OOM'd (was caching full history for all 19k
# items during ranking, not just the aggregate — fixed in poller.js, but keeping the extra
# headroom). NODE_OPTIONS caps V8's heap below the container limit, leaving room for non-heap
# overhead (Node itself, native buffers) so we get a clean JS OOM instead of a raw SIGKILL if
# something does grow unexpectedly.
JOB_ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID},BQ_DATASET=${DATASET},ITEM_MODE=top_volume,TOP_N_ITEMS=750,HISTORY_DAYS=14,WRITE_RAW_ORDERS=true,RANK_CONCURRENCY=10,SCAN_CONCURRENCY=6,NODE_OPTIONS=--max-old-space-size=1536"
gcloud run jobs create "$JOB_NAME" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$POLLER_SA_EMAIL" \
  --set-env-vars="$JOB_ENV_VARS" \
  --max-retries=1 \
  --task-timeout=3600 \
  --memory=2Gi \
  --cpu=1 \
  || gcloud run jobs update "$JOB_NAME" \
       --image="$IMAGE" \
       --region="$REGION" \
       --service-account="$POLLER_SA_EMAIL" \
       --set-env-vars="$JOB_ENV_VARS" \
       --max-retries=1 \
       --task-timeout=3600 \
       --memory=2Gi \
       --cpu=1

echo "== Step 10: test-run it once, watch it go =="
gcloud run jobs execute "$JOB_NAME" --region="$REGION" --wait

if [ "$SETUP_SCHEDULER" = "true" ]; then
  echo "== Step 11: service account for Scheduler to invoke the job =="
  gcloud iam service-accounts create "$SCHEDULER_SA" --display-name="EVE Jita Scheduler" \
    || echo "Service account already exists, continuing..."
  SCHEDULER_SA_EMAIL="${SCHEDULER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
    --region="$REGION" \
    --member="serviceAccount:${SCHEDULER_SA_EMAIL}" \
    --role="roles/run.invoker"

  echo "== Step 12: create the Cloud Scheduler trigger =="
  gcloud scheduler jobs create http "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$SCHEDULE_CRON" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run" \
    --http-method=POST \
    --oauth-service-account-email="$SCHEDULER_SA_EMAIL" \
    || echo "Scheduler job already exists, continuing..."
else
  echo "== Steps 11-12 skipped (SETUP_SCHEDULER=false) — no automatic daily run, manual-only for now =="
  echo "When you're ready to automate: bash deploy.sh (SETUP_SCHEDULER defaults to true)"
fi

echo ""
echo "Done. Project: $PROJECT_ID"
echo "Check data landed: bq query --use_legacy_sql=false 'SELECT * FROM \`${PROJECT_ID}.${DATASET}.market_snapshots\` ORDER BY scanned_at DESC LIMIT 10'"
echo "Trigger a run manually any time: gcloud run jobs execute $JOB_NAME --region=$REGION"
echo "Watch scheduled runs: gcloud scheduler jobs describe $SCHEDULER_JOB --location=$REGION"
