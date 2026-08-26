-- eve_jita_scanner BigQuery schema
-- Run once via deploy.sh (uses `bq query`). Safe to re-run — uses CREATE TABLE IF NOT EXISTS.

CREATE SCHEMA IF NOT EXISTS `eve_jita_scanner`
OPTIONS (location = 'US');

-- One row per (scan, item): the computed aggregate — this is what the dashboard queries most.
-- Partitioned by scan date, clustered by type_id for cheap "history of this one item" queries.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.market_snapshots` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL, -- partition column, derived from scanned_at at insert time
  type_id INT64 NOT NULL,
  item_name STRING NOT NULL,
  region_buy_orders INT64,
  region_sell_orders INT64,
  region_buy_volume INT64,
  region_sell_volume INT64,
  region_buy_avg5 FLOAT64,
  region_sell_avg5 FLOAT64,
  region_margin_pct FLOAT64,
  station_buy_orders INT64,
  station_sell_orders INT64,
  station_buy_volume INT64,
  station_sell_volume INT64,
  station_buy_avg5 FLOAT64,
  station_sell_avg5 FLOAT64,
  station_margin_pct FLOAT64,
  avg_daily_volume_14d FLOAT64,
  error STRING -- non-null if this item's fetch failed that run; other fields will be null
)
PARTITION BY scan_date
CLUSTER BY type_id;

-- Full raw order book per scan, for queue-depth analysis (this is the "Buy a Sell Ordery" ask —
-- lets you answer "how much volume sat ahead of my price, and how has that changed over time").
-- This table can get big fast (thousands of rows per scan × every scan). Partition expiration
-- keeps cost bounded — default 90 days below, change `partition_expiration_days` to taste, or
-- remove the OPTIONS line entirely to keep everything forever.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.market_orders_raw` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL,
  type_id INT64 NOT NULL,
  item_name STRING NOT NULL,
  order_id INT64,
  is_buy_order BOOL,
  price FLOAT64,
  volume_remain INT64,
  volume_total INT64,
  location_id INT64,
  system_id INT64,
  min_volume INT64,
  duration INT64,
  issued TIMESTAMP
)
PARTITION BY scan_date
CLUSTER BY type_id, is_buy_order
OPTIONS (
  partition_expiration_days = 90
);

-- ESI market history (daily region-wide volume/price), replaced wholesale each run since ESI
-- always returns the full ~1-year window anyway — no point accumulating duplicates.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.market_history` (
  type_id INT64 NOT NULL,
  item_name STRING NOT NULL,
  history_date DATE NOT NULL,
  average FLOAT64,
  highest FLOAT64,
  lowest FLOAT64,
  order_count INT64,
  volume INT64,
  fetched_at TIMESTAMP NOT NULL
)
PARTITION BY history_date
CLUSTER BY type_id;

-- Matej's own open orders, pulled from ESI via esi-auth/fetch_my_orders.js (run locally,
-- not from Cloud Run — see esi-auth/README.md). One row per (pull, order) — same order_id
-- reappears across pulls as its price/volume_remain change over time, so this doubles as a
-- history of how each order actually filled/got repriced, not just a snapshot.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.my_orders` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL,
  order_id INT64 NOT NULL,
  type_id INT64 NOT NULL,
  item_name STRING,
  is_buy_order BOOL,
  price FLOAT64,
  volume_remain INT64,
  volume_total INT64,
  location_id INT64,
  location_name STRING,
  region_id INT64,
  `range` STRING,
  min_volume INT64,
  duration INT64,
  issued TIMESTAMP
)
PARTITION BY scan_date
CLUSTER BY type_id;

-- Full order book of the Perimeter citadel ("0.0% Neutral States Market HQ", structure_id
-- 1044752365771), pulled from ESI via esi-auth/fetch_perimeter_market.js (run locally).
-- Same shape/purpose as market_orders_raw above, but for a player-owned structure, which
-- (unlike Jita 4-4) needs an authenticated per-structure call — see esi-auth/README.md.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.perimeter_orders_raw` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL,
  type_id INT64 NOT NULL,
  item_name STRING,
  order_id INT64,
  is_buy_order BOOL,
  price FLOAT64,
  volume_remain INT64,
  volume_total INT64,
  min_volume INT64,
  `range` STRING,
  duration INT64,
  issued TIMESTAMP
)
PARTITION BY scan_date
CLUSTER BY type_id, is_buy_order
OPTIONS (
  partition_expiration_days = 90
);
