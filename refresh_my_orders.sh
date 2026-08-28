#!/usr/bin/env bash
#
# esi-my-orders-poller — refresh Matej's own open orders, stejný tvar jako refresh.sh
# / refresh_perimeter.sh (standardizováno 2026-08-26). Spustit v Cloud Shellu z kořene
# repa:
#   bash refresh_my_orders.sh
#
# Udělá dvě věci: (1) spustí Cloud Run Job, co stáhne aktuální ordery přes ESI a
# zapíše je do BigQuery `my_orders` (potřebuje hotové přihlášení přes
# esi-oauth-service — viz jeho README), (2) vytáhne z BigQuery jen nejnovější řádek
# per order_id (tabulka je historie, ne snapshot-replace) a uloží do CSV. Výstupní CSV
# pak stačí nahrát zpátky do konverzace s Claude.

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
DATASET="eve_jita_scanner"
JOB_NAME="esi-my-orders-poller"
OUT_CSV="my_orders_latest.csv"

echo "== 1/2: spouštím stažení tvých orderů z ESI =="
gcloud run jobs execute "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" --wait

echo "== 2/2: vytahuji nejnovější řádek pro každý order_id, jen skutečně otevřené =="
# is_open filtr přidán 2026-08-28 (fix phantom-order bugu) — bez něj by tu pořád
# viselo Vexor/Platinum/Small Skill Injector jako "otevřené", i když dávno nejsou.
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 "
SELECT * EXCEPT(rn) FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
  FROM \`${PROJECT_ID}.${DATASET}.my_orders\`
)
WHERE rn = 1 AND (is_open IS NULL OR is_open = TRUE)
ORDER BY scanned_at DESC
" > "$OUT_CSV"

echo ""
echo "Hotovo. Nahraj '$OUT_CSV' zpátky do konverzace s Claude."
