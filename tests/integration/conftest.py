"""
Fixtures for expense-service integration tests.

Runs INSIDE the vyogo/erpnext:sne-version-16 container (via docker compose exec)
where real Frappe, ERPNext, MariaDB, and Redis are all available.

The parent tests/conftest.py (which has no Frappe imports) is excluded via
--confcutdir when invoking pytest — see scripts/run_integration_tests.sh.

Session-scoped setup:
  frappe_session   — boots Frappe once for the entire test run
  test_company     — finds/creates the integration test Company
  test_accounts    — discovers expense account, cost center, payable account
  ensure_fiscal_year — ensures a Fiscal Year exists for today
  test_supplier    — ensures a reusable Supplier exists
  test_item        — ensures a reusable Item exists
  mock_app         — patches get_app() so expense-service code uses real TenantAwareDB

Function-scoped:
  tenant_db        — TenantAwareDB bound to TEST_TENANT_ID
  _rollback        — rolls back DB after every test (autouse)
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import frappe
from frappe_microservice.tenant import TenantAwareDB, patch_valid_dict_for_tenant_id

TEST_TENANT_ID = "expense-integ-tenant-001"
TEST_COMPANY   = "_Test Expense Integ Co"
TEST_COMPANY_ABBR = "TEIC"

BENCH_PATH         = "/home/frappe/frappe-bench"
DEFAULT_SITES_PATH = os.path.join(BENCH_PATH, "sites")
EXPENSE_MOUNT      = "/mnt/expense"


def _discover_site():
    site = os.environ.get("FRAPPE_SITE")
    if site:
        return site
    sites_path = os.environ.get("FRAPPE_SITES_PATH", DEFAULT_SITES_PATH)
    currentsite = os.path.join(sites_path, "currentsite.txt")
    if os.path.exists(currentsite):
        with open(currentsite) as f:
            name = f.read().strip()
            if name:
                return name
    for candidate in ("dev.localhost", "frontend", "site1.local"):
        if os.path.isdir(os.path.join(sites_path, candidate)):
            return candidate
    return "dev.localhost"


def _ensure_column(doctype, column, coltype="varchar(140)"):
    try:
        frappe.db.sql(f"ALTER TABLE `tab{doctype}` ADD COLUMN `{column}` {coltype}")
    except Exception:
        pass


def _add_expense_python_path():
    """Make controllers/ and expense_tracker/ importable from /mnt/expense."""
    if EXPENSE_MOUNT not in sys.path:
        sys.path.insert(0, EXPENSE_MOUNT)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def frappe_session():
    """Boot Frappe once for the entire test session."""
    _add_expense_python_path()

    sites_path = os.environ.get("FRAPPE_SITES_PATH", DEFAULT_SITES_PATH)
    site = _discover_site()

    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user("Administrator")
    frappe.flags.in_test = True

    # Ensure tenant_id column exists on all relevant tables
    for dt in (
        "Purchase Invoice",
        "Purchase Invoice Item",
        "Purchase Taxes and Charges",
        "Supplier",
        "Item",
        "Item Group",
        "Cost Center",
        "Account",
        "Company",
    ):
        _ensure_column(dt, "tenant_id")

    # Ensure expense-service custom columns exist
    for col, coltype in (
        ("expense_item_name",  "varchar(140)"),
        ("expense_item_group", "varchar(140)"),
        ("expense_items_count","int(11) DEFAULT 0"),
    ):
        _ensure_column("Purchase Invoice", col, coltype)

    patch_valid_dict_for_tenant_id()
    frappe.db.commit()

    yield

    frappe.destroy()


@pytest.fixture(scope="session")
def test_company(frappe_session):
    """Create the integration test Company if it doesn't exist."""
    if not frappe.db.exists("Company", TEST_COMPANY):
        company = frappe.get_doc({
            "doctype": "Company",
            "company_name": TEST_COMPANY,
            "abbr": TEST_COMPANY_ABBR,
            "default_currency": "AUD",
            "country": "Australia",
        })
        company.insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.commit()

    # TenantAwareDB injects tenant_id into all queries; set it on the company
    # so controller lookups (company abbr, default accounts) can find it.
    frappe.db.set_value("Company", TEST_COMPANY, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()

    return TEST_COMPANY


@pytest.fixture(scope="session")
def ensure_fiscal_year(frappe_session, test_company):
    """Ensure a Fiscal Year covering today's date exists."""
    today = date.today()
    existing = frappe.db.sql(
        "SELECT name FROM `tabFiscal Year` "
        "WHERE year_start_date <= %s AND year_end_date >= %s LIMIT 1",
        (today, today), as_dict=True,
    )
    if existing:
        return existing[0]["name"]

    fy_start = today.replace(month=7, day=1) if today.month >= 7 \
               else today.replace(year=today.year - 1, month=7, day=1)
    fy_end   = fy_start.replace(year=fy_start.year + 1, month=6, day=30)
    fy_name  = f"{fy_start.year}-{fy_end.year}"

    if not frappe.db.exists("Fiscal Year", fy_name):
        fy = frappe.get_doc({
            "doctype": "Fiscal Year",
            "year": fy_name,
            "year_start_date": str(fy_start),
            "year_end_date":   str(fy_end),
        })
        fy.insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.sql(
            "INSERT IGNORE INTO `tabFiscal Year Company` "
            "(name, parent, parenttype, parentfield, company, "
            " creation, modified, modified_by, owner, docstatus, idx) "
            "VALUES (%s, %s, 'Fiscal Year', 'companies', %s, "
            " NOW(), NOW(), 'Administrator', 'Administrator', 0, 1)",
            (f"{fy_name}-{test_company}", fy_name, test_company),
        )
        frappe.db.commit()

    return fy_name


@pytest.fixture(scope="session")
def test_accounts(frappe_session, test_company):
    """Ensure minimum chart of accounts exists for the test company."""
    abbr = TEST_COMPANY_ABBR

    def _ensure_account(name, account_name, root_type, report_type,
                        account_type=None, parent=None, is_group=0):
        if not frappe.db.exists("Account", name):
            data = {
                "doctype": "Account",
                "account_name": account_name,
                "company": test_company,
                "root_type": root_type,
                "report_type": report_type,
                "is_group": is_group,
            }
            if account_type:
                data["account_type"] = account_type
            if parent:
                data["parent_account"] = parent
            doc = frappe.get_doc(data)
            doc.insert(ignore_permissions=True, ignore_mandatory=True)
        elif account_type:
            # ERPNext bootstrap may have created the account without account_type;
            # ensure it is always set to the expected value.
            current = frappe.db.get_value("Account", name, "account_type")
            if current != account_type:
                frappe.db.set_value("Account", name, "account_type", account_type)
        return name

    # Root accounts (groups)
    assets_root    = _ensure_account(f"Assets - {abbr}",      "Assets",      "Asset",     "Balance Sheet",       is_group=1)
    liability_root = _ensure_account(f"Liabilities - {abbr}", "Liabilities", "Liability", "Balance Sheet",       is_group=1)
    expense_root   = _ensure_account(f"Expenses - {abbr}",    "Expenses",    "Expense",   "Profit and Loss",     is_group=1)

    # Leaf accounts
    payable  = _ensure_account(
        f"Accounts Payable - {abbr}", "Accounts Payable",
        "Liability", "Balance Sheet", account_type="Payable",
        parent=liability_root,
    )
    expense  = _ensure_account(
        f"General Expenses - {abbr}", "General Expenses",
        "Expense", "Profit and Loss", account_type="Expense Account",
        parent=expense_root,
    )

    # Set defaults on the company
    frappe.db.set_value("Company", test_company, {
        "default_payable_account": payable,
        "default_expense_account": expense,
    })
    frappe.db.commit()

    # TenantAwareDB injects tenant_id into all queries; tag key records so
    # controller lookups (_company_abbr, expense account, cost center) find them.
    for acct in (payable, expense):
        frappe.db.set_value("Account", acct, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()

    # Cost center
    cc_root = f"{test_company} - {abbr}"
    if not frappe.db.exists("Cost Center", cc_root):
        frappe.get_doc({
            "doctype": "Cost Center",
            "cost_center_name": test_company,
            "company": test_company,
            "is_group": 1,
        }).insert(ignore_permissions=True, ignore_mandatory=True)

    cc_main = f"Main - {abbr}"
    if not frappe.db.exists("Cost Center", cc_main):
        frappe.get_doc({
            "doctype": "Cost Center",
            "cost_center_name": "Main",
            "company": test_company,
            "parent_cost_center": cc_root,
            "is_group": 0,
        }).insert(ignore_permissions=True, ignore_mandatory=True)

    frappe.db.set_value("Company", test_company, "cost_center", cc_main)
    frappe.db.commit()

    # Tag cost centers with tenant_id so TenantAwareDB can find them
    for cc in (cc_root, cc_main):
        frappe.db.set_value("Cost Center", cc, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()

    return {
        "expense_account": expense,
        "cost_center":     cc_main,
        "payable_account": payable,
    }


@pytest.fixture(scope="session")
def test_supplier(frappe_session, test_company):
    """Ensure a reusable test Supplier exists."""
    name = "_Test Expense Supplier"
    if not frappe.db.exists("Supplier", name):
        sg = frappe.get_all("Supplier Group", limit=1, pluck="name")
        frappe.get_doc({
            "doctype": "Supplier",
            "supplier_name": name,
            "supplier_group": sg[0] if sg else "All Supplier Groups",
        }).insert(ignore_permissions=True)
    frappe.db.set_value("Supplier", name, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()
    return name


@pytest.fixture(scope="session")
def test_item(frappe_session):
    """Ensure a reusable test Item exists."""
    name = "_Test Expense Item"
    if not frappe.db.exists("Item", name):
        frappe.get_doc({
            "doctype": "Item",
            "item_code": name,
            "item_name": name,
            "item_group": "All Item Groups",
            "is_stock_item": 0,
            "is_purchase_item": 1,
            "is_sales_item": 0,
        }).insert(ignore_permissions=True)
    frappe.db.set_value("Item", name, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()
    return name


# ---------------------------------------------------------------------------
# Function-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tenant_db(frappe_session):
    """TenantAwareDB scoped to the integration test tenant."""
    return TenantAwareDB(lambda: TEST_TENANT_ID)


@pytest.fixture
def mock_app(tenant_db, test_company):
    """Patch frappe_microservice.get_app() so expense-service code uses real TenantAwareDB.

    Also sets frappe.session.user to Administrator with a DefaultValue pointing
    to the test company, so _resolve_company_from_user() works without Flask.
    """
    frappe.set_user("Administrator")

    # Set DefaultValue so _resolve_company_from_user() returns test_company
    if not frappe.db.get_value(
        "DefaultValue",
        {"parent": "Administrator", "defkey": "company"},
        "name",
    ):
        frappe.get_doc({
            "doctype": "DefaultValue",
            "parent": "Administrator",
            "parenttype": "User",
            "parentfield": "defaults",
            "defkey": "company",
            "defvalue": test_company,
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    else:
        frappe.db.set_value(
            "DefaultValue",
            {"parent": "Administrator", "defkey": "company"},
            "defvalue", test_company,
        )
        frappe.db.commit()

    app_mock = MagicMock()
    app_mock.tenant_db = tenant_db

    # Ensure the module-level _registry in frappe_microservice.controller points
    # at the shared frappe._microservice_registry so controller hooks can find
    # the PurchaseInvoice controller class (server.py normally does this).
    import frappe_microservice.controller as _ctrl_module
    from frappe_microservice.controller import setup_controllers as _setup_controllers
    _ctrl_module._registry = _ctrl_module.get_controller_registry()
    _setup_controllers(app_mock, controllers_directory=os.path.join(EXPENSE_MOUNT, "controllers"))

    with patch("frappe_microservice.get_app", return_value=app_mock), \
         patch("expense_tracker.api.get_app", return_value=app_mock), \
         patch("controllers.purchase_invoice.get_app", return_value=app_mock):
        yield app_mock


@pytest.fixture(autouse=True)
def _rollback():
    """Roll back all uncommitted changes after each test."""
    yield
    frappe.db.rollback()
