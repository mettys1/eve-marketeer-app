#!/usr/bin/env bash
#
# esi-perimeter-poller — Perimeter refresh, stejný tvar jako refresh.sh pro Jitu
# (standardizováno 2026-08-26). Spustit v Cloud Shellu z kořene repa:
#   bash refresh_perimeter.sh
#
# Udělá dvě věci: (1) spustí nový scan Perimeter citadely přes ESI (Cloud Run Job,
# potřebuje hotové přihlášení přes esi-oauth-service — viz jeho README), (2) přepočítá
# ceny/marže top-of-book metodou přes bigquery/recompute_perimeter_top_of_book.sql a
# uloží výsledek do CSV. Výstupní CSV pak stačí nahrát zpátky do konverzace s Claude.

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
JOB_NAME="esi-perimeter-poller"
OUT_CSV="perimeter_top_of_book.csv"

echo "== 1/2: spouštím scan Perimeter citadely (běží pár minut) =="
gcloud run jobs execute "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" --wait

echo "== 2/2: přepočítávám ceny/marže (top-of-book) z čerstvých dat =="
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 \
  < "$(dirname "$0")/bigquery/recompute_perimeter_top_of_book.sql" > "$OUT_CSV"

echo ""
echo "Hotovo. Nahraj '$OUT_CSV' zpátky do konverzace s Claude."
