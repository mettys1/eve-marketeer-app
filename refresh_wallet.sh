#!/usr/bin/env bash
#
# esi-wallet-poller — stáhne wallet transakce + peněžní deník přes ESI a zapíše je do
# BigQuery (wallet_transactions, wallet_journal). Na rozdíl od refresh_my_orders.sh /
# refresh_perimeter.sh / refresh_jita.sh nevytváří CSV — tohle je čistě sběr trvalé
# historie do BigQuery, analýza (stale orders podle skutečných fillů, ne podle
# `issued`) se dělá dotazem přímo na tabulku, ne přes CSV upload. Spustit v Cloud
# Shellu z kořene repa:
#   bash refresh_wallet.sh
#
# Potřebuje scope esi-wallet.read_character_wallet.v1 — pokud job spadne na 403
# zmiňujícím scope, znovu se přihlas přes /login (viz esi-oauth-service/README.md),
# ať se token obnoví s novým oprávněním.

set -euo pipefail

PROJECT_ID="eve-jita-scanner-21359"
REGION="europe-west1"
JOB_NAME="esi-wallet-poller"

echo "== stahuji wallet transakce + deník přes ESI =="
gcloud run jobs execute "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" --wait

echo ""
echo "Hotovo. Data jsou v BigQuery (wallet_transactions, wallet_journal) — žádné CSV"
echo "k nahrání, Claude si je stáhne sám přes /report endpoint."
