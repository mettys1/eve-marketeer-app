"""
Logs one row per (scan_date, type_id) to ml_features, purely for future use.
Nothing in run_eval.py reads this table back — Matej explicitly deferred ML
until he knows the market better. This just makes sure history is there
when he's ready, instead of having to backfill later.

Row = density snapshot at evaluation time + (once known) whether that item's
order filled or had to be repriced/cancelled — the label for a future model.
Written append-only; never read here.

Fixed 2026-09-01: `insert_rows_json` needs plain JSON-serializable types —
values pulled off a pandas itertuples() row (type_id, margin_pct,
density_per_1000, reprice_cost_so_far) come back as numpy.int64/float64,
which json.dumps chokes on ("Object of type int64 is not JSON
serializable"). Every value below is now cast to a native Python
int/float/None before being handed to the client.
"""

from datetime import date

import pandas as pd
from google.cloud import bigquery

import config

SCHEMA = [
    bigquery.SchemaField("scan_date", "DATE"),
    bigquery.SchemaField("type_id", "INTEGER"),
    bigquery.SchemaField("item_name", "STRING"),
    bigquery.SchemaField("density_per_1000", "FLOAT"),
    bigquery.SchemaField("risk_band", "STRING"),
    bigquery.SchemaField("margin_pct", "FLOAT"),
    bigquery.SchemaField("action", "STRING"),          # REPRICE / CANCEL / NEW / HOLD
    bigquery.SchemaField("reprice_cost_so_far", "FLOAT"),
    bigquery.SchemaField("filled", "BOOLEAN"),          # nullable — unknown until later reconciled
]


def _int(v):
    return None if v is None or pd.isna(v) else int(v)


def _float(v):
    return None if v is None or pd.isna(v) else float(v)


def ensure_table(client) -> None:
    table_ref = bigquery.TableReference.from_string(config.TABLE_ML_FEATURES)
    try:
        client.get_table(table_ref)
    except Exception:
        table = bigquery.Table(table_ref, schema=SCHEMA)
        client.create_table(table)
        print(f"[features] created {config.TABLE_ML_FEATURES}")


def log_features(client, orders_eval: pd.DataFrame, candidates: pd.DataFrame) -> None:
    ensure_table(client)
    rows = []
    today = date.today().isoformat()

    for row in orders_eval.itertuples():
        rows.append({
            "scan_date": today,
            "type_id": _int(row.type_id),
            "item_name": row.item_name,
            "density_per_1000": None,  # not computed for existing orders in step 2
            "risk_band": None,
            "margin_pct": None,
            "action": row.action,
            "reprice_cost_so_far": _float(getattr(row, "reprice_cost_so_far", None)),
            "filled": None,
        })

    for row in candidates.itertuples():
        rows.append({
            "scan_date": today,
            "type_id": _int(row.type_id),
            "item_name": row.item_name,
            "density_per_1000": _float(row.density_per_1000),
            "risk_band": row.risk_band,
            "margin_pct": _float(row.margin_pct),
            "action": "NEW",
            "reprice_cost_so_far": None,
            "filled": None,
        })

    if not rows:
        return

    errors = client.insert_rows_json(config.TABLE_ML_FEATURES, rows)
    if errors:
        print(f"[features] insert errors (non-fatal, logging only): {errors}")
    else:
        print(f"[features] logged {len(rows)} rows for {today}")
