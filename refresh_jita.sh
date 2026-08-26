#!/usr/bin/env bash
#
# eve-jita-poller — denní refresh, stejný tvar jako refresh_my_orders.sh /
# refresh_perimeter.sh (standardizováno 2026-08-26). Spustit v Cloud Shellu z kořene
# repa:
#   bash refresh_jita.sh
#
# Udělá dvě věci: (1) spustí nový scan trhu (Cloud Run Job), (2) přepočítá ceny/marže
# top-of-book metodou přes bigquery/recompute_top_of_book.sql a uloží výsledek do CSV.
# Výstupní CSV pak stačí nahrát zpátky do konverzace s Claude — přegeneruje se z něj
# Excel report i dashboard.

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
JOB_NAME="eve-jita-poller"
OUT_CSV="recompute_top_of_book.csv"

echo "== 1/2: spouštím denní scan trhu (běží pár minut) =="
gcloud run jobs execute "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" --wait

echo "== 2/2: přepočítávám ceny/marže (top-of-book) z čerstvých dat =="
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 \
  < "$(dirname "$0")/bigquery/recompute_top_of_book.sql" > "$OUT_CSV"

echo ""
echo "Hotovo. Nahraj '$OUT_CSV' zpátky do konverzace s Claude."
