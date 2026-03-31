# Expense Service — Setup and API Guide

This document covers the full setup required to create expenses (Purchase Invoices)
via the expense service, including company account defaults, prerequisite data
(Supplier, Item Group, Item), and all curl examples.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Authentication — Login](#authentication--login)
3. [One-Time Setup — Company Default Accounts](#one-time-setup--company-default-accounts)
4. [One-Time Setup — Supplier](#one-time-setup--supplier)
5. [Creating Data — Step by Step](#creating-data--step-by-step)
   - [Step 1: Create Item Group](#step-1-create-item-group)
   - [Step 2: Create Item](#step-2-create-item)
   - [Step 3: Create Purchase Invoice (Expense)](#step-3-create-purchase-invoice-expense)
6. [Reading Data](#reading-data)
   - [Get Expense by Name](#get-expense-by-name)
   - [List Expenses](#list-expenses)
   - [Dashboard Summary](#dashboard-summary)
7. [Update and Delete](#update-and-delete)
8. [Data Model Reference](#data-model-reference)
9. [Kong Routing](#kong-routing)
10. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
Mobile / curl
      |
      v
   Kong (localhost:8000)
      |
      ├── /api/method/login           → auth-service
      ├── /api/resource/Item Group    → expense-service
      ├── /api/resource/Item          → expense-service
      ├── /api/resource/Purchase Invoice → expense-service
      ├── /api/method/expense_tracker.api.get_dashboard_summary → expense-service
      └── /api/* (fallback)           → central-site
```

The expense service is a Flask microservice (`frappe_microservice`) that:
- Authenticates via SID cookie validated against the central Frappe site.
- Uses `TenantAwareDB` for multi-tenant data isolation.
- Runs a `PurchaseInvoice` controller with a `before_save` hook that auto-resolves
  expense accounts, cost centres, suppliers, and GST tax rows.

---

## Authentication — Login

All API calls require a session ID (`sid`). Obtain one by logging in:

```bash
curl -sS -X POST "http://localhost:8000/api/method/login" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -d '{
    "usr": "your_email@example.com",
    "pwd": "your_password"
  }'
```

**Response:**

```json
{
  "message": "Logged in successfully",
  "sid": "b098f328c455fe88e03e80e343aa9f948183c877b93b11be44754a14",
  "success": true,
  "user": {
    "email": "your_email@example.com",
    "full_name": "Your Name",
    "tenant_id": "your-tenant-uuid"
  }
}
```

Use the returned `sid` in all subsequent requests via `Cookie` and `X-Frappe-SID` headers.

**Standard headers for all authenticated requests:**

```
Accept: application/json
Content-Type: application/json
X-Requested-With: XMLHttpRequest
X-Frappe-SID: <sid>
Cookie: sid=<sid>; system_user=yes; user_id=<email>; user_lang=en
```

---

## One-Time Setup — Company Default Accounts

ERPNext requires default accounting accounts on the Company before Purchase Invoices
can be created. Without these, you will see errors like:

- `Please set default Stock Received But Not Billed in Company <name>`
- `Please ensure that the Credit To account is a Balance Sheet account`

### What accounts are needed

| Company Field                    | Account Type                      | Root Type  | report_type    |
|----------------------------------|-----------------------------------|------------|----------------|
| `default_receivable_account`     | Receivable                        | Asset      | Balance Sheet  |
| `default_payable_account`        | Payable                           | Liability  | Balance Sheet  |
| `stock_received_but_not_billed`  | Stock Received But Not Billed     | Liability  | Balance Sheet  |
| `stock_adjustment_account`       | Stock Adjustment                  | Expense    | Profit and Loss|
| `default_expense_account`        | Expense Account                   | Expense    | Profit and Loss|
| `round_off_account`              | Round Off                         | Expense    | Profit and Loss|
| `write_off_account`              | (none)                            | Expense    | Profit and Loss|
| `exchange_gain_loss_account`     | (none)                            | Expense    | Profit and Loss|

### How to set them (via bench in the container)

1. Copy the setup script into the central-site container:

```bash
podman cp expense-service/scripts/setup_company_defaults.py central-site:/tmp/setup_company_defaults.py
```

2. Or create and run a script directly:

```bash
podman exec -u frappe central-site bash -c \
  'cd /home/frappe/frappe-bench && env/bin/python /tmp/setup_company_defaults.py'
```

**The script does the following:**

1. Detects the company abbreviation from existing accounts (e.g. `B8334CE0`).
2. Creates root account groups if missing: `Expenses`, `Income`, `Equity`.
   - Root accounts must be created with `flags.ignore_mandatory = True` because
     `parent_account` is mandatory but root nodes have no parent.
3. Creates sub-groups: `Current Liabilities`, `Direct Expenses`.
4. Creates leaf accounts: `Stock Received But Not Billed`, `Stock Adjustment`,
   `Miscellaneous Expenses`, `Rounded Off`, `Write Off`, `Exchange Gain or Loss`,
   `Retained Earnings`.
5. Sets `report_type` on all accounts:
   - Asset / Liability / Equity → `Balance Sheet`
   - Income / Expense → `Profit and Loss`
6. Sets all defaults on the Company doc and saves.

**Important:** Every account's `report_type` must be set correctly. If the original
accounts (Assets, Liabilities, Accounts Payable, Accounts Receivable) were created
without `report_type`, fix them:

```python
# Inside a bench script
bs_types = ("Asset", "Liability", "Equity")
for acct in frappe.get_all("Account", filters={"company": COMPANY}, fields=["name", "root_type", "report_type"]):
    expected = "Balance Sheet" if acct.root_type in bs_types else "Profit and Loss"
    if acct.report_type != expected:
        frappe.db.set_value("Account", acct.name, "report_type", expected, update_modified=False)
frappe.db.commit()
```

---

## One-Time Setup — Supplier

Purchase Invoices require a `supplier` field. Create at least one Supplier before
creating invoices.

### Via bench script

```bash
podman exec -u frappe central-site bash -c 'cd /home/frappe/frappe-bench && env/bin/python << "PYEOF"
import frappe
frappe.init(site="dev.localhost", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")

if not frappe.db.exists("Supplier Group", "Services"):
    frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "Services"}).insert(ignore_permissions=True)
    frappe.db.commit()
    print("Created Supplier Group: Services")

if not frappe.db.exists("Supplier", "Telstra"):
    frappe.get_doc({
        "doctype": "Supplier",
        "supplier_name": "Telstra",
        "supplier_group": "Services",
        "country": "Australia",
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    print("Created Supplier: Telstra")
PYEOF'
```

### Via Frappe Desk

Navigate to **Buying > Supplier > New**, fill in Supplier Name and Supplier Group, save.

---

## Creating Data — Step by Step

The dependency order is:

```
Item Group → Item (needs item_group) → Purchase Invoice (needs item_code + supplier)
```

Replace `YOUR_SID` and `YOUR_EMAIL` in all examples below.

### Step 1: Create Item Group

```bash
curl -sS -X POST "http://localhost:8000/api/resource/Item%20Group" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en" \
  -d '{
    "item_group_name": "Telephony",
    "parent_item_group": "All Item Groups",
    "is_group": 0
  }'
```

**Response:**

```json
{
  "doctype": "Item Group",
  "name": "Telephony",
  "success": true
}
```

**Notes:**
- `parent_item_group` must be an existing group. `All Item Groups` is the default
  ERPNext root and always exists.
- `is_group: 0` = leaf node (category). `is_group: 1` = can contain sub-groups.

### Step 2: Create Item

```bash
curl -sS -X POST "http://localhost:8000/api/resource/Item" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en" \
  -d '{
    "item_code": "Mobile Phone Plan",
    "item_name": "Mobile Phone Plan",
    "item_group": "Telephony",
    "stock_uom": "Nos",
    "is_stock_item": 0
  }'
```

**Response:**

```json
{
  "doctype": "Item",
  "name": "Mobile Phone Plan",
  "success": true
}
```

**Notes:**
- `item_group` must already exist (created in Step 1).
- `is_stock_item: 0` marks it as a service/non-stock item (typical for expenses).
- `stock_uom: "Nos"` = unit of measure ("Numbers"). Use `"Nos"` for services.

### Step 3: Create Purchase Invoice (Expense)

```bash
curl -sS -X POST "http://localhost:8000/api/resource/Purchase%20Invoice" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en" \
  -d '{
    "company": "thiru varasu",
    "supplier": "Telstra",
    "posting_date": "2026-03-30",
    "remarks": "March mobile plan",
    "items": [
      {
        "item_code": "Mobile Phone Plan",
        "qty": 1,
        "rate": 100.0
      }
    ]
  }'
```

**Response:**

```json
{
  "doctype": "Purchase Invoice",
  "name": "ACC-PINV-2026-00001",
  "success": true
}
```

**Notes:**
- `company` must match an existing Company with default accounts configured.
- `supplier` must exist (created in the one-time setup above).
- `item_code` in each item must reference an existing Item.
- The invoice is created as **Draft** (`docstatus: 0`).
- The `before_save` hook auto-resolves: expense account, cost centre, and GST taxes.
- `cost_center` and `expense_account` do not need to be sent by the client.

---

## Reading Data

### Get Expense by Name

```bash
curl -sS -X GET "http://localhost:8000/api/resource/Purchase%20Invoice/ACC-PINV-2026-00001" \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en"
```

### List Expenses

```bash
curl -sS -G "http://localhost:8000/api/resource/Purchase%20Invoice" \
  --data-urlencode 'fields=["name","posting_date","grand_total","status","supplier"]' \
  --data-urlencode 'limit_page_length=20' \
  --data-urlencode 'order_by=modified desc' \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en"
```

### Dashboard Summary

```bash
curl -sS -G "http://localhost:8000/api/method/expense_tracker.api.get_dashboard_summary" \
  --data-urlencode 'from_date=2026-03-01' \
  --data-urlencode 'to_date=2026-03-31' \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en"
```

**Response:**

```json
{
  "total_spend": 100.0,
  "gst_total": 10.0,
  "currency": "AUD",
  "period": "March 2026",
  "breakdown": [
    {"item_group": "Telephony", "total": 100.0}
  ]
}
```

Note: `company` is resolved from the authenticated user's defaults; it is not
passed as a parameter in the current (reverted) codebase.

---

## Update and Delete

### Update an Expense

```bash
curl -sS -X PUT "http://localhost:8000/api/resource/Purchase%20Invoice/ACC-PINV-2026-00001" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en" \
  -d '{"remarks": "Updated remarks"}'
```

### Delete an Expense

Only works if the invoice is still Draft (not submitted/cancelled):

```bash
curl -sS -X DELETE "http://localhost:8000/api/resource/Purchase%20Invoice/ACC-PINV-2026-00001" \
  -H "Accept: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "X-Frappe-SID: YOUR_SID" \
  -H "Cookie: sid=YOUR_SID; system_user=yes; user_id=YOUR_EMAIL; user_lang=en"
```

---

## Data Model Reference

```
Company (thiru varasu)
  └── has default accounts (Stock Received But Not Billed, Payable, Expense, etc.)
  └── has Suppliers (Telstra, etc.)

Item Group (Telephony, Office Supplies, ...)
  └── parent: All Item Groups

Item (Mobile Phone Plan, Printer Paper, ...)
  └── belongs to Item Group
  └── stock_uom: Nos
  └── is_stock_item: 0 (service/expense)

Purchase Invoice (the "expense")
  └── company: thiru varasu
  └── supplier: Telstra
  └── items[]: each has item_code, qty, rate
  └── auto-resolved: expense_account, cost_center, taxes
```

### Required Frappe Roles

| Operation               | Required Role(s)              |
|--------------------------|-------------------------------|
| Create/Edit Item Group   | Item Manager                  |
| Create/Edit Item         | Item Manager                  |
| Create/Edit Purchase Invoice | Accounts Manager or Accounts User |

---

## Kong Routing

The expense service routes are defined in `kong.yml`:

```yaml
- name: expense-service
  url: http://expense-service:8000
  routes:
    - name: expense-routes
      paths:
        - "/api/resource/Purchase Invoice"
        - "/api/resource/Item Group"
        - "/api/resource/Item"
        - "/api/method/expense_tracker.api.get_dashboard_summary"
        - "/api/method/expense_tracker.api.get_expense_dashboard_summary"
      strip_path: false
```

If a new DocType is registered in `server.py` via `app.register_resource(...)`,
a matching path must also be added to `kong.yml` and Kong must be reloaded:

```bash
podman exec invox-kong-1 kong reload
```

Without the Kong route, requests fall through to the `central-site` catch-all,
which returns `404: central-site does not exist`.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Session expired or invalid` | SID is stale | Login again to get a fresh SID |
| `central-site does not exist` (404) | Kong has no route for this path to the expense service | Add the path to `kong.yml` expense-routes and reload Kong |
| `Please set default Stock Received But Not Billed` | Company missing default accounts | Run the setup script (see One-Time Setup section) |
| `Credit To account is a Balance Sheet account` | Account `report_type` is not set | Set `report_type` to `Balance Sheet` for Asset/Liability/Equity accounts |
| `Could not find Item: X` | Item doesn't exist | Create the Item first (Step 2) |
| `Could not find Parent Item Group: X` | Parent Item Group doesn't exist | Use `All Item Groups` or create the parent first |
| `supplier is required` | Purchase Invoice requires a supplier | Add `"supplier": "..."` to the request body |
| `403 Permission Error` | User role lacks create/read permission for the DocType | Grant the required role (Item Manager, Accounts User, etc.) via Frappe desk |
