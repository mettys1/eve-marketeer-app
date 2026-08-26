-- Detekce "zamrzlého" kapitálu: ordery, které stojí dlouho a skoro se nehýbou.
-- Proxy metrika (dokud nemáme dost historie ze scanů na přesnou fill-velocity):
--   order_age_days = kolik dní je order v EVE reálně otevřený (od `issued`, ne od
--                     posledního scanu)
--   fill_pct        = kolik % z původního objemu se už prodalo/koupilo
-- Order označíme jako STALE, když je starý (>3 dny) a skoro nenaplněný (<10 %).
-- To signalizuje kapitál, co jen leží v escrow a nic nedělá — kandidát na
-- přecenění blíž k trhu, nebo na zrušení a přesun jinam.

WITH latest_orders AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
    FROM `eve-jita-scanner-21359.eve_jita_scanner.my_orders`
  )
  WHERE rn = 1
)
SELECT
  location_name,
  item_name,
  type_id,
  is_buy_order,
  price,
  volume_remain,
  volume_total,
  ROUND(SAFE_DIVIDE(volume_total - volume_remain, volume_total) * 100, 1) AS fill_pct,
  DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) AS order_age_days,
  ROUND(price * volume_remain, 0) AS isk_locked,
  CASE
    WHEN DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) >= 3
     AND SAFE_DIVIDE(volume_total - volume_remain, volume_total) < 0.10
    THEN 'STALE — kapitál zamrzlý'
    WHEN DATE_DIFF(CURRENT_DATE(), DATE(issued), DAY) >= 1
     AND SAFE_DIVIDE(volume_total - volume_remain, volume_total) < 0.10
    THEN 'sledovat'
    ELSE 'v pohybu'
  END AS status
FROM latest_orders
ORDER BY isk_locked DESC;
