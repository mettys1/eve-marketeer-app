-- Přepočet nákupní/prodejní ceny na top-of-book pro Perimeter citadelu ("0.0% Neutral
-- States Market HQ"), stejná metoda a stejný tvar výstupu jako
-- bigquery/recompute_top_of_book.sql pro Jitu — standardizováno 2026-08-26 na Matejovu
-- žádost, aby oba pipeline (Jita přes veřejné ESI, Perimeter přes autentizované ESI
-- structure_markets) fungovaly stejně: raw order book v BigQuery → tenhle recompute →
-- top-of-book CSV. Žádný nový scan není potřeba, přepočet je okamžitý z dat, co tam
-- nechal poslední běh esi-jobs/job_perimeter.js.
--
-- Rozdíl oproti Jita verzi: žádný `location_id` filtr (perimeter_orders_raw obsahuje
-- jen tuhle jednu strukturu, na rozdíl od market_orders_raw, které je region-wide) a
-- žádný avg_daily_volume_14d sloupec/filtr — pro Perimeter nemáme ekvivalent
-- market_history (ESI region-wide historie by beztak neříkala nic o objemu na téhle
-- konkrétní struktuře). Pokud to bude chybět, zvaž doplnění zvlášť později.
--
-- Spustit: bq query --use_legacy_sql=false < recompute_perimeter_top_of_book.sql
-- Pro plný export:
--   bq query --use_legacy_sql=false --format=csv --max_rows=5000 \
--     < recompute_perimeter_top_of_book.sql > perimeter_top_of_book.csv

WITH latest_orders AS (
  SELECT MAX(scan_date) AS d
  FROM `eve-jita-scanner-21359.eve_jita_scanner.perimeter_orders_raw`
),
structure AS (
  SELECT r.type_id, r.item_name, r.is_buy_order, r.price, r.volume_remain
  FROM `eve-jita-scanner-21359.eve_jita_scanner.perimeter_orders_raw` r, latest_orders l
  WHERE r.scan_date = l.d
),
best_buy AS (
  SELECT type_id, item_name, price AS buy_price
  FROM structure
  WHERE is_buy_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY price DESC) = 1
),
best_sell AS (
  SELECT type_id, price AS sell_price
  FROM structure
  WHERE NOT is_buy_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY price ASC) = 1
),
order_counts AS (
  SELECT type_id,
    COUNTIF(is_buy_order) AS buy_orders,
    COUNTIF(NOT is_buy_order) AS sell_orders
  FROM structure
  GROUP BY type_id
),
joined AS (
  SELECT
    bb.type_id,
    bb.item_name,
    ROUND(bb.buy_price, 2)  AS buy_price,
    ROUND(bs.sell_price, 2) AS sell_price,
    oc.buy_orders,
    oc.sell_orders,
    ROUND((bs.sell_price * (1 - 0.01382 - 0.03375)) - (bb.buy_price * (1 + 0.01382)), 2) AS profit_per_unit,
    ROUND(SAFE_DIVIDE(
      (bs.sell_price * (1 - 0.01382 - 0.03375)) - (bb.buy_price * (1 + 0.01382)),
      bb.buy_price * (1 + 0.01382)
    ) * 100, 3) AS margin_pct
  FROM best_buy bb
  JOIN best_sell bs USING (type_id)
  JOIN order_counts oc USING (type_id)
)
SELECT *
FROM joined
WHERE buy_orders >= 3
  AND sell_orders >= 3
  AND margin_pct BETWEEN 0.1 AND 100
ORDER BY margin_pct DESC;
