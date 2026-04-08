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
- New `Purchase Invoice` rows stay **Draft** until the user confirms. Typical flow: **POST** minimal body to `/api/resource/Purchase Invoice` (draft), optional **PUT** `/api/resource/Purchase Invoice/{name}` to update fields (e.g. `remarks`), then **POST** `/api/method/frappe.client.submit` with JSON `{"name":"<invoice name>"}` (or `invoice_name`, or Frappe-style `{"doc":{"doctype":"Purchase Invoice","name":"..."}}`) to run **real** submit (`docstatus` 1). See `test_expense_flow.sh` for a full curl example.
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
- `POST /api/method/frappe.client.submit` — body `{"name":"<Purchase Invoice name>"}` or `{"doc":{"doctype":"Purchase Invoice","name":"..."}}` (draft → submitted via Frappe submit)
- `DELETE /api/resource/Purchase Invoice/{name}` — removes the expense; if still **Submitted**, the service cancels it first (docstatus 2) then deletes (no separate cancel endpoint).

## Kong
`manifests/kong/kong-configmap.yaml` exposes:
- `/api/resource/Purchase Invoice`
- `/api/resource/Item Group`
- `/api/method/expense_tracker.api.get_dashboard_summary`
- `/api/method/frappe.client.submit`

## Contract notes for mobile
- Supplier creation is transparent: sending a new supplier name will create the `Supplier` record.
- Mobile does not need to send `cost_center` or tax rows.
- Tax calculation is handled inside the service via `before_save`.
- Keep line items minimal with `item_code`, `qty`, and `rate` style data.
- Item Group creation follows standard Frappe Resource API behavior (POST on `/api/resource/Item Group`).

## Tests

### Unit tests (pytest)

| File | What it covers |
|------|---------------|
| `tests/test_purchase_invoice.py` | Controller enrichment, custom fields, dashboard summary, `frappe.client.submit` wrapper, `delete_purchase_invoice` (cancel-if-submitted then delete) |
| `tests/test_server.py` | Registered resources |
| `tests/test_document_model.py` | Document-model correctness — proves child rows are `FakeChildDocument` instances, not plain dicts, and that `save()` crashes when they are |

```bash
# Run all unit tests (integration tests excluded automatically)
cd expense-service && PYTHONPATH=. pytest tests/
```

#### Document-model tests — why they exist

`MagicMock` accepts anything silently. In production, Frappe's `_set_defaults()` calls `is_new()` on every child table row during `save()`. If a row is a plain Python `dict` (as `setattr`-based code produces), this crashes with:

```
AttributeError: 'dict' object has no attribute 'is_new'
```

`tests/conftest.py` provides two test doubles that reproduce this:

- **`FakeChildDocument`** — has `is_new()`, supports both attribute and dict-style access (`row.item_code` / `row["item_code"]`)
- **`FakeDocumentController`** — converts child table dicts to `FakeChildDocument` on `append()`/`set()`, and its `save()` calls `_set_defaults()` → `is_new()` on every row, crashing on plain dicts

### Integration tests (pytest + live ERPNext)

Integration tests run against a real Frappe/ERPNext/MariaDB/Redis instance inside the `vyogo/erpnext:sne-version-16` all-in-one container — no mocks.

**Prerequisites:** Docker (or Podman) and the `frappe-microservice-lib` repo checked out as a sibling directory (`../frappe-microservice-lib`).

```
git/
├── expense-service/          ← this repo
└── frappe-microservice-lib/  ← sibling (used to install frappe-microservice inside container)
```

```bash
# Full cycle: start container → wait → bootstrap → install → test → teardown
./scripts/run_integration_tests.sh

# Keep the container running after tests (useful for debugging)
KEEP_RUNNING=1 ./scripts/run_integration_tests.sh
```

#### What the integration tests cover

| Test class | Scenarios |
|-----------|-----------|
| `TestPurchaseInvoiceCreate` | Controller sets `expense_account` and `cost_center` on items; populates `expense_item_name`, `expense_item_group`, `expense_items_count`; child rows are Document instances (not dicts); auto-creates missing supplier; auto-creates missing item; multiple items all enriched |
| `TestPurchaseInvoiceSubmit` | Draft → Submitted via `submit_purchase_invoice()`; rejects already-submitted invoice |
| `TestPurchaseInvoiceDelete` | Draft delete removes document; submitted invoice is cancelled first then deleted |
| `TestDashboardSummary` | Aggregates `grand_total` across invoices for the resolved company |
| `TestGetExpenses` | Returns paginated list with embedded item rows |

#### Infrastructure

| File | Purpose |
|------|---------|
| `docker-compose.integration.yml` | `dev-central-site` (ERPNext) + `cypress` (profile) services; mounts `.:/mnt/expense` and `../frappe-microservice-lib:/mnt/lib` |
| `scripts/run_integration_tests.sh` | Orchestration: start → wait for site → bootstrap data → install lib + pytest → Python tests → start expense-service → Cypress tests → teardown |
| `tests/integration/conftest.py` | Frappe session boot, company/accounts/fiscal year/supplier/item fixtures, `tenant_db`, `mock_app`, rollback |
| `tests/integration/pytest.ini` | Pytest config for integration run (verbosity, timeout, markers) |

### Cypress API tests (BDD + Cucumber)

Cypress runs **after** the Python integration tests using the same ERPNext container. The `cypress` service (in the `cypress` Docker Compose profile) connects to `dev-central-site` via the shared Docker network.

Authentication is handled automatically: Cypress calls `POST /api/method/login` on the Frappe instance and stores the session SID — no manual `EXPENSE_TEST_SID` injection is needed.

| Feature file | Scenarios |
|-------------|-----------|
| `expense_submit/expense_draft_submit.feature` | Draft create → GET docstatus → submit → GET submitted; submit without name → 400 |
| `expense_api/expense_full_api_flow.feature` | Full lifecycle: create → enrichment check; submit; delete draft; dashboard total_spend; submit without name → 400 |

```bash
# Run the full suite locally (Python + Cypress)
./scripts/run_integration_tests.sh

# Skip Python tests, run only Cypress
SKIP_PYTHON=1 ./scripts/run_integration_tests.sh

# Skip Cypress, run only Python
SKIP_CYPRESS=1 ./scripts/run_integration_tests.sh

# Run Cypress only (container already up)
KEEP_RUNNING=1 SKIP_PYTHON=1 ./scripts/run_integration_tests.sh
```

Environment variables recognised by the Cypress container:

| Variable | Default | Description |
|----------|---------|-------------|
| `CYPRESS_EXPENSE_SERVICE_URL` | `http://dev-central-site:9004` | Expense-service HTTP base URL |
| `CYPRESS_EXPENSE_FRAPPE_URL` | `http://dev-central-site:8000` | Frappe login endpoint |
| `CYPRESS_EXPENSE_TEST_COMPANY` | `_Test Expense Integ Co` | Company created by Python fixtures |
| `ADMIN_PASSWORD` | `admin` | Administrator password for Frappe login |

### CI/CD pipeline

```
test (unit + coverage) → integration-test (Python + Cypress + coverage) → build (image)
```

| Job | What it does |
|-----|-------------|
| **test** | Runs unit tests with `--cov` inside `ghcr.io/atxinvox/frappe-microservice-lib:latest` |
| **integration-test** | Checks out sibling `frappe-microservice-lib` repo → starts ERPNext container → installs lib → Python integration tests with `--cov` → starts expense-service on port 9004 → Cypress API tests via the `cypress` Docker service (same network) |
| **build** | Builds and pushes the container image to GHCR — only after both test jobs pass |
