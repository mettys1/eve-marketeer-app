"""
run_cj.py — C-J6MT import pipeline. Run AFTER run_eval.py:

    python run_eval.py && python run_cj.py

Steps:
  1. gcloud run jobs execute esi-cj-poller --wait   (skip with --skip-job)
     -> C-J order book, type info, Jita books for missing types, Delve history
  2. freshness check on the latest cj_runs row (refuses stale data)
  3. cj/analysis.py  -> Jita price, C-J market, spread, m3 shipping, quantity
  4. cj/dashboard.py -> output/cj_YYYYMMDD_HHMM.html (+ Jita multibuy list)

Read-only like run_eval.py: nothing is placed/cancelled in-game.
"""

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

import config
from eval import bq, refresh
from cj import analysis, dashboard


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-job", action="store_true", help="don't run esi-cj-poller, use the latest data")
    ap.add_argument("--allow-stale", action="store_true", help="render even if the latest C-J run is old")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if config.CJ_STRUCTURE_ID is None:
        print("config.CJ_STRUCTURE_ID není nastaveno — viz docs/cj-pipeline.md.", file=sys.stderr)
        return 1

    client = bq.get_client()

    if not args.skip_job:
        print(f"[1/4] {config.CJ_CLOUD_RUN_JOB} (C-J market, Jita books, Delve history)...")
        proc = refresh._execute_job(config.CJ_CLOUD_RUN_JOB)
        out, err = proc.communicate()
        if proc.returncode != 0:
            print(f"JOB FAILED (exit {proc.returncode})\n{out}\n{err}", file=sys.stderr)
            return 1

    print("[2/4] freshness check...")
    run = analysis.load_latest_run(client)
    if run is None:
        print("V cj_runs není žádný 'full' běh pro tuhle strukturu — spusť bez --skip-job.", file=sys.stderr)
        return 1
    run_at = pd.Timestamp(run["run_at"]).to_pydatetime()
    age_h = (datetime.now(timezone.utc) - run_at).total_seconds() / 3600
    if age_h > config.FRESHNESS_THRESHOLD_HOURS and not args.allow_stale:
        print(f"Poslední C-J běh je {age_h:.1f} h starý (limit {config.FRESHNESS_THRESHOLD_HOURS} h). "
              "Nepokračuju se starými čísly (--allow-stale to přebije).", file=sys.stderr)
        return 1

    print("[3/4] analysis...")
    res = analysis.analyze(client, run)
    for w in res.warnings:
        print(f"  ! {w}")
    print(f"  {len(res.recommendations)} doporučení, vyřazeno: {res.rejected_counts}")

    print("[4/4] dashboard...")
    out = dashboard.render(res, open_browser=not args.no_browser)
    print(f"done -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
