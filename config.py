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
# MARGIN_FLOOR_PCT lowered 8.0 -> 4.0 on 2026-09-02, replacing an unfounded
# guess with a number derived from Matej's real wallet journal. Derivation:
#   target net margin we want left over  = 2.0%
#   + buffer for a couple more reprice cycles before this fills
#   ---------------------------------------------------------------
#   -> 4.0% (rounded up from a precise ~2.76%, since Matej's actual reprice
#      deltas are usually small "a few spots down the book" moves, not the
#      full flat-fee assumption the old 8.0% implicitly priced in)
# This is now paired with REPRICE_SCC_*/REPRICE_FLAT_FEE below, which let
# eval/orders.py compute the REAL incremental cost of repricing an existing
# buy order (not the full new-order BROKER_FEE_RATE this constant used to be
# implicitly compared against) — see reprice_margin_pct() in eval/orders.py.
MARGIN_FLOOR_PCT = 4.0           # below this -> CANCEL instead of REPRICE
REPRICE_TICK = 0.01             # ISK increment placed above buy.max

# Real cost of repricing an EXISTING buy order upward at Perimeter (0%
# structure broker fee) — reverse-engineered + verified to the ISK against
# Matej's live wallet journal on 2026-09-02 (3 real reprice events: Federation
# Navy 400mm Steel Plates, Shadow Serpentis Assault Damage Control, Hexite).
# Formula (all three matched exactly):
#   reprice_fee = REPRICE_FLAT_FEE
#               + REPRICE_SCC_RATE_VALUE * (new_price * qty)
#               + REPRICE_SCC_RATE_DELTA * (new_price - placed_price) * qty
# This is a genuinely separate, much smaller cost than a fresh new order
# (BROKER_FEE_RATE above is ~12x REPRICE_SCC_RATE_VALUE) — using the flat
# BROKER_FEE_RATE/SALES_TAX_RATE for an existing-order reprice (as the old
# margin_pct() did) systematically understated real repriced margin, which
# is most of why MARGIN_FLOOR_PCT=8.0 felt too aggressive to Matej. Only
# verified for price INCREASES (delta >= 0) — the only direction a buy
# reprice ever moves (you're chasing the top of book upward).
REPRICE_FLAT_FEE = 100.0         # flat "Brokers Fee" line, per reprice event
REPRICE_SCC_RATE_VALUE = 0.001   # SCC surcharge: 0.10% of new order value
REPRICE_SCC_RATE_DELTA = 0.005   # SCC surcharge: 0.50% of the price-increase (escrow) delta

# Real cost of repricing an EXISTING sell order downward at a Jita NPC
# station (Jita IV - Moon 4) — verified to the ISK against 2 real reprice
# events (Medium Shield Booster II, Vigor Compact Micro Auxiliary Power
# Core) on 2026-09-02: fee = qty * new_price * SELL_REPRICE_RATE, no SCC
# surcharge line at all (that's Perimeter/citadel-specific — this is an NPC
# station). Only verified for price DECREASES, which per Matej is the only
# direction a sell reprice ever happens in practice ("Tohle se ale nikdy
# nestane. Vzdy budeme jen slevnovat."). Not used anywhere yet (step 2 only
# evaluates buy orders — see OPEN_ORDERS_SQL's `is_buy_order` filter) —
# kept here for when sell-order evaluation gets built.
SELL_REPRICE_RATE = 0.0027751

# --- Step 3: new buy order sizing ("first in line") ----------------------
HARD_UNIT_CAP = 60
PER_POSITION_PCT = 0.20
CAPITAL_RESERVE_PCT = 0.01      # kept aside, never spent on new buy orders
DEPTH_CAP_FRACTION = 0.15       # of min(buy.volume, sell.volume)

# Sanity filters for NEW candidates, added 2026-09-01 after a real run
# surfaced 9000%+ "margins" on near-zero-liquidity items — same blunt
# safeguards bigquery/recompute_top_of_book.sql already uses.
MARGIN_CEILING_PCT = 100.0      # above this -> reference-price artifact, not a real trade
MIN_ORDERS_PER_SIDE = 3         # No longer a standalone liquidity gate on its own (see
                                 # MIN_DAILY_PROFIT_TURNOVER below) — 2026-09-02, after Matej
                                 # found 344/344 candidates were cheap niche items (compressed
                                 # gases/ores) that passed this check but aren't really traded.
                                 # Order counts alone prove standing orders exist, not that real
                                 # volume moves. Repurposed same day as the absolute-order-count
                                 # fallback in eval/sizing.compute_risk_band() for low-volume
                                 # items — see DENSITY_MIN_VOLUME_FOR_RATIO below.

# Replaces MIN_ORDERS_PER_SIDE as the liquidity gate for step 3 candidates
# (added 2026-09-02). Real signal = actual realized ESI trade volume
# (avg_daily_volume_14d) weighted by how much profit each unit is worth,
# not just "are there >=3 standing orders" (which a stale/parked order
# satisfies without any real trading happening). Filter:
#   avg_daily_volume_14d * profit_per_unit >= MIN_DAILY_PROFIT_TURNOVER
# STARTER GUESS, NOT CALIBRATED — the diagnostic query to size this against
# real candidate data was never run. Treat the first real dashboard output
# after this ships as the calibration run, and adjust with Matej from there.
MIN_DAILY_PROFIT_TURNOVER = 50_000.0   # ISK/day of profit turnover, floor

# --- Step 3b: "first mover" candidates (no existing buy order) -----------
# Added 2026-09-02. CANDIDATES_SQL requires an existing buy order (jita or
# perimeter) to compute a reference buy price — this silently drops items
# that ARE traded (real sell-side volume) but have zero standing buy orders
# right now. Per Matej, that's actually the ideal case for a "first in
# line" strategy: no competition to outbid. Confirmed 2026-09-02:
# "jakou cenu nabídnout, když neexistuje žádná konkurenční buy objednávka?
# Malou, skoro nulovou. Spousta lidi nekontroluje buy ordery a jen to
# proda." -> price = sell_min * NO_COMPETITION_BUY_PRICE_PCT.
# Starting value confirmed by Matej ("Asi otestujeme. Zacni 10") — treat
# as a first test value, not a final calibrated number.
NO_COMPETITION_BUY_PRICE_PCT = 0.10

# Reserved slice of available_capital set aside for first-mover candidates,
# kept separate from the main ranked walk in rank_new_candidates(). Needed
# because first-mover profit_per_unit is artificially huge (buy priced at
# 10% of sell) — without a separate cap, these rows would sort to the top
# by profit_per_unit and could consume the entire day's budget before any
# normal candidate gets a look in. STARTER GUESS — flag for Matej to
# confirm/adjust, same as MIN_DAILY_PROFIT_TURNOVER above.
FIRST_MOVER_BUDGET_PCT = 0.15

# Reserved slice of main_budget guaranteed to the "levné" price tier via its
# own walk in eval/sizing.rank_new_candidates(), run BEFORE the general walk
# over everything else — added 2026-09-02, same day risk_band stopped being
# a hard filter (see DENSITY_MIN_VOLUME_FOR_RATIO below). Once "střední"/
# "drahé" candidates could appear, a real run showed "levné" candidates
# collapse from ~250 to 30: sorting by absolute profit_per_unit means a
# handful of expensive positions (each near the 20%-of-budget
# PER_POSITION_PCT cap) ate almost the whole budget before the walk got
# deep into the individually-cheap levné rows. Matej confirmed 2026-09-02
# the tier split already makes this fine to browse, but wanted more than 30
# absolute candidates. STARTER GUESS, not calibrated — adjust after seeing
# how many levné/střední/drahé candidates a real run produces with this.
LEVNE_RESERVED_BUDGET_PCT = 0.40

# Density risk bands (density * 1000, i.e. "per 1000 units of volume").
# DROPPED as a hard filter in eval/sizing.rank_new_candidates() /
# rank_first_mover_candidates() on 2026-09-02 — confirmed with Matej after
# eval/debug_candidates.py showed the ratio was simply too strict for
# anything but very-high-volume cheap items: a real run had 486/486
# "střední" and 641/641 "drahé" candidates at risk_band="high", and even
# after adding the DENSITY_MIN_VOLUME_FOR_RATIO fallback below, everything
# priced between 10M and 1.6B ISK was STILL empty (a genuine dead zone, not
# just the near-zero-volume edge case the fallback targeted).
# MIN_DAILY_PROFIT_TURNOVER above is the real liquidity gate now — it's a
# strictly better signal anyway (weighted by real profit, not raw order
# count). risk_band/density_per_1000 are still computed and shown on the
# dashboard as an informational column via eval.sizing.compute_risk_band(),
# just no longer block a candidate from appearing.
DENSITY_LOW_MAX = 0.2
DENSITY_MEDIUM_MAX = 2.0
# > DENSITY_MEDIUM_MAX -> high/very high risk (label only, not a filter)

# Below this avg_daily_volume_14d (units/day), compute_risk_band() falls
# back to the absolute MIN_ORDERS_PER_SIDE floor instead of the ratio above
# (dividing by a near-zero volume explodes the ratio into the thousands for
# ANY normal order count) — still only affects the risk_band LABEL, not
# filtering, per the note above.
DENSITY_MIN_VOLUME_FOR_RATIO = 10.0

# Price tiers for candidates — DISPLAY ONLY (added 2026-09-02). Doesn't touch
# ranking, filtering, or sizing at all — sizing.rank_new_candidates() still
# ranks/budgets exactly as before; this just labels each row so the
# dashboard can show a mix across price ranges instead of one flat list,
# which Matej found dominated by cheap items on 2026-09-01 (± items that
# turned out not to be commonly traded — a separate, still-open concern
# about TOP_N_ITEMS=4000 pulling in thin/illiquid items, not a tiering
# problem). Boundaries are round-number guesses, not derived from anything —
# adjust if they don't carve up a real candidate list sensibly.
PRICE_TIER_CHEAP_MAX = 1_000_000      # < this -> "levné"
PRICE_TIER_MID_MAX = 10_000_000       # < this (and >= cheap max) -> "střední"
# >= PRICE_TIER_MID_MAX -> "drahé"

# --- Dashboard ------------------------------------------------------------
DASHBOARD_DEFAULT_WINDOW_DAYS = 30
DASHBOARD_OUTPUT_DIR = Path(__file__).parent / "output"
