"""
Step 1 — Refresh.

There is no local `daily_ops.js` (confirmed 2026-09-01 — it never existed;
the repo's own README/refresh_all.sh were the source of truth all along).
The refresh is 4 independent Cloud Run Jobs, invoked with `gcloud run jobs
execute --wait`, in parallel, exactly like this repo's own refresh_all.sh
does. This module deliberately does NOT reimplement scanning/reprice-check/
sell-suggestions/net worth calc — those Cloud Run Jobs already do it and are
the source of truth. This just launches them and proves (via freshness
check against market_snapshots) that they actually produced new data,
instead of silently trusting exit code 0.

Requires the `gcloud` CLI installed and authenticated (`gcloud auth login`)
with access to config.PROJECT_ID — same requirement the existing
refresh_*.sh shell scripts already have.

Fixed 2026-09-01 — Windows: the Cloud SDK installs `gcloud` as `gcloud.cmd`,
a batch file, not a `.exe`. `subprocess.Popen([...], shell=False)` (the
default) calls Windows' CreateProcess directly, which has no notion of
PATHEXT/file-association resolution — it looks for a literal `gcloud`/
`gcloud.exe` and fails with `FileNotFoundError: [WinError 2]` even though
`gcloud version` works fine in the same terminal. `shell=True` routes the
command through `cmd.exe`, which *does* resolve `.cmd` via PATHEXT, same as
typing `gcloud ...` at a prompt. Safe here since every argument is a fixed
string from config, never user input. Linux/Cloud Shell (where `gcloud` is
already a real executable/shell script) is unaffected either way, but this
only enables shell=True on Windows to avoid changing behavior there.
"""

import platform
import subprocess

import config
from eval import bq

_USE_SHELL = platform.system() == "Windows"


def _execute_job(job_name: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "gcloud", "run", "jobs", "execute", job_name,
            f"--region={config.GCLOUD_REGION}",
            f"--project={config.PROJECT_ID}",
            "--wait",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=_USE_SHELL,
    )


def run_refresh(client) -> None:
    before = bq.last_scan_time(client)

    # Launch all 4 jobs in parallel — they're independent, same as
    # refresh_all.sh. Total time = slowest job (usually eve-jita-poller),
    # not the sum of all four.
    procs = {job: _execute_job(job) for job in config.CLOUD_RUN_JOBS}

    failures = []
    for job, proc in procs.items():
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            failures.append(
                f"--- {job} (exit {proc.returncode}) ---\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )

    if failures:
        raise RuntimeError(
            "One or more Cloud Run Jobs failed during refresh:\n\n"
            + "\n\n".join(failures)
        )

    after = bq.last_scan_time(client)
    if after is None or (before is not None and after <= before):
        raise RuntimeError(
            "All refresh jobs exited 0 but MAX(scanned_at) did not advance "
            f"(before={before}, after={after}). Refresh did not actually "
            "write new data — check Cloud Run job logs, don't proceed."
        )

    print(f"[refresh] ok — market_snapshots advanced {before} -> {after}")
