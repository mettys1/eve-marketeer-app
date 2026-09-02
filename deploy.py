"""
Deploy / redeploy eve-jita-poller to Cloud Run.

Added 2026-09-02 to replace deploy.sh, specifically to kill the multi-machine
workflow that ate most of a day (2026-09-01/02): deploy.sh needs bash, so it
got run in Cloud Shell — a THIRD, disconnected clone of this repo, separate
from the Windows clone Claude edits and separate from wherever run_eval.py
ran. Result: code changes got committed on Windows but never pushed, or
pushed but never pulled where deploy.sh actually ran, and the Cloud Run Job
kept running old code for hours while every symptom looked like a fresh bug.
See docs/eve-jita-scanner-ops.md for the full postmortem if it happens again.

This is plain Python (same `gcloud`-via-subprocess approach eval/refresh.py
already uses successfully on Windows, including the same shell=True fix for
gcloud.cmd) so it runs from the exact same place as run_eval.py — one
machine, one clone, one terminal, no git push/pull round-trip through a
second environment required just to redeploy.

Usage:
    python deploy.py            # redeploy only (the thing you run after
                                 # every poller.js change): build+push image,
                                 # update the Cloud Run Job, test-execute it
                                 # once. This is what deploy.sh's Steps 7-10
                                 # did.
    python deploy.py --full     # ALSO (re)run the one-time GCP account setup
                                 # (APIs, service accounts, IAM, Artifact
                                 # Registry repo, BigQuery schema, Cloud
                                 # Scheduler) — deploy.sh's Steps 1-6 + 11-12.
                                 # Safe to re-run (idempotent, same as
                                 # deploy.sh's `|| echo already exists`
                                 # guards) — only needed for a fresh GCP
                                 # project or if you suspect something in
                                 # that one-time setup got missed/reset.

Prereqs: gcloud CLI installed and authenticated (`gcloud auth login`) with
access to config.PROJECT_ID — the same requirement eval/refresh.py already
has for `gcloud run jobs execute`. If `python run_eval.py` already works
from here, this will too.
"""

import argparse
import platform
import subprocess
import sys
import time
from pathlib import Path

import config

REGION = config.GCLOUD_REGION
PROJECT_ID = config.PROJECT_ID
DATASET = config.DATASET

AR_REPO = "eve-jita-poller"
JOB_NAME = "eve-jita-poller"
POLLER_SA = "eve-jita-poller-sa"
SCHEDULER_SA = "eve-jita-scheduler-sa"
SCHEDULER_JOB = "eve-jita-poller-trigger"
SCHEDULE_CRON = "17 6 * * *"  # daily 06:17 UTC — matches deploy.sh's original schedule
BILLING_ACCOUNT_ID = "012A7C-D17981-530577"  # Matej's "Matej" billing account, fixed

REPO_ROOT = Path(__file__).parent
POLLER_DIR = REPO_ROOT / "poller"

# Same Windows gcloud.cmd fix as eval/refresh.py: subprocess.run(shell=False)
# (the default) can't resolve gcloud.cmd via PATHEXT on Windows.
_USE_SHELL = platform.system() == "Windows"


def run(cmd, check=False):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, shell=_USE_SHELL)
    if check and result.returncode != 0:
        print(f"\nCommand failed (exit {result.returncode}) — stopping.")
        sys.exit(result.returncode)
    return result


def run_idempotent(cmd):
    """For steps that fail harmlessly if already done (deploy.sh's `|| echo
    already exists` pattern) — print a note and continue either way."""
    result = run(cmd)
    if result.returncode != 0:
        print("  (nonzero exit — assuming 'already exists/already done', continuing)")
    return result


def redeploy():
    """The part you actually run every time poller.js changes."""
    image = f"{REGION}-docker.pkg.dev/{PROJECT_ID}/{AR_REPO}/poller:latest"

    print("== Build + push the poller image via Cloud Build ==")
    run(["gcloud", "builds", "submit", str(POLLER_DIR), f"--tag={image}"], check=True)

    poller_sa_email = f"{POLLER_SA}@{PROJECT_ID}.iam.gserviceaccount.com"
    # Keep in sync with config.py's TOP_N_ITEMS/coverage comments if these change.
    job_env_vars = (
        f"GCP_PROJECT_ID={PROJECT_ID},BQ_DATASET={DATASET},ITEM_MODE=top_volume,"
        f"TOP_N_ITEMS=4000,HISTORY_DAYS=14,WRITE_RAW_ORDERS=true,RANK_CONCURRENCY=10,"
        f"SCAN_CONCURRENCY=6,NODE_OPTIONS=--max-old-space-size=2560"
    )
    job_common_args = [
        f"--image={image}",
        f"--region={REGION}",
        f"--service-account={poller_sa_email}",
        f"--set-env-vars={job_env_vars}",
        "--max-retries=1",
        "--task-timeout=3600",
        "--memory=3Gi",
        "--cpu=1",
    ]

    print("== Update the Cloud Run Job (create it instead if this is the first deploy) ==")
    update = run(["gcloud", "run", "jobs", "update", JOB_NAME, *job_common_args])
    if update.returncode != 0:
        print("  update failed (job probably doesn't exist yet) — creating...")
        run(["gcloud", "run", "jobs", "create", JOB_NAME, *job_common_args], check=True)

    print("== Test-run it once, watch it go ==")
    run(["gcloud", "run", "jobs", "execute", JOB_NAME, f"--region={REGION}", "--wait"], check=True)

    print("\nRedeploy done. Now run: python run_eval.py")


def full_setup():
    """One-time GCP account setup — deploy.sh's Steps 1-6 + 11-12. Idempotent,
    safe to re-run, but normally not needed (already done for this project)."""
    print("== Create project (skip if it already exists) ==")
    run_idempotent(["gcloud", "projects", "create", PROJECT_ID, "--name=EVE Jita Scanner"])

    print("== Link billing ==")
    run(["gcloud", "billing", "projects", "link", PROJECT_ID, f"--billing-account={BILLING_ACCOUNT_ID}"], check=True)

    print("== Set active project ==")
    run(["gcloud", "config", "set", "project", PROJECT_ID], check=True)

    print("== Enable required APIs ==")
    run([
        "gcloud", "services", "enable",
        "run.googleapis.com", "cloudscheduler.googleapis.com", "bigquery.googleapis.com",
        "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "iam.googleapis.com",
    ], check=True)

    print("== Provision Cloud Build's service agent + grant it roles ==")
    run_idempotent(["gcloud", "beta", "services", "identity", "create",
                     "--service=cloudbuild.googleapis.com", f"--project={PROJECT_ID}"])
    proj_num = subprocess.run(
        ["gcloud", "projects", "describe", PROJECT_ID, "--format=value(projectNumber)"],
        shell=_USE_SHELL, capture_output=True, text=True,
    ).stdout.strip()
    run(["gcloud", "projects", "add-iam-policy-binding", PROJECT_ID,
         f"--member=serviceAccount:{proj_num}@cloudbuild.gserviceaccount.com",
         "--role=roles/cloudbuild.builds.builder"], check=True)
    run(["gcloud", "projects", "add-iam-policy-binding", PROJECT_ID,
         f"--member=serviceAccount:{proj_num}-compute@developer.gserviceaccount.com",
         "--role=roles/editor"], check=True)
    print("Waiting ~60s for IAM propagation...")
    time.sleep(60)

    print("== Create BigQuery dataset + tables ==")
    schema_path = REPO_ROOT / "bigquery" / "schema.sql"
    with open(schema_path) as f:
        subprocess.run(["bq", "query", "--use_legacy_sql=false", f"--project_id={PROJECT_ID}"],
                        shell=_USE_SHELL, stdin=f, check=True)

    print("== Create Artifact Registry repo ==")
    run_idempotent(["gcloud", "artifacts", "repositories", "create", AR_REPO,
                     "--repository-format=docker", f"--location={REGION}",
                     "--description=eve-jita-poller container images"])

    print("== Service account for the job (BigQuery write access) ==")
    run_idempotent(["gcloud", "iam", "service-accounts", "create", POLLER_SA,
                     "--display-name=EVE Jita Poller"])
    poller_sa_email = f"{POLLER_SA}@{PROJECT_ID}.iam.gserviceaccount.com"
    run(["gcloud", "projects", "add-iam-policy-binding", PROJECT_ID,
         f"--member=serviceAccount:{poller_sa_email}", "--role=roles/bigquery.dataEditor"], check=True)
    run(["gcloud", "projects", "add-iam-policy-binding", PROJECT_ID,
         f"--member=serviceAccount:{poller_sa_email}", "--role=roles/bigquery.jobUser"], check=True)

    print("== Service account for Scheduler to invoke the job ==")
    run_idempotent(["gcloud", "iam", "service-accounts", "create", SCHEDULER_SA,
                     "--display-name=EVE Jita Scheduler"])
    scheduler_sa_email = f"{SCHEDULER_SA}@{PROJECT_ID}.iam.gserviceaccount.com"
    run(["gcloud", "run", "jobs", "add-iam-policy-binding", JOB_NAME, f"--region={REGION}",
         f"--member=serviceAccount:{scheduler_sa_email}", "--role=roles/run.invoker"], check=True)

    print("== Create the Cloud Scheduler trigger ==")
    run_idempotent([
        "gcloud", "scheduler", "jobs", "create", "http", SCHEDULER_JOB,
        f"--location={REGION}", f"--schedule={SCHEDULE_CRON}",
        f"--uri=https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{PROJECT_ID}/jobs/{JOB_NAME}:run",
        "--http-method=POST", f"--oauth-service-account-email={scheduler_sa_email}",
    ])
    print("\nFull setup done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                         help="Also run the one-time GCP account setup steps (idempotent).")
    args = parser.parse_args()

    if args.full:
        full_setup()
    redeploy()


if __name__ == "__main__":
    main()
