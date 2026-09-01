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
TABLE_NET_WORTH_HISTORY = f"{PROJECT_ID}.{DATASET}.net_worth_history"
TABLE_ML_FEATURES = f"{PROJECT_ID}.{DATASET}.ml_features"  # created by features.py if missing

# --- Existing daily_ops.js pipeline --------------------------------------
# TODO(Matej): point this at your real ops/ folder (the one with daily_ops.js
# and package.json). Left relative-looking on purpose so it's obviously a
# placeholder, not a guess at your real path.
OPS_DIR = Path(r"C:\Users\Matej\Documents\GitHub\<eve-trading-repo>\ops")
DAILY_OPS_SCRIPT = "daily_ops.js"

# How old MAX(scanned_at) is allowed to be before we trust it as "fresh"
# without re-running the refresh. Matches the freshness rule in the skill.
FRESHNESS_THRESHOLD_HOURS = 2

# --- Fees (from Matej's actual buy/sell screenshots — re-confirm if rates change) ---
BROKER_FEE_RATE = 0.01382
SALES_TAX_RATE = 0.03375

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
