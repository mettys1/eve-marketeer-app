#!/usr/bin/env bash
#
# check_stale_orders — najde ordery, co dlouho leží skoro nenaplněné (zamrzlý
# kapitál). Nespouští nový scan (na to je `refresh_my_orders.sh`) — jen se
# podívá na poslední už stažená data. Spustit v Cloud Shellu z kořene repa:
#   bash check_stale_orders.sh

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
OUT_CSV="stale_orders.csv"

echo "== hledám zamrzlý kapitál v posledním stavu orderů =="
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=200 \
  < "$(dirname "$0")/bigquery/stale_orders.sql" > "$OUT_CSV"

echo ""
echo "Hotovo. Nahraj '$OUT_CSV' zpátky do konverzace s Claude."
