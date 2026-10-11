-- C-J6MT import pipeline tables (added 2026-10-11). Run once via deploy_cj.sh.
-- Safe to re-run — CREATE TABLE IF NOT EXISTS.

-- Full C-J Keepstar order book per esi-cj-poller run. Consecutive snapshots are diffed in
-- cj/analysis.py to estimate how many units actually sell in C-J per day (structure
-- markets have no ESI history endpoint, so this is the only C-J-specific demand signal).
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.cj_orders_raw` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL,
  structure_id INT64 NOT NULL,
  type_id INT64 NOT NULL,
  order_id INT64 NOT NULL,
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
OPTIONS (partition_expiration_days = 90);

-- Static type info cache (ESI /universe/types + /groups). packaged_volume drives the
-- m3 shipping formula — modules are shipped packaged.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.type_info` (
  type_id INT64 NOT NULL,
  name STRING,
  group_id INT64,
  category_id INT64,
  volume FLOAT64,
  packaged_volume FLOAT64,
  market_group_id INT64,
  published BOOL,
  fetched_at TIMESTAMP
)
CLUSTER BY type_id;

-- Jita (The Forge) order books fetched by esi-cj-poller for C-J types the main
-- eve-jita-poller scan didn't cover (or for all of them, if the main scan was stale).
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.cj_jita_orders_raw` (
  scanned_at TIMESTAMP NOT NULL,
  scan_date DATE NOT NULL,
  type_id INT64 NOT NULL,
  order_id INT64,
  is_buy_order BOOL,
  price FLOAT64,
  volume_remain INT64,
  location_id INT64,
  system_id INT64,
  min_volume INT64,
  `range` STRING
)
PARTITION BY scan_date
CLUSTER BY type_id, is_buy_order
OPTIONS (partition_expiration_days = 30);

-- Delve region daily-volume summary (fallback demand only — Delve includes 1DQ1-A etc.,
-- so this overstates C-J demand; scaled by config.CJ_DELVE_SHARE in analysis).
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.cj_delve_history` (
  fetched_date DATE NOT NULL,
  type_id INT64 NOT NULL,
  avg_daily_volume FLOAT64,
  avg_price FLOAT64,
  days_with_trades INT64,
  window_days INT64
)
PARTITION BY fetched_date
CLUSTER BY type_id
OPTIONS (partition_expiration_days = 60);

-- One row per esi-cj-poller run — run_cj.py uses the latest 'full' row to know which
-- snapshot / Jita scan belong together and to check freshness.
CREATE TABLE IF NOT EXISTS `eve_jita_scanner.cj_runs` (
  run_at TIMESTAMP NOT NULL,
  structure_id INT64,
  mode STRING,
  cj_orders INT64,
  cj_types INT64,
  selected_types INT64,
  jita_reused_scan_at TIMESTAMP,
  jita_fetched_types INT64,
  delve_fetched_types INT64,
  category_ids STRING
);
