-- Přepočet nákupní/prodejní ceny na top-of-book (nejlepší standing order), místo dřívějšího
-- váženého průměru top 5 % objemu, který byl systematicky tažen dolů obřími "floor" nákupními
-- ordery na surovinách/moon goo (Promethium, Strontium Clathrates, compressed ore/gas, ...).
-- Používá RAW order book, který už v BigQuery leží z posledního scanu — žádný nový scan není
-- potřeba, přepočet je okamžitý.
--
-- v3: opravuje problém nalezený v prvním běhu a ruší chybnou úvahu z v2:
--   1) latest_snapshot mělo víc než 1 řádek na type_id pro daný scan_date (reziduální řádky ze
--      starších testovacích běhů ve watchlist módu, spuštěných ten samý den) -> LEFT JOIN
--      duplikoval výstupní řádky (viz X5 Enduring Stasis Webifier, Arbalest Compact Light
--      Missile Launcher v prvním běhu). Teď bereme nejnovější řádek podle scanned_at. (Beze změny.)
--   2) v2 přidalo min. 8 orderů na stranu s odůvodněním "u tenké knihy čekáš dny, než na tebe
--      přijde řada" -- to je špatně. EVE market matching je price-priority: nový nejlepší order
--      okamžitě chytá VEŠKERÝ příchozí instant-buy/sell flow, žádná fronta neexistuje. Vráceno
--      zpátky na původní >= 3. Skutečné riziko u tenkých knih (typu Soil, buy 1.3 / sell 80) není
--      "čekání", ale že avg_daily_volume_14d je za celý region a neříká, jak moc je ten objem
--      obousměrný -- u loot itemů může jít skoro celý o farmáře dumpující do buy orderů, bez
--      reálné poptávky na sell straně. To z jednoho snapshotu knihy nejde ověřit; margin_pct
--      strop <=100 % zůstává jako hrubá pojistka, ale u čehokoliv blízko stropu stojí za to mrknout
--      do klienta na skutečnou hloubku/historii, než na to dáš kapitál.
--
-- Spustit v BigQuery konzoli nebo přes: bq query --use_legacy_sql=false < recompute_top_of_book.sql
-- Pro plný export (ne jen prvních ~100 řádků v terminálu):
--   bq query --use_legacy_sql=false --format=csv --max_rows=5000 \
--     < bigquery/recompute_top_of_book.sql > recompute_top_of_book.csv

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
  -- Nejnovější řádek PER type_id, ne jen "cokoliv z posledního dne" — market_snapshots může mít
  -- pro stejný scan_date víc než jeden běh (reziduální testovací data), takže bez tohohle dedup
  -- se LEFT JOIN níž znásobí.
  SELECT type_id, avg_daily_volume_14d
  FROM `eve-jita-scanner-21359.eve_jita_scanner.market_snapshots`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY type_id ORDER BY scanned_at DESC) = 1
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
  AND margin_pct BETWEEN 0.1 AND 100
ORDER BY margin_pct DESC;
