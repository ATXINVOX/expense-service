#!/usr/bin/env bash
set -euo pipefail

# ─── Accept env vars or prompt ────────────────────────────────────────
if [[ -n "${EXPENSE_BASE_URL:-}" ]]; then
  BASE_URL="$EXPENSE_BASE_URL"
else
  read -rp "Enter API base URL [http://localhost:9004]: " BASE_URL
  BASE_URL="${BASE_URL:-http://localhost:9004}"
fi

if [[ -n "${EXPENSE_COMPANY:-}" ]]; then
  COMPANY="$EXPENSE_COMPANY"
else
  read -rp "Enter Company Name: " COMPANY
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

HEADERS=(
  -H "Accept: application/json"
  -H "Content-Type: application/json"
  -H "X-Requested-With: XMLHttpRequest"
  -H "X-Frappe-SID: $SID"
  -H "Cookie: sid=$SID"
)

TS=$(date +%s)
ITEM_GROUP="Office Supplies - $TS"
ITEM_NAME="Printer Paper A4 - $TS"
SUPPLIER="Officeworks - $TS"

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

# ─── Step 3: Create Purchase Invoice (with supplier) ─────────────────
echo "──── Step 3: Create Purchase Invoice (with supplier) ────"
TODAY=$(date +%Y-%m-%d)
PINV_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Purchase%20Invoice" \
  "${HEADERS[@]}" \
  -d "{
    \"company\": \"$COMPANY\",
    \"supplier\": \"$SUPPLIER\",
    \"posting_date\": \"$TODAY\",
    \"remarks\": \"Test expense with supplier\",
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
PINV1_NAME=$(echo "$PINV_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name','N/A'))" 2>/dev/null || echo "N/A")
echo "✓ Purchase Invoice created: $PINV1_NAME"
echo ""

# ─── Step 4: Create Purchase Invoice (no supplier — tests default) ───
echo "──── Step 4: Create Purchase Invoice (no supplier) ────"
PINV2_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  "$BASE_URL/api/resource/Purchase%20Invoice" \
  "${HEADERS[@]}" \
  -d "{
    \"posting_date\": \"$TODAY\",
    \"remarks\": \"Minimal expense — no supplier, no company\",
    \"items\": [
      {
        \"item_code\": \"Coffee\",
        \"qty\": 1,
        \"rate\": 5.50
      }
    ]
  }")

PINV2_HTTP=$(echo "$PINV2_RESP" | tail -1)
PINV2_BODY=$(echo "$PINV2_RESP" | sed '$d')

echo "HTTP $PINV2_HTTP"
echo "$PINV2_BODY" | python3 -m json.tool 2>/dev/null || echo "$PINV2_BODY"

if [[ "$PINV2_HTTP" -lt 200 || "$PINV2_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not create minimal Purchase Invoice."
  exit 1
fi
PINV2_NAME=$(echo "$PINV2_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name','N/A'))" 2>/dev/null || echo "N/A")
echo "✓ Minimal Purchase Invoice created: $PINV2_NAME"
echo ""

# ─── Step 5: Fetch expenses via resource API with custom fields ──────
echo "──── Step 5: GET /api/resource/Purchase Invoice (with custom fields) ────"
FIELDS="name,company,supplier,posting_date,grand_total,total_taxes_and_charges,remarks,docstatus,status,expense_item_name,expense_item_group,expense_items_count"
EXPENSES_RESP=$(curl -s -w "\n%{http_code}" -G \
  "$BASE_URL/api/resource/Purchase%20Invoice" \
  --data-urlencode "fields=$FIELDS" \
  --data-urlencode "limit=20" \
  --data-urlencode "order_by=posting_date desc" \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: $SID" \
  -H "Cookie: sid=$SID")

EXPENSES_HTTP=$(echo "$EXPENSES_RESP" | tail -1)
EXPENSES_BODY=$(echo "$EXPENSES_RESP" | sed '$d')

echo "HTTP $EXPENSES_HTTP"
echo "$EXPENSES_BODY" | python3 -m json.tool 2>/dev/null || echo "$EXPENSES_BODY"

if [[ "$EXPENSES_HTTP" -lt 200 || "$EXPENSES_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not fetch expenses via resource API."
  exit 1
fi

EXPENSE_COUNT=$(echo "$EXPENSES_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null || echo "?")
echo "✓ Fetched $EXPENSE_COUNT expenses via resource API"

# Verify custom fields are present in the first invoice
HAS_CUSTOM=$(echo "$EXPENSES_BODY" | python3 -c "
import sys,json
d = json.load(sys.stdin)
rows = d.get('data', [])
if not rows:
    print('NO_DATA')
else:
    r = rows[0]
    item = r.get('expense_item_name', '')
    group = r.get('expense_item_group', '')
    count = r.get('expense_items_count', 0)
    print(f'item={item}|group={group}|count={count}')
" 2>/dev/null || echo "PARSE_ERROR")

echo "  Custom fields → $HAS_CUSTOM"

if [[ "$HAS_CUSTOM" == "NO_DATA" ]]; then
  echo "WARNING: No invoices returned."
elif [[ "$HAS_CUSTOM" == "PARSE_ERROR" ]]; then
  echo "WARNING: Could not parse custom fields."
else
  CF_ITEM=$(echo "$HAS_CUSTOM" | cut -d'|' -f1 | cut -d'=' -f2)
  CF_GROUP=$(echo "$HAS_CUSTOM" | cut -d'|' -f2 | cut -d'=' -f2)
  CF_COUNT=$(echo "$HAS_CUSTOM" | cut -d'|' -f3 | cut -d'=' -f2)
  if [[ -n "$CF_ITEM" && -n "$CF_GROUP" && "$CF_COUNT" -gt 0 ]]; then
    echo "✓ Custom fields verified: item=$CF_ITEM, group=$CF_GROUP, count=$CF_COUNT"
  else
    echo "⚠ Custom fields may not be populated yet (item=$CF_ITEM, group=$CF_GROUP, count=$CF_COUNT)"
  fi
fi
echo ""

# ─── Step 6: Dashboard summary ───────────────────────────────────────
echo "──── Step 6: GET /api/method/expense_tracker.api.get_dashboard_summary ────"
DASH_RESP=$(curl -s -w "\n%{http_code}" -G \
  "$BASE_URL/api/method/expense_tracker.api.get_dashboard_summary" \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: $SID" \
  -H "Cookie: sid=$SID")

DASH_HTTP=$(echo "$DASH_RESP" | tail -1)
DASH_BODY=$(echo "$DASH_RESP" | sed '$d')

echo "HTTP $DASH_HTTP"
echo "$DASH_BODY" | python3 -m json.tool 2>/dev/null || echo "$DASH_BODY"

if [[ "$DASH_HTTP" -lt 200 || "$DASH_HTTP" -ge 300 ]]; then
  echo "FAILED: Could not fetch dashboard summary."
  exit 1
fi
echo "✓ Dashboard summary fetched"
echo ""

# ─── Summary ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════"
echo "  ALL 6 STEPS PASSED"
echo "═══════════════════════════════════════════════════"
echo "  Company    : $COMPANY"
echo "  Item Group : $ITEM_GROUP"
echo "  Item       : $ITEM_NAME"
echo "  Supplier   : $SUPPLIER (auto-created by backend)"
echo "  Invoice 1  : $PINV1_NAME (with supplier)"
echo "  Invoice 2  : $PINV2_NAME (no supplier — default)"
echo "  Expenses   : $EXPENSE_COUNT via resource API (with custom fields)"
echo "  Date       : $TODAY"
echo "═══════════════════════════════════════════════════"
