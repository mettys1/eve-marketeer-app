-- Pro každý Matejův aktuálně otevřený order (Jita i Perimeter) vytáhne aktuální
-- nejlepší konkurenční cenu na OBOU trzích, bez filtru na minimální likviditu
-- (recompute_*.sql tenhle filtr má, proto v nich Matejovy konkrétní itemy chybí).
-- Účel: rozhodnout přecenit / nechat / zrušit pro každý order zvlášť.

WITH latest_orders AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
    FROM `eve-jita-scanner-21359.eve_jita_scanner.my_orders`
  )
  WHERE rn = 1
),
perimeter_book AS (
  SELECT
    type_id,
    MAX(IF(is_buy_order, price, NULL)) AS best_buy,
    MIN(IF(NOT is_buy_order, price, NULL)) AS best_sell,
    COUNTIF(is_buy_order) AS buy_orders,
    COUNTIF(NOT is_buy_order) AS sell_orders
  FROM `eve-jita-scanner-21359.eve_jita_scanner.perimeter_orders_raw`
  WHERE scan_date = (SELECT MAX(scan_date) FROM `eve-jita-scanner-21359.eve_jita_scanner.perimeter_orders_raw`)
  GROUP BY type_id
),
jita_book AS (
  SELECT
    type_id,
    MAX(IF(is_buy_order, price, NULL)) AS best_buy,
    MIN(IF(NOT is_buy_order, price, NULL)) AS best_sell,
    COUNTIF(is_buy_order) AS buy_orders,
    COUNTIF(NOT is_buy_order) AS sell_orders
  FROM `eve-jita-scanner-21359.eve_jita_scanner.market_orders_raw`
  WHERE scan_date = (SELECT MAX(scan_date) FROM `eve-jita-scanner-21359.eve_jita_scanner.market_orders_raw`)
    AND location_id = 60003760
  GROUP BY type_id
)
SELECT
  o.location_name,
  o.item_name,
  o.type_id,
  o.is_buy_order,
  o.price AS my_price,
  o.volume_remain,
  o.volume_total,
  o.issued,
  p.best_buy AS perim_best_buy,
  p.best_sell AS perim_best_sell,
  p.buy_orders AS perim_buy_ct,
  p.sell_orders AS perim_sell_ct,
  j.best_buy AS jita_best_buy,
  j.best_sell AS jita_best_sell,
  j.buy_orders AS jita_buy_ct,
  j.sell_orders AS jita_sell_ct
FROM latest_orders o
LEFT JOIN perimeter_book p USING (type_id)
LEFT JOIN jita_book j USING (type_id)
ORDER BY o.location_name, o.item_name;
