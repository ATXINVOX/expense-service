#!/usr/bin/env bash
set -euo pipefail

# ─── Prompt for inputs ───────────────────────────────────────────────
read -rp "Enter API base URL [https://api.atxinvox.com.au]: " BASE_URL
BASE_URL="${BASE_URL:-https://api.atxinvox.com.au}"

read -rp "Enter Company Name: " COMPANY
if [[ -z "$COMPANY" ]]; then
  echo "ERROR: Company name is required."
  exit 1
fi

read -rp "Enter X-Frappe-SID: " SID
if [[ -z "$SID" ]]; then
  echo "ERROR: SID is required."
  exit 1
fi

HEADERS=(
  -H "Accept: application/json"
  -H "Content-Type: application/json"
  -H "X-Requested-With: XMLHttpRequest"
  -H "X-Frappe-SID: $SID"
  -H "Cookie: sid=$SID"
)

ITEM_GROUP="Office Supplies - $(date +%s)"
ITEM_NAME="Printer Paper A4 - $(date +%s)"
SUPPLIER="Officeworks - $(date +%s)"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Expense Flow Test"
echo "═══════════════════════════════════════════════════"
echo "  Base URL  : $BASE_URL"
echo "  Company   : $COMPANY"
echo "  Item Group: $ITEM_GROUP"
echo "  Item      : $ITEM_NAME"
echo "  Supplier  : $SUPPLIER"
echo "═══════════════════════════════════════════════════"
echo ""

# ─── Step 1: Create Item Group ────────────────────────────────────────
echo "──── Step 1: Create Item Group ────"
ITEM_GROUP_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Item%20Group" \
  "${HEADERS[@]}" \
  -d "{
    \"item_group_name\": \"$ITEM_GROUP\",
    \"parent_item_group\": \"All Item Groups\",
    \"is_group\": 0
  }")

ITEM_GROUP_HTTP=$(echo "$ITEM_GROUP_RESP" | tail -1)
ITEM_GROUP_BODY=$(echo "$ITEM_GROUP_RESP" | sed '$d')

echo "HTTP $ITEM_GROUP_HTTP"
echo "$ITEM_GROUP_BODY" | python3 -m json.tool 2>/dev/null || echo "$ITEM_GROUP_BODY"

if [[ "$ITEM_GROUP_HTTP" -lt 200 || "$ITEM_GROUP_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not create Item Group."
  exit 1
fi
echo "✓ Item Group created"
echo ""

# ─── Step 2: Create Item ──────────────────────────────────────────────
echo "──── Step 2: Create Item ────"
ITEM_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Item" \
  "${HEADERS[@]}" \
  -d "{
    \"item_code\": \"$ITEM_NAME\",
    \"item_name\": \"$ITEM_NAME\",
    \"item_group\": \"$ITEM_GROUP\",
    \"stock_uom\": \"Nos\",
    \"is_stock_item\": 0,
    \"is_purchase_item\": 1
  }")

ITEM_HTTP=$(echo "$ITEM_RESP" | tail -1)
ITEM_BODY=$(echo "$ITEM_RESP" | sed '$d')

echo "HTTP $ITEM_HTTP"
echo "$ITEM_BODY" | python3 -m json.tool 2>/dev/null || echo "$ITEM_BODY"

if [[ "$ITEM_HTTP" -lt 200 || "$ITEM_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not create Item."
  exit 1
fi
echo "✓ Item created"
echo ""

# ─── Step 3: Create Purchase Invoice ─────────────────────────────────
echo "──── Step 3: Create Purchase Invoice (Expense) ────"
TODAY=$(date +%Y-%m-%d)
PINV_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Purchase%20Invoice" \
  "${HEADERS[@]}" \
  -d "{
    \"company\": \"$COMPANY\",
    \"supplier\": \"$SUPPLIER\",
    \"posting_date\": \"$TODAY\",
    \"remarks\": \"Test expense from shell script\",
    \"items\": [
      {
        \"item_code\": \"$ITEM_NAME\",
        \"item_group\": \"$ITEM_GROUP\",
        \"qty\": 2,
        \"rate\": 45.50
      }
    ]
  }")

PINV_HTTP=$(echo "$PINV_RESP" | tail -1)
PINV_BODY=$(echo "$PINV_RESP" | sed '$d')

echo "HTTP $PINV_HTTP"
echo "$PINV_BODY" | python3 -m json.tool 2>/dev/null || echo "$PINV_BODY"

if [[ "$PINV_HTTP" -lt 200 || "$PINV_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not create Purchase Invoice."
  exit 1
fi
echo "✓ Purchase Invoice created"
echo ""

# ─── Summary ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════"
echo "  ALL STEPS PASSED"
echo "═══════════════════════════════════════════════════"
echo "  Item Group : $ITEM_GROUP"
echo "  Item       : $ITEM_NAME"
echo "  Supplier   : $SUPPLIER (auto-created by backend)"
echo "  Invoice    : $(echo "$PINV_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name','N/A'))" 2>/dev/null || echo "see response above")"
echo "  Date       : $TODAY"
echo "  Amount     : 2 x \$45.50 = \$91.00"
echo "═══════════════════════════════════════════════════"
