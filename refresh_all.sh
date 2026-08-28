#!/usr/bin/env bash
#
# refresh_all.sh — jeden skript, co refreshne úplně všechno naráz: Jita scan,
# Perimeter, tvoje otevřené ordery (my_orders) a wallet (transakce + deník).
# Spustit v Cloud Shellu z kořene repa:
#   bash refresh_all.sh
#
# Všechny 4 Cloud Run Joby na sobě nezávisí, takže běží PARALELNĚ — celkový čas
# je daný tím nejpomalejším z nich (obvykle Jita scan), ne součtem všech čtyř.
# Po dokončení se navíc přepočítá top-of-book pro Jitu i Perimeter a exportuje
# se aktuální stav tvých orderů — ale nic z toho už nemusíš nikam nahrávat,
# Claude si to tahá přímo z BigQuery přes /report a bq query.

set -uo pipefail   # záměrně BEZ -e — chceme vidět výsledek všech 4 jobů, i kdyby jeden spadl

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "== Spouštím všechny 4 scany paralelně (Jita, Perimeter, my_orders, wallet) =="
echo "   (pár minut — Jita scan bývá nejpomalejší, ostatní tři doběhnou dřív)"
echo ""

gcloud run jobs execute eve-jita-poller      --region="$REGION" --project="$PROJECT_ID" --wait > /tmp/refresh_jita.log      2>&1 &
PID_JITA=$!
gcloud run jobs execute esi-perimeter-poller --region="$REGION" --project="$PROJECT_ID" --wait > /tmp/refresh_perimeter.log 2>&1 &
PID_PERIM=$!
gcloud run jobs execute esi-my-orders-poller --region="$REGION" --project="$PROJECT_ID" --wait > /tmp/refresh_my_orders.log 2>&1 &
PID_ORDERS=$!
gcloud run jobs execute esi-wallet-poller    --region="$REGION" --project="$PROJECT_ID" --wait > /tmp/refresh_wallet.log    2>&1 &
PID_WALLET=$!

FAIL=0
wait "$PID_JITA"   || { echo "!! eve-jita-poller SELHAL — viz /tmp/refresh_jita.log";          FAIL=1; }
wait "$PID_PERIM"  || { echo "!! esi-perimeter-poller SELHAL — viz /tmp/refresh_perimeter.log"; FAIL=1; }
wait "$PID_ORDERS" || { echo "!! esi-my-orders-poller SELHAL — viz /tmp/refresh_my_orders.log"; FAIL=1; }
wait "$PID_WALLET" || { echo "!! esi-wallet-poller SELHAL — viz /tmp/refresh_wallet.log";       FAIL=1; }

if [ "$FAIL" = "1" ]; then
  echo ""
  echo "Aspoň jeden job selhal — zkontroluj log(y) výše, než pojedeš dál."
  exit 1
fi

echo ""
echo "== Všechny 4 scany hotové. Přepočítávám top-of-book (Jita + Perimeter) a export orderů =="

bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 \
  < "$REPO_ROOT/bigquery/recompute_top_of_book.sql" > "$REPO_ROOT/recompute_top_of_book.csv"

bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 \
  < "$REPO_ROOT/bigquery/recompute_perimeter_top_of_book.sql" > "$REPO_ROOT/perimeter_top_of_book.csv"

# is_open filtr přidán 2026-08-28 (fix phantom-order bugu) — bez něj by tu pořád
# viselo Vexor/Platinum/Small Skill Injector jako "otevřené", i když dávno nejsou.
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" --format=csv --max_rows=5000 "
SELECT * EXCEPT(rn) FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY scanned_at DESC) AS rn
  FROM \`${PROJECT_ID}.eve_jita_scanner.my_orders\`
)
WHERE rn = 1 AND (is_open IS NULL OR is_open = TRUE)
ORDER BY scanned_at DESC
" > "$REPO_ROOT/my_orders_latest.csv"

echo ""
echo "HOTOVO. Všechna data jsou čerstvá v BigQuery. Nic nemusíš nahrávat do konverzace —"
echo "stačí říct Claudovi 'refresh hotov, pojďme dál' a on si zbytek stáhne sám."
