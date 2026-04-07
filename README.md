# Expense Service

This service is the backend for the Mobile Expense Tracker.

It uses a strict Frappe-style API surface and keeps the mobile payloads minimal:
- Purchase invoices are created directly.
- Enrichment happens in service hooks.
- Reference data is exposed only where needed for mobile screens.

## Runtime architecture
- Framework: `frappe_microservice`
- Core logic is implemented in `PurchaseInvoice` controller `before_save` hook
- App name: `expense_tracker`
- DB access: tenant-aware adapter from `frappe_microservice.get_app().db`
- Deployment: microservice runs behind Kong in production

## How the service works end-to-end
- Mobile creates or updates `Purchase Invoice` via resource API.
- `before_save` hook resolves:
- supplier by name and auto-creates missing suppliers
- default expense account from `Item Default` per line item
- default cost centre from `Company`
- GST tax rows from AU GST 10% template
- `POST /api/resource/Purchase Invoice` and read operations are served with Frappe resource API
- dashboard data is computed by `get_dashboard_summary` as a custom endpoint

### Important behavior
- New `Purchase Invoice` rows stay **Draft** until the user confirms; then call `POST /api/method/expense_tracker.api.submit_purchase_invoice` with JSON `{"name":"<invoice name>"}` (or `invoice_name`) to set **Submitted** (`docstatus` 1).
- Cost centre is internal and always resolved by service.
- `Item Group` is used for category-like grouping in the mobile UI.
- `get_dashboard_summary` does not take a `company` argument.
- `company` is resolved from authenticated user defaults only.

## Database access policy
- No direct SQL calls are used in service code.
- Tenant isolation is handled through the MS DB adapter.
- All reads and writes use adapter methods such as `get_all`, `get_value`, and `insert/create`.

## API endpoints
### Resource API
- `GET  /api/resource/Purchase%20Invoice`
- `POST /api/resource/Purchase%20Invoice`
- `GET  /api/resource/Purchase%20Invoice/{name}`
- `GET  /api/resource/Item%20Group`
- `POST /api/resource/Item%20Group`

Example create payloads:
```json
{
  "supplier": "Acme Pty Ltd",
  "posting_date": "2026-03-29",
  "company": "Acme Pty Ltd",
  "items": [
    {
      "item_code": "Mobile Phone Plan",
      "qty": 1,
      "rate": 100.0
    }
  ]
}
```

```json
{
  "doctype": "Item Group",
  "item_group_name": "Telecom",
  "parent_item_group": "Expenses",
  "is_group": 0
}
```

### Custom API
- `GET /api/method/expense_tracker.api.get_dashboard_summary`
- `POST /api/method/expense_tracker.api.submit_purchase_invoice` — body `{"name":"<Purchase Invoice name>"}` (draft → submitted)

## Kong
`manifests/kong/kong-configmap.yaml` exposes:
- `/api/resource/Purchase Invoice`
- `/api/resource/Item Group`
- `/api/method/expense_tracker.api.get_dashboard_summary`
- `/api/method/expense_tracker.api.submit_purchase_invoice`

## Contract notes for mobile
- Supplier creation is transparent: sending a new supplier name will create the `Supplier` record.
- Mobile does not need to send `cost_center` or tax rows.
- Tax calculation is handled inside the service via `before_save`.
- Keep line items minimal with `item_code`, `qty`, and `rate` style data.
- Item Group creation follows standard Frappe Resource API behavior (POST on `/api/resource/Item Group`).

## Tests
### TDD (pytest)
- `tests/test_purchase_invoice.py` — enrichment, custom fields, `get_dashboard_summary`, **`submit_purchase_invoice`** (draft → submit validation and DB updates)
- `tests/test_server.py` — registered resources

```bash
cd expense-service && PYTHONPATH=. pytest tests/
```

### BDD (Cypress + Cucumber)
- `cypress/e2e/features/expense_submit/expense_draft_submit.feature` — end-to-end draft create, GET docstatus, POST submit, GET submitted (requires running API + `EXPENSE_TEST_SID`)
- `cypress/e2e/features/expense_custom_fields/expense_custom_fields.feature` — spec / future steps for custom-field behaviour

```bash
cd expense-service && npm install && npx cypress run --spec 'cypress/e2e/features/expense_submit/**/*.feature' --env EXPENSE_TEST_SID=your_sid
```
