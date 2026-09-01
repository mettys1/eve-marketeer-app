"""
Thin BigQuery helper. Every module goes through here so freshness-checking
stays consistent — never query market_snapshots directly from another file.
"""

from datetime import datetime, timezone
from google.cloud import bigquery

import config


def get_client() -> bigquery.Client:
    return bigquery.Client(project=config.PROJECT_ID)


def last_scan_time(client: bigquery.Client) -> datetime:
    query = f"SELECT MAX(scanned_at) AS max_scanned_at FROM `{config.TABLE_MARKET_SNAPSHOTS}`"
    row = next(iter(client.query(query).result()))
    return row.max_scanned_at


def freshness_hours(client: bigquery.Client) -> float:
    ts = last_scan_time(client)
    if ts is None:
        return float("inf")
    now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def assert_fresh_or_raise(client: bigquery.Client) -> None:
    """
    Step 0 rule from eve-jita-own-infra: never present anything as current
    without checking freshness first. Called at the start of every step
    after refresh — if this raises, run_eval.py should stop, not fall back
    to a stale view.
    """
    hours = freshness_hours(client)
    if hours > config.FRESHNESS_THRESHOLD_HOURS:
        raise RuntimeError(
            f"market_snapshots is {hours:.1f}h old (threshold "
            f"{config.FRESHNESS_THRESHOLD_HOURS}h) — refresh did not "
            f"produce fresh data. Not proceeding with stale numbers."
        )


def query_df(client: bigquery.Client, sql: str):
    """Run a query and return a pandas DataFrame."""
    return client.query(sql).to_dataframe()
