#!/usr/bin/env bash
set -euo pipefail

# ─── Single-expense flow (draft → update → submit → optional cancel) ───
#
# Env overrides:
#   EXPENSE_BASE_URL          API root (default prompts or http://localhost:8000)
#   EXPENSE_COMPANY           Company name (session defaults)
#   EXPENSE_SID               X-Frappe-SID / Cookie sid
#   EXPENSE_SUPPLIER          Supplier label (default: aavin)
#   EXPENSE_POSTING_DATE      e.g. 2026-04-01
#   EXPENSE_ITEM_GROUP        default Groceries (or suffixed if UNIQUE_ITEMS=1)
#   EXPENSE_ITEM_CODE         default Milk 2L
#   EXPENSE_UNIQUE_ITEMS=1    append e2e-<ts>-<rand>-$$ to item group/code (default 1; set 0 for prod)
#   EXPENSE_CANCEL=1          after submit, call cancel API (non-interactive)
#   EXPENSE_CANCEL=0          skip cancel and do not prompt (good for CI / e2e)
#   EXPENSE_SKIP_ITEM_SETUP=1 skip POST Item Group / Item (use when they already exist)
#   EXPENSE_INCLUDE_EXTRAS=1  also run: reject cancel-while-draft, list, dashboard
#
# Optional cancel (if EXPENSE_CANCEL unset): TTY prompts "Cancel after submit? [y/N]"

# ─── Config ───────────────────────────────────────────────────────────
if [[ -n "${EXPENSE_BASE_URL:-}" ]]; then
  BASE_URL="$EXPENSE_BASE_URL"
else
  read -rp "Enter API base URL [http://localhost:8000]: " BASE_URL
  BASE_URL="${BASE_URL:-http://localhost:8000}"
fi
if [[ "$BASE_URL" != http://* && "$BASE_URL" != https://* ]]; then
  BASE_URL="http://${BASE_URL}"
fi
BASE_URL="${BASE_URL%/}"

if [[ -n "${EXPENSE_COMPANY:-}" ]]; then
  COMPANY="$EXPENSE_COMPANY"
else
  read -rp "Enter Company Name (for session defaults): " COMPANY
fi
if [[ -z "$COMPANY" ]]; then
  echo "ERROR: Company name is required."
  exit 1
fi

if [[ -n "${EXPENSE_SID:-}" ]]; then
  SID="$EXPENSE_SID"
else
  read -rp "Enter X-Frappe-SID: " SID
fi
if [[ -z "$SID" ]]; then
  echo "ERROR: SID is required."
  exit 1
fi

SUPPLIER="${EXPENSE_SUPPLIER:-aavin}"
POSTING_DATE="${EXPENSE_POSTING_DATE:-2026-04-01}"
UNIQUE_ITEMS="${EXPENSE_UNIQUE_ITEMS:-1}"

if [[ "$UNIQUE_ITEMS" == "1" ]]; then
  E2E_SUFFIX="${EXPENSE_E2E_SUFFIX:-e2e-$(date +%s)-${RANDOM}-$$}"
  ITEM_GROUP="${EXPENSE_ITEM_GROUP:-Groceries-$E2E_SUFFIX}"
  ITEM_CODE="${EXPENSE_ITEM_CODE:-Milk 2L-$E2E_SUFFIX}"
else
  ITEM_GROUP="${EXPENSE_ITEM_GROUP:-Groceries}"
  ITEM_CODE="${EXPENSE_ITEM_CODE:-Milk 2L}"
fi

HEADERS=(
  -H "Accept: application/json"
  -H "Content-Type: application/json"
  -H "X-Requested-With: XMLHttpRequest"
  -H "X-Frappe-SID: $SID"
  -H "Cookie: sid=$SID"
)

SESS_CHECK=$(curl -s -w "\n%{http_code}" -G \
  "$BASE_URL/api/method/frappe.auth.get_logged_user" \
  -H "Accept: application/json" \
  -H "X-Frappe-SID: $SID" \
  -H "Cookie: sid=$SID")
SESS_HTTP=$(echo "$SESS_CHECK" | tail -1)
if [[ "$SESS_HTTP" == "401" ]] || [[ "$SESS_HTTP" == "403" ]]; then
  echo "ERROR: Session invalid or expired (HTTP $SESS_HTTP). Log in and pass a fresh sid."
  exit 1
fi

urlencode_name() {
  PINV_NAME="$1" python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["PINV_NAME"], safe=""))'
}

build_create_payload() {
  ITEM_GROUP="$ITEM_GROUP" ITEM_CODE="$ITEM_CODE" SUPPLIER="$SUPPLIER" POSTING_DATE="$POSTING_DATE" python3 <<'PY'
import json, os
print(json.dumps({
    "supplier": os.environ["SUPPLIER"],
    "posting_date": os.environ["POSTING_DATE"],
    "remarks": "Grocery for office pantry",
    "items": [{
        "item_code": os.environ["ITEM_CODE"],
        "item_group": os.environ["ITEM_GROUP"],
        "qty": 3,
        "rate": 4.50,
    }],
}))
PY
}

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Expense: draft → PUT update → submit → optional cancel"
echo "═══════════════════════════════════════════════════════════════"
echo "  Base URL : $BASE_URL"
echo "  Company  : $COMPANY"
echo "  Supplier : $SUPPLIER"
echo "  Item     : $ITEM_CODE / $ITEM_GROUP"
echo "═══════════════════════════════════════════════════════════════"
echo ""

if [[ "${EXPENSE_SKIP_ITEM_SETUP:-}" != "1" ]]; then
  echo "──── 1. POST Item Group '$ITEM_GROUP' ────"
  IG_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    "$BASE_URL/api/resource/Item%20Group" \
    "${HEADERS[@]}" \
    -d "{\"item_group_name\": \"$ITEM_GROUP\", \"parent_item_group\": \"All Item Groups\", \"is_group\": 0}")
  IG_HTTP=$(echo "$IG_RESP" | tail -1)
  IG_BODY=$(echo "$IG_RESP" | sed '$d')
  echo "HTTP $IG_HTTP"; echo "$IG_BODY" | python3 -m json.tool 2>/dev/null || echo "$IG_BODY"
  [[ "$IG_HTTP" -ge 200 && "$IG_HTTP" -lt 300 ]] || echo "⚠ (continuing if group already exists)"
  echo ""

  echo "──── 2. POST Item '$ITEM_CODE' ────"
  IT_RESP=$(curl -s -w "\n%{http_code}" -X POST \
    "$BASE_URL/api/resource/Item" \
    "${HEADERS[@]}" \
    -d "{
      \"item_code\": \"$ITEM_CODE\",
      \"item_name\": \"$ITEM_CODE\",
      \"item_group\": \"$ITEM_GROUP\",
      \"stock_uom\": \"Nos\",
      \"is_stock_item\": 0,
      \"is_purchase_item\": 1
    }")
  IT_HTTP=$(echo "$IT_RESP" | tail -1)
  IT_BODY=$(echo "$IT_RESP" | sed '$d')
  echo "HTTP $IT_HTTP"; echo "$IT_BODY" | python3 -m json.tool 2>/dev/null || echo "$IT_BODY"
  [[ "$IT_HTTP" -ge 200 && "$IT_HTTP" -lt 300 ]] || echo "⚠ (continuing if item already exists)"
  echo ""
else
  echo "──── Skipping Item Group / Item (EXPENSE_SKIP_ITEM_SETUP=1) ────"
  echo ""
fi

echo "──── 3. POST Purchase Invoice (draft, docstatus 0) ────"
PINV_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Purchase%20Invoice" \
  "${HEADERS[@]}" \
  -d "$(build_create_payload)")
PINV_HTTP=$(echo "$PINV_RESP" | tail -1)
PINV_BODY=$(echo "$PINV_RESP" | sed '$d')
echo "HTTP $PINV_HTTP"; echo "$PINV_BODY" | python3 -m json.tool 2>/dev/null || echo "$PINV_BODY"
[[ "$PINV_HTTP" -ge 200 && "$PINV_HTTP" -lt 300 ]] || { echo "FAILED: create draft"; exit 1; }
PINV_NAME=$(echo "$PINV_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null || true)
[[ -n "$PINV_NAME" ]] || { echo "FAILED: no invoice name in response"; exit 1; }
ENC=$(urlencode_name "$PINV_NAME")
echo "✓ Draft: $PINV_NAME"
echo ""

echo "──── 4. GET Purchase Invoice (verify draft) ────"
GET0=$(curl -s -w "\n%{http_code}" -X GET \
  "$BASE_URL/api/resource/Purchase%20Invoice/$ENC" \
  "${HEADERS[@]}")
GET0_HTTP=$(echo "$GET0" | tail -1)
GET0_BODY=$(echo "$GET0" | sed '$d')
echo "HTTP $GET0_HTTP"; echo "$GET0_BODY" | python3 -m json.tool 2>/dev/null || echo "$GET0_BODY"
[[ "$GET0_HTTP" -ge 200 && "$GET0_HTTP" -lt 300 ]] || exit 1
echo ""

if [[ "${EXPENSE_INCLUDE_EXTRAS:-}" == "1" ]]; then
  echo "──── 4b. POST cancel on draft (expect 400) ────"
  CAN_D=$(PINV_NAME="$PINV_NAME" python3 -c 'import json,os; print(json.dumps({"name": os.environ["PINV_NAME"]}))')
  R=$(curl -s -w "\n%{http_code}" -X POST \
    "$BASE_URL/api/method/expense_tracker.api.cancel_purchase_invoice" \
    "${HEADERS[@]}" -d "$CAN_D")
  echo "HTTP $(echo "$R" | tail -1)"; echo "$R" | sed '$d' | python3 -m json.tool 2>/dev/null || echo "$R" | sed '$d'
  echo ""
fi

echo "──── 5. PUT update draft (remarks) ────"
PATCH_REMARKS="${EXPENSE_PATCH_REMARKS:-Grocery for office pantry — updated before submit}"
PUT_PAYLOAD=$(PATCH_REMARKS="$PATCH_REMARKS" python3 -c 'import json,os; print(json.dumps({"remarks": os.environ["PATCH_REMARKS"]}))')
PUT_RESP=$(curl -s -w "\n%{http_code}" -X PUT \
  "$BASE_URL/api/resource/Purchase%20Invoice/$ENC" \
  "${HEADERS[@]}" \
  -d "$PUT_PAYLOAD")
PUT_HTTP=$(echo "$PUT_RESP" | tail -1)
PUT_BODY=$(echo "$PUT_RESP" | sed '$d')
echo "HTTP $PUT_HTTP"
echo "$PUT_BODY" | python3 -m json.tool 2>/dev/null || echo "$PUT_BODY"
[[ "$PUT_HTTP" -ge 200 && "$PUT_HTTP" -lt 300 ]] || { echo "FAILED: PUT"; exit 1; }
echo "✓ Updated"
echo ""

echo "──── 6. POST submit (draft → submitted) ────"
SUB_PAYLOAD=$(PINV_NAME="$PINV_NAME" python3 -c 'import json,os; print(json.dumps({"name": os.environ["PINV_NAME"]}))')
SUB_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/method/expense_tracker.api.submit_purchase_invoice" \
  "${HEADERS[@]}" \
  -d "$SUB_PAYLOAD")
SUB_HTTP=$(echo "$SUB_RESP" | tail -1)
SUB_BODY=$(echo "$SUB_RESP" | sed '$d')
echo "HTTP $SUB_HTTP"
echo "$SUB_BODY" | python3 -m json.tool 2>/dev/null || echo "$SUB_BODY"
[[ "$SUB_HTTP" -ge 200 && "$SUB_HTTP" -lt 300 ]] || { echo "FAILED: submit"; exit 1; }
echo "✓ Submitted"
echo ""

echo "──── 7. GET Purchase Invoice (verify docstatus 1) ────"
GET1=$(curl -s -w "\n%{http_code}" -X GET \
  "$BASE_URL/api/resource/Purchase%20Invoice/$ENC" \
  "${HEADERS[@]}")
GET1_HTTP=$(echo "$GET1" | tail -1)
GET1_BODY=$(echo "$GET1" | sed '$d')
echo "HTTP $GET1_HTTP"
echo "$GET1_BODY" | python3 -m json.tool 2>/dev/null || echo "$GET1_BODY"
echo ""

DO_CANCEL=false
if [[ "${EXPENSE_CANCEL:-}" == "1" ]]; then
  DO_CANCEL=true
elif [[ "${EXPENSE_CANCEL:-}" == "0" ]]; then
  :
elif [[ -t 0 ]]; then
  read -rp "Cancel this submitted expense? [y/N]: " _ans || true
  [[ "${_ans,,}" == y* ]] && DO_CANCEL=true
fi

if [[ "$DO_CANCEL" == true ]]; then
  echo "──── 8. POST cancel_purchase_invoice ────"
  CAN_P=$(PINV_NAME="$PINV_NAME" python3 -c 'import json,os; print(json.dumps({"name": os.environ["PINV_NAME"]}))')
  CAN_R=$(curl -s -w "\n%{http_code}" -X POST \
    "$BASE_URL/api/method/expense_tracker.api.cancel_purchase_invoice" \
    "${HEADERS[@]}" \
    -d "$CAN_P")
  CAN_HTTP=$(echo "$CAN_R" | tail -1)
  CAN_BODY=$(echo "$CAN_R" | sed '$d')
  echo "HTTP $CAN_HTTP"
  echo "$CAN_BODY" | python3 -m json.tool 2>/dev/null || echo "$CAN_BODY"
  [[ "$CAN_HTTP" -ge 200 && "$CAN_HTTP" -lt 300 ]] || { echo "FAILED: cancel"; exit 1; }
  echo "✓ Cancelled"
else
  echo "──── 8. Skip cancel (set EXPENSE_CANCEL=1 or answer y next time) ────"
fi
echo ""

if [[ "${EXPENSE_INCLUDE_EXTRAS:-}" == "1" ]]; then
  echo "──── List + dashboard ────"
  curl -s -G "$BASE_URL/api/resource/Purchase%20Invoice" \
    --data-urlencode "fields=name,company,supplier,posting_date,grand_total,remarks,docstatus,status" \
    --data-urlencode "limit=10" \
    --data-urlencode "order_by=modified desc" \
    -H "Accept: application/json" -H "X-Frappe-SID: $SID" -H "Cookie: sid=$SID" | python3 -m json.tool | head -40
  echo "…"
  curl -s -G "$BASE_URL/api/method/expense_tracker.api.get_dashboard_summary" \
    -H "Accept: application/json" -H "X-Frappe-SID: $SID" -H "Cookie: sid=$SID" | python3 -m json.tool
  echo ""
fi

echo "═══════════════════════════════════════════════════════════════"
echo "  Done — invoice: $PINV_NAME"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "─── cURL reference (replace BASE, SID, NAME, ENC) ───"
echo "# URL-encode invoice name for path:"
echo 'ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=\"\"))" "NAME")'
echo ""
echo "# UPDATE draft (PUT):"
echo 'curl -s -X PUT "${BASE}/api/resource/Purchase%20Invoice/${ENC}" \'
echo '  -H "Accept: application/json" -H "Content-Type: application/json" \'
echo '  -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}" \'
echo "  -d '{\"remarks\":\"Updated remarks\"}'"
echo ""
echo "# SUBMIT (draft → Submitted, docstatus 1):"
echo 'curl -s -X POST "${BASE}/api/method/expense_tracker.api.submit_purchase_invoice" \'
echo '  -H "Accept: application/json" -H "Content-Type: application/json" \'
echo '  -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}" \'
echo "  -d '{\"name\":\"NAME\"}'"
echo ""
echo "# CANCEL (submitted only, docstatus → 2):"
echo 'curl -s -X POST "${BASE}/api/method/expense_tracker.api.cancel_purchase_invoice" \'
echo '  -H "Accept: application/json" -H "Content-Type: application/json" \'
echo '  -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}" \'
echo "  -d '{\"name\":\"NAME\"}'"
echo ""
