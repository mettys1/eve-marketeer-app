"""
Central config for eve-marketeer-app.

Every threshold here was an explicit decision made with Matej — do not change
a value without re-confirming, and do not let any module hardcode a number
that belongs here instead.
"""

from pathlib import Path

# --- BigQuery -----------------------------------------------------------
# TODO(Matej): confirm these against your actual project/dataset — copied
# from the eve-jita-own-infra skill notes, not re-verified in this session.
PROJECT_ID = "eve-jita-scanner-21359"
DATASET = "eve_jita_scanner"

TABLE_MARKET_SNAPSHOTS = f"{PROJECT_ID}.{DATASET}.market_snapshots"
TABLE_MARKET_ORDERS_RAW = f"{PROJECT_ID}.{DATASET}.market_orders_raw"
TABLE_PERIMETER_ORDERS_RAW = f"{PROJECT_ID}.{DATASET}.perimeter_orders_raw"
TABLE_MY_ORDERS = f"{PROJECT_ID}.{DATASET}.my_orders"
TABLE_WALLET_TRANSACTIONS = f"{PROJECT_ID}.{DATASET}.wallet_transactions"
TABLE_WALLET_JOURNAL = f"{PROJECT_ID}.{DATASET}.wallet_journal"
TABLE_ML_FEATURES = f"{PROJECT_ID}.{DATASET}.ml_features"  # created by features.py if missing
# NOTE: there is no net_worth_history table (confirmed against live BigQuery
# 2026-09-01, `bq ls` — not just schema.sql) and nothing writes one. kpi.py /
# sizing.py compute cash + capital directly from wallet_journal / my_orders
# instead, reusing the same SQL already proven in esi-oauth-service's
# /report endpoint (wallet_capital, trading_pnl_daily reports).

# --- Existing eve-trading pipeline (Cloud Run Jobs) ----------------------
# CONFIRMED 2026-09-01: there is no local `daily_ops.js` — it never existed.
# The refresh step is 4 independent Cloud Run Jobs (source: refresh_all.sh,
# refresh_jita.sh, refresh_my_orders.sh, refresh_perimeter.sh,
# refresh_wallet.sh, all already in THIS repo's root). refresh.py invokes
# them the same way those scripts do: `gcloud run jobs execute <job>
# --region=... --project=... --wait`, in parallel, exactly like
# refresh_all.sh.
GCLOUD_REGION = "europe-west1"
CLOUD_RUN_JOBS = [
    "eve-jita-poller",        # Jita market scan -> market_snapshots / market_orders_raw
    "esi-perimeter-poller",   # Perimeter citadel scan -> perimeter_orders_raw
    "esi-my-orders-poller",   # Matej's open orders (ESI, needs esi-oauth-service login) -> my_orders
    "esi-wallet-poller",      # wallet transactions + journal (ESI) -> wallet_transactions / wallet_journal
]

# How old MAX(scanned_at) is allowed to be before we trust it as "fresh"
# without re-running the refresh. Matches the freshness rule in the skill.
FRESHNESS_THRESHOLD_HOURS = 2

# --- Fees (from Matej's actual buy/sell screenshots — re-confirm if rates change) ---
BROKER_FEE_RATE = 0.01382
SALES_TAX_RATE = 0.03375

# --- Reference pricing (confirmed 2026-09-01) -----------------------------
# Reference buy price = MAX(best buy across the WHOLE Jita solar system, best
# buy in the Perimeter citadel) — explicitly NOT limited to a single
# station/structure. "Jita" here means the full system (system_id below),
# not just the Jita IV - Moon 4 NPC station recompute_top_of_book.sql
# restricts to — Matej confirmed 2026-09-01 that station-only was wrong for
# this app. Reference sell price = best sell in the Jita system only (that's
# where Matej actually lists sells).
JITA_SYSTEM_ID = 30000142  # EVE static data: "Jita" solar system

# --- Step 2: evaluate open buy orders ------------------------------------
MARGIN_FLOOR_PCT = 8.0          # below this -> CANCEL instead of REPRICE
REPRICE_TICK = 0.01             # ISK increment placed above buy.max

# --- Step 3: new buy order sizing ("first in line") ----------------------
HARD_UNIT_CAP = 60
PER_POSITION_PCT = 0.20
CAPITAL_RESERVE_PCT = 0.01      # kept aside, never spent on new buy orders
DEPTH_CAP_FRACTION = 0.15       # of min(buy.volume, sell.volume)

# Density risk bands (density * 1000, i.e. "per 1000 units of volume")
DENSITY_LOW_MAX = 0.2
DENSITY_MEDIUM_MAX = 2.0
# > DENSITY_MEDIUM_MAX -> high/very high risk

# --- Dashboard ------------------------------------------------------------
DASHBOARD_DEFAULT_WINDOW_DAYS = 30
DASHBOARD_OUTPUT_DIR = Path(__file__).parent / "output"
