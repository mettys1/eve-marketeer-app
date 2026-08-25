-- Přepočet nákupní/prodejní ceny na top-of-book (nejlepší standing order), místo dřívějšího
-- váženého průměru top 5 % objemu, který byl systematicky tažen dolů obřími "floor" nákupními
-- ordery na surovinách/moon goo (Promethium, Strontium Clathrates, compressed ore/gas, ...).
-- Používá RAW order book, který už v BigQuery leží z posledního scanu — žádný nový scan není
-- potřeba, přepočet je okamžitý.
--
-- Spustit v BigQuery konzoli nebo přes: bq query --use_legacy_sql=false < recompute_top_of_book.sql

WITH latest_orders AS (
  SELECT MAX(scan_date) AS d
  FROM `eve-jita-scanner-21359.eve_jita_scanner.market_orders_raw`
),
station AS (
  SELECT r.type_id, r.item_name, r.is_buy_order, r.price, r.volume_remain
  FROM `eve-jita-scanner-21359.eve_jita_scanner.market_orders_raw` r, latest_orders l
  WHERE r.scan_date = l.d
    AND r.location_id = 60003760  -- Jita IV - Moon 4 - Caldari Navy Assembly Plant
),
best_buy AS (
  SELECT type_id, item_name, price AS buy_price
  FROM station
  WHERE is_buy_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY price DESC) = 1
),
best_sell AS (
  SELECT type_id, price AS sell_price
  FROM station
  WHERE NOT is_buy_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY price ASC) = 1
),
order_counts AS (
  SELECT type_id,
    COUNTIF(is_buy_order) AS buy_orders,
    COUNTIF(NOT is_buy_order) AS sell_orders
  FROM station
  GROUP BY type_id
),
latest_snapshot AS (
  SELECT type_id, avg_daily_volume_14d
  FROM `eve-jita-scanner-21359.eve_jita_scanner.market_snapshots`
  WHERE scan_date = (SELECT MAX(scan_date) FROM `eve-jita-scanner-21359.eve_jita_scanner.market_snapshots`)
),
joined AS (
  SELECT
    bb.type_id,
    bb.item_name,
    ROUND(bb.buy_price, 2)  AS buy_price,
    ROUND(bs.sell_price, 2) AS sell_price,
    oc.buy_orders,
    oc.sell_orders,
    ls.avg_daily_volume_14d,
    ROUND((bs.sell_price * (1 - 0.01382 - 0.03375)) - (bb.buy_price * (1 + 0.01382)), 2) AS profit_per_unit,
    ROUND(SAFE_DIVIDE(
      (bs.sell_price * (1 - 0.01382 - 0.03375)) - (bb.buy_price * (1 + 0.01382)),
      bb.buy_price * (1 + 0.01382)
    ) * 100, 3) AS margin_pct
  FROM best_buy bb
  JOIN best_sell bs USING (type_id)
  JOIN order_counts oc USING (type_id)
  LEFT JOIN latest_snapshot ls USING (type_id)
)
SELECT *
FROM joined
WHERE buy_orders >= 3
  AND sell_orders >= 3
  AND avg_daily_volume_14d >= 50
  AND margin_pct > 0
ORDER BY margin_pct DESC;
