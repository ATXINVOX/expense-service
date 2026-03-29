# Mobile Expense Tracker — Backend API Requirements

**Platform**: ERPNext / Frappe REST API  
**Scope**: Backend only — endpoints required to support the mobile expense tracker  
**Approach**: Use Frappe's native Resource API for all CRUD operations. Custom code is limited to one API method and one server-side hook.

---

## Authentication

- All requests use Frappe token-based authentication (API Key + API Secret)
- Pass credentials via request header: `Authorization: token {api_key}:{api_secret}`
- All resource API responses are automatically scoped to the authenticated user's permissions in ERPNext

---

## Resource API — Base Pattern

Frappe exposes all doctypes via a standard REST interface. No custom code required.

```
GET    /api/resource/{DocType}          → list records
GET    /api/resource/{DocType}/{name}   → single record
POST   /api/resource/{DocType}          → create record
PUT    /api/resource/{DocType}/{name}   → update record
```

Common query parameters available on all list endpoints:

| Parameter | Purpose | Example |
|---|---|---|
| `fields` | Specify fields to return | `["name","supplier","status"]` |
| `filters` | Filter records | `[["status","=","Unpaid"]]` |
| `limit` | Page size | `20` |
| `limit_start` | Offset for pagination | `0` |
| `order_by` | Sort order | `posting_date desc` |

---

## Endpoints

---

### 1. List Expenses

**Purpose**: Paginated list of Purchase Invoices for the Expense History screen.

**Method**: `GET`  
**Endpoint**: `/api/resource/Purchase Invoice`

**Example request**:
```
GET /api/resource/Purchase Invoice
  ?fields=["name","supplier","posting_date","status","grand_total","total_taxes_and_charges"]
  &filters=[["company","=","Acme Pty Ltd"],["status","=","Unpaid"]]
  &order_by=posting_date desc
  &limit=20
  &limit_start=0
```

**Notes**:
- Filter by `item_group` requires joining through Purchase Invoice Item — use the dashboard summary endpoint or a report for group-level filtering
- `status` accepts: `Draft`, `Unpaid`, `Paid`, `Cancelled`

---

### 2. Get Expense Detail

**Purpose**: Full detail of a single Purchase Invoice including line items and attachments.

**Method**: `GET`  
**Endpoint**: `/api/resource/Purchase Invoice/{name}`

**Example request**:
```
GET /api/resource/Purchase Invoice/ACC-PINV-2025-00042
```

**Notes**:
- Returns the full document including child table `items[]`
- Attachments are fetched separately via `/api/resource/File?filters=[["attached_to_name","=","{name}"]]`

---

### 3. Create Expense

**Purpose**: Create a new Purchase Invoice from the mobile Add Expense form.

**Method**: `POST`  
**Endpoint**: `/api/resource/Purchase Invoice`

**Request body**:
```json
{
  "company": "Acme Pty Ltd",
  "supplier": "Telstra",
  "posting_date": "2025-07-15",
  "items": [
    {
      "item_code": "Mobile Phone Plan",
      "qty": 1,
      "rate": 100.00,
      "cost_center": "Operations - Acme"
    }
  ],
  "remarks": "July mobile plan"
}
```

**Notes**:
- `expense_account` and `taxes` are resolved automatically server-side via the `before_save` hook (see Server-Side Hooks section) — the client does not send them
- Invoice is created as `Draft` by default
- To submit immediately, call `PUT /api/resource/Purchase Invoice/{name}` with `{"docstatus": 1}` after creation

---

### 4. Get Supplier List

**Purpose**: Searchable supplier list for the Add Expense form dropdown.

**Method**: `GET`  
**Endpoint**: `/api/resource/Supplier`

**Example request**:
```
GET /api/resource/Supplier
  ?fields=["name","supplier_group"]
  &filters=[["supplier_name","like","%telstra%"]]
  &limit=20
```

---

### 5. Create Supplier

**Purpose**: Create a new Supplier inline when one does not exist.

**Method**: `POST`  
**Endpoint**: `/api/resource/Supplier`

**Request body**:
```json
{
  "supplier_name": "Origin Energy",
  "supplier_group": "Utilities",
  "country": "Australia"
}
```

---

### 6. Get Item List

**Purpose**: Searchable item list for the Add Expense form dropdown.

**Method**: `GET`  
**Endpoint**: `/api/resource/Item`

**Example request**:
```
GET /api/resource/Item
  ?fields=["name","item_group","item_name"]
  &filters=[["is_stock_item","=",0],["item_name","like","%fuel%"]]
  &limit=20
```

**Notes**:
- `is_stock_item = 0` filters to service/expense items only
- `expense_account` and GST details are resolved server-side on invoice save — no need to fetch them on the client

---

### 7. Get Item Groups

**Purpose**: Item Group list for filter dropdowns on the Expense History screen.

**Method**: `GET`  
**Endpoint**: `/api/resource/Item Group`

**Example request**:
```
GET /api/resource/Item Group
  ?fields=["name"]
  &filters=[["is_group","=",0]]
```

---

### 8. Get Cost Centres

**Purpose**: Cost Centre list for the Add Expense form.

**Method**: `GET`  
**Endpoint**: `/api/resource/Cost Center`

**Example request**:
```
GET /api/resource/Cost Center
  ?fields=["name","cost_center_name"]
  &filters=[["company","=","Acme Pty Ltd"],["is_group","=",0]]
```

---

### 9. Attach Receipt

**Purpose**: Upload a receipt image or PDF and attach it to a Purchase Invoice.

**Method**: `POST`  
**Endpoint**: `/api/method/upload_file`  
**Content-Type**: `multipart/form-data`

**Form fields**:
| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | binary | Yes | JPEG, PNG, or PDF |
| `doctype` | string | Yes | `Purchase Invoice` |
| `docname` | string | Yes | e.g. `ACC-PINV-2025-00042` |
| `is_private` | integer | No | `1` to restrict access |

**Notes**:
- This is a Frappe built-in method — no custom code required
- File is stored in ERPNext file manager and linked to the Purchase Invoice automatically

---

### 10. Get Dashboard Summary *(only custom endpoint required)*

**Purpose**: Return aggregated spend totals and breakdown by Item Group for the Home screen. Cannot be achieved via the Resource API alone as it requires aggregation across Purchase Invoice and Purchase Invoice Item.

**Method**: `GET`  
**Endpoint**: `/api/method/expense_tracker.api.get_dashboard_summary`

**Query parameters**:
| Parameter | Type | Required | Notes |
|---|---|---|---|
| `company` | string | Yes | ERPNext Company name |
| `from_date` | date | No | Defaults to first day of current month |
| `to_date` | date | No | Defaults to today |

**Response**:
```json
{
  "total_spend": 4250.00,
  "gst_total": 386.36,
  "currency": "AUD",
  "period": "July 2025",
  "breakdown": [
    { "item_group": "Transport & Fuel", "total": 1200.00 },
    { "item_group": "Telephony", "total": 350.00 },
    { "item_group": "Office Expenses", "total": 2700.00 }
  ]
}
```

---

## Server-Side Hooks *(only custom hook required)*

### GST & Expense Account Auto-resolution

**Trigger**: `before_save` on Purchase Invoice  
**Purpose**: Resolve `expense_account` and `taxes` from Item defaults so the mobile client does not need to send them.

**Logic**:
1. For each line item in `items[]`, look up the Item's default `expense_account` from Item Defaults filtered by Company
2. Look up the Item's applicable tax template from Item Tax
3. Apply the `AU GST 10%` Purchase Taxes and Charges Template if the Item has GST
4. Populate `taxes[]` on the Purchase Invoice accordingly

**Notes**:
- This hook already exists partially in the AU Localisation App — confirm before writing new code
- If the Item has no tax template, no GST is applied (e.g. GST-free suppliers)

---

## Error Handling

Frappe Resource API returns consistent error responses natively:

```json
{
  "exc_type": "DoesNotExistError",
  "exception": "Purchase Invoice ACC-PINV-2025-00099 does not exist",
  "_server_messages": "..."
}
```

| HTTP Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request — missing or invalid parameters |
| 401 | Unauthenticated |
| 403 | Insufficient permissions |
| 404 | Record not found |
| 500 | Server / validation error |

The mobile client should handle 403 and 404 explicitly — these are the most common in a scoped SME environment where users may have restricted roles.

---

## Summary

| Endpoint | Type | Custom Code? |
|---|---|---|
| List expenses | Resource API | No |
| Get expense detail | Resource API | No |
| Create expense | Resource API | No |
| List suppliers | Resource API | No |
| Create supplier | Resource API | No |
| List items | Resource API | No |
| List item groups | Resource API | No |
| List cost centres | Resource API | No |
| Attach receipt | `/api/method/upload_file` | No |
| Dashboard summary | Custom API method | Yes — 1 method |
| GST auto-resolution | `before_save` hook | Yes — 1 hook |

**Custom backend work is limited to one API method and one server-side hook.** All other operations use Frappe's native Resource API with no additional development required.

---

## Out of Scope (v1)

- Payment Entry creation on mobile
- Journal Entry creation on mobile
- Expense Claim flow (employee reimbursements)
- Multi-company switching
- ABN lookup or supplier verification
- Approval workflow push notifications
- Cancellation and amendment of submitted invoices
