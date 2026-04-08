#!/usr/bin/env bash
set -euo pipefail

# ─── Single-expense flow (draft → update → submit → optional delete) ───
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
#   EXPENSE_DELETE=1          DELETE invoice + GET expect 404 (submitted invoices are auto-cancelled then deleted).
#   EXPENSE_STOP_BEFORE_SUBMIT=1  exit after PUT (step 5); pair with EXPENSE_DELETE=1 to delete draft only.
#   EXPENSE_SKIP_ITEM_SETUP=1 skip POST Item Group / Item (use when they already exist)
#   EXPENSE_INCLUDE_EXTRAS=1  also run: list, dashboard
#   EXPENSE_EXTENDED_GET_DELETE=1  after main flow: GET submitted + DELETE + verify 404;
#                             optional second draft PI demo if needed. With STOP_BEFORE_SUBMIT,
#                             runs GET+DELETE on the main draft before exit.
#
# GET by id: steps 4 (after create) and 7 (after submit) — GET /api/resource/Purchase%20Invoice/{ENC}

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

# GET by id; print HTTP + one-line name/docstatus/status (optional full JSON via EXPENSE_GET_FULL_JSON=1).
run_get_pinv_by_id() {
  local enc="$1"
  local title="$2"
  echo "──── GET by id — $title ────"
  local G G_HTTP G_BODY
  G=$(curl -s -w "\n%{http_code}" -X GET \
    "$BASE_URL/api/resource/Purchase%20Invoice/$enc" \
    "${HEADERS[@]}")
  G_HTTP=$(echo "$G" | tail -1)
  G_BODY=$(echo "$G" | sed '$d')
  echo "HTTP $G_HTTP"
  if [[ "${EXPENSE_GET_FULL_JSON:-}" == "1" ]]; then
    echo "$G_BODY" | python3 -m json.tool 2>/dev/null || echo "$G_BODY"
  else
    echo "$G_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  name=%r docstatus=%s status=%r'%(d.get('name'),d.get('docstatus'),d.get('status')))" 2>/dev/null || echo "$G_BODY"
  fi
  [[ "$G_HTTP" -ge 200 && "$G_HTTP" -lt 300 ]] || return 1
  echo ""
  return 0
}

# DELETE Purchase Invoice then GET — expect 404 ($1 = URL-encoded name; default: global ENC).
run_delete_pinv_and_verify() {
  local enc="${1:-$ENC}"
  echo "──── DELETE by id — $enc ────"
  local DEL_R DEL_HTTP DEL_BODY GETD GETD_HTTP GETD_BODY
  DEL_R=$(curl -s -w "\n%{http_code}" -X DELETE \
    "$BASE_URL/api/resource/Purchase%20Invoice/$enc" \
    "${HEADERS[@]}")
  DEL_HTTP=$(echo "$DEL_R" | tail -1)
  DEL_BODY=$(echo "$DEL_R" | sed '$d')
  echo "HTTP $DEL_HTTP"
  echo "$DEL_BODY" | python3 -m json.tool 2>/dev/null || echo "$DEL_BODY"
  [[ "$DEL_HTTP" -ge 200 && "$DEL_HTTP" -lt 300 ]] || return 1
  echo "✓ Deleted"
  echo ""
  echo "──── GET after delete (expect 404) ────"
  GETD=$(curl -s -w "\n%{http_code}" -X GET \
    "$BASE_URL/api/resource/Purchase%20Invoice/$enc" \
    "${HEADERS[@]}")
  GETD_HTTP=$(echo "$GETD" | tail -1)
  GETD_BODY=$(echo "$GETD" | sed '$d')
  echo "HTTP $GETD_HTTP"
  echo "$GETD_BODY" | python3 -m json.tool 2>/dev/null || echo "$GETD_BODY"
  [[ "$GETD_HTTP" == "404" ]] || return 2
  echo "✓ Confirmed removed"
  echo ""
  return 0
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
echo "  Expense: draft → PUT update → submit → optional cancel → optional DELETE"
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

if [[ "${EXPENSE_STOP_BEFORE_SUBMIT:-}" == "1" ]]; then
  echo "──── STOP_BEFORE_SUBMIT: skipping submit / final delete ────"
  if [[ "${EXPENSE_EXTENDED_GET_DELETE:-}" == "1" ]]; then
    run_get_pinv_by_id "$ENC" "main invoice (draft)" || exit 1
    run_delete_pinv_and_verify "$ENC" || { echo "FAILED: draft DELETE or post-delete GET"; exit 1; }
  elif [[ "${EXPENSE_DELETE:-}" == "1" ]]; then
    run_delete_pinv_and_verify || { echo "FAILED: DELETE or post-delete GET"; exit 1; }
  else
    echo "(invoice still draft on server — set EXPENSE_DELETE=1 or EXPENSE_EXTENDED_GET_DELETE=1)"
    echo ""
  fi
  echo "═══════════════════════════════════════════════════════════════"
  echo "  Done (stopped before submit) — invoice: $PINV_NAME"
  echo "═══════════════════════════════════════════════════════════════"
  exit 0
fi

echo "──── 6. POST submit (draft → submitted) ────"
SUB_PAYLOAD=$(PINV_NAME="$PINV_NAME" python3 -c 'import json,os; print(json.dumps({"name": os.environ["PINV_NAME"]}))')
SUB_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/method/frappe.client.submit" \
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

if [[ "${EXPENSE_EXTENDED_GET_DELETE:-}" == "1" ]]; then
  echo "═══════════════════════════════════════════════════════════════"
  echo "  EXTENDED: GET/DELETE by id (submitted + draft)"
  echo "═══════════════════════════════════════════════════════════════"
  echo ""

  echo "──── Ext 1. GET by id (main invoice — expect submitted) ────"
  run_get_pinv_by_id "$ENC" "main after submit (docstatus 1)" || exit 1

  echo "──── Ext 2. DELETE by id (submitted — auto-cancel then delete) ────"
  SUBDEL=$(curl -s -w "\n%{http_code}" -X DELETE \
    "$BASE_URL/api/resource/Purchase%20Invoice/$ENC" \
    "${HEADERS[@]}")
  SUBDEL_HTTP=$(echo "$SUBDEL" | tail -1)
  SUBDEL_BODY=$(echo "$SUBDEL" | sed '$d')
  echo "HTTP $SUBDEL_HTTP"
  echo "$SUBDEL_BODY" | python3 -m json.tool 2>/dev/null || echo "$SUBDEL_BODY"
  if [[ "$SUBDEL_HTTP" -ge 200 && "$SUBDEL_HTTP" -lt 300 ]]; then
    echo "✓ Submitted invoice deleted."
    echo "──── Ext 2b. GET after delete main (expect 404) ────"
    GONE=$(curl -s -w "\n%{http_code}" -X GET \
      "$BASE_URL/api/resource/Purchase%20Invoice/$ENC" \
      "${HEADERS[@]}")
    echo "HTTP $(echo "$GONE" | tail -1)"
    echo "$(echo "$GONE" | sed '$d')" | python3 -m json.tool 2>/dev/null || echo "$(echo "$GONE" | sed '$d')"
    echo ""
    echo "⚠ Main invoice removed; skipping Ext 3 second draft demo."
  else
    echo "⚠ Submitted DELETE returned HTTP $SUBDEL_HTTP (unexpected — check server logs)."
    echo ""
    echo "──── Ext 3. Second PI (draft only) — POST → GET → DELETE → GET 404 ────"
    EXT_JSON=$(COMPANY="$COMPANY" ITEM_GROUP="$ITEM_GROUP" ITEM_CODE="$ITEM_CODE" SUPPLIER="$SUPPLIER" POSTING_DATE="$POSTING_DATE" python3 <<'PY'
import json, os
d = {
    "supplier": os.environ["SUPPLIER"],
    "posting_date": os.environ["POSTING_DATE"],
    "remarks": "Extended: draft GET/DELETE demo (second PI)",
    "items": [{"item_code": os.environ["ITEM_CODE"], "item_group": os.environ["ITEM_GROUP"], "qty": 1, "rate": 0.01}],
}
c = (os.environ.get("COMPANY") or "").strip()
if c:
    d["company"] = c
print(json.dumps(d))
PY
)
    P2R=$(curl -s -w "\n%{http_code}" -X POST \
      "$BASE_URL/api/resource/Purchase%20Invoice" \
      "${HEADERS[@]}" \
      -d "$EXT_JSON")
    P2_HTTP=$(echo "$P2R" | tail -1)
    P2_BODY=$(echo "$P2R" | sed '$d')
    echo "POST HTTP $P2_HTTP"
    echo "$P2_BODY" | python3 -m json.tool 2>/dev/null || echo "$P2_BODY"
    [[ "$P2_HTTP" -ge 200 && "$P2_HTTP" -lt 300 ]] || { echo "FAILED: second PI create"; exit 1; }
    PINV2=$(echo "$P2_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null || true)
    [[ -n "$PINV2" ]] || { echo "FAILED: no name for second PI"; exit 1; }
    ENC2=$(urlencode_name "$PINV2")
    echo "✓ Second draft: $PINV2"
    echo ""

    run_get_pinv_by_id "$ENC2" "second PI (draft)" || exit 1
    run_delete_pinv_and_verify "$ENC2" || { echo "FAILED: second PI delete"; exit 1; }
  fi
  echo ""
fi

if [[ "${EXPENSE_DELETE:-}" == "1" ]] && [[ "${EXPENSE_EXTENDED_GET_DELETE:-}" != "1" ]]; then
  echo "──── 9–10. DELETE + GET (expect 404) ────"
  if run_delete_pinv_and_verify; then
    :
  else
    echo "FAILED: DELETE or post-delete GET."
    echo "  Hint: use EXPENSE_STOP_BEFORE_SUBMIT=1 EXPENSE_DELETE=1 ... to delete a draft only."
    exit 1
  fi
elif [[ "${EXPENSE_EXTENDED_GET_DELETE:-}" != "1" ]]; then
  echo "──── 9–10. Skip DELETE + verify GET (EXPENSE_DELETE=1; draft path: add EXPENSE_STOP_BEFORE_SUBMIT=1) ────"
  echo ""
fi

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
echo "# GET one by id (full JSON document):"
echo 'curl -s -X GET "${BASE}/api/resource/Purchase%20Invoice/${ENC}" \'
echo '  -H "Accept: application/json" -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}"'
echo ""
echo "# DELETE one (draft, cancelled, or submitted — submitted is auto-cancelled first):"
echo 'curl -s -X DELETE "${BASE}/api/resource/Purchase%20Invoice/${ENC}" \'
echo '  -H "Accept: application/json" -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}"'
echo ""
echo "# UPDATE draft (PUT):"
echo 'curl -s -X PUT "${BASE}/api/resource/Purchase%20Invoice/${ENC}" \'
echo '  -H "Accept: application/json" -H "Content-Type: application/json" \'
echo '  -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}" \'
echo "  -d '{\"remarks\":\"Updated remarks\"}'"
echo ""
echo "# SUBMIT (draft → Submitted, docstatus 1):"
echo 'curl -s -X POST "${BASE}/api/method/frappe.client.submit" \'
echo '  -H "Accept: application/json" -H "Content-Type: application/json" \'
echo '  -H "X-Requested-With: XMLHttpRequest" \'
echo '  -H "X-Frappe-SID: ${SID}" -H "Cookie: sid=${SID}" \'
echo "  -d '{\"name\":\"NAME\"}'"
echo ""
