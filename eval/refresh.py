"""
Step 1 — Refresh.

Deliberately does NOT reimplement scanning/reprice-check/sell-suggestions/net
worth calc — daily_ops.js already does all four and is the source of truth.
This just calls it as a subprocess and proves (via freshness check) that it
actually produced new data, instead of silently trusting exit code 0.
"""

import subprocess

import config
from eval import bq


def run_refresh(client) -> None:
    before = bq.last_scan_time(client)

    result = subprocess.run(
        ["node", config.DAILY_OPS_SCRIPT],
        cwd=str(config.OPS_DIR),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"daily_ops.js exited {result.returncode}.\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    after = bq.last_scan_time(client)
    if after is None or (before is not None and after <= before):
        raise RuntimeError(
            "daily_ops.js exited 0 but MAX(scanned_at) did not advance "
            f"(before={before}, after={after}). Refresh did not actually "
            "write new data — check Cloud Run job logs, don't proceed."
        )

    print(f"[refresh] ok — market_snapshots advanced {before} -> {after}")
