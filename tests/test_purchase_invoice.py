from datetime import date
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch
import pytest
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MockDocumentController:
    def __init__(self, data):
        for key, value in data.items():
            setattr(self, key, value)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, value):
        if not hasattr(self, key):
            setattr(self, key, [])
        getattr(self, key).append(value)

    def get(self, key, default=None):
        return getattr(self, key, default)


mock_frappe = MagicMock()
mock_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)

mock_app = MagicMock()
mock_app.db = MagicMock()
mock_app.db.exists.return_value = False
mock_app.tenant_db = mock_app.db

mock_microservice = MagicMock()
mock_microservice_controller = MagicMock()
mock_microservice_controller.DocumentController = MockDocumentController
mock_microservice.get_app.return_value = mock_app


def mock_secure_route(rule, **options):
    def decorator(f):
        return f
    return decorator


mock_app.secure_route.side_effect = mock_secure_route

sys.modules["frappe"] = mock_frappe
sys.modules["frappe_microservice"] = mock_microservice
sys.modules["frappe_microservice.controller"] = mock_microservice_controller


def _configure_frappe_throw():
    """Make frappe.throw raise so submit_purchase_invoice errors are testable."""
    mock_frappe.ValidationError = type("ValidationError", (Exception,), {})
    mock_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    mock_frappe.PermissionError = type("PermissionError", (Exception,), {})

    def throw_fn(msg, exc=None, *args, **kwargs):
        if exc is not None:
            raise exc(msg)
        raise RuntimeError(msg)

    mock_frappe.throw = throw_fn


_configure_frappe_throw()

# submit_purchase_invoice imports flask.request at runtime; provide a stub so tests run without Flask installed.
if "flask" not in sys.modules:
    _flask_stub = ModuleType("flask")
    _flask_stub.request = MagicMock()
    sys.modules["flask"] = _flask_stub

from controllers.purchase_invoice import PurchaseInvoice, _expense_title
from expense_tracker.api import (
    cancel_purchase_invoice,
    get_dashboard_summary,
    submit_purchase_invoice,
    _app_db,
)


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_app.db.reset_mock()
    mock_app.db.get_all.side_effect = None
    mock_app.db.get_value.side_effect = None
    mock_app.db.exists.return_value = False
    mock_app.tenant_db.reset_mock()
    mock_frappe.db.reset_mock()
    mock_frappe.get_all.reset_mock()
    mock_frappe.get_doc.reset_mock()
    mock_app.tenant_db.get_value.side_effect = lambda *args, **kwargs: mock_frappe.db.get_value(*args, **kwargs)
    mock_app.tenant_db.get_all.side_effect = lambda *args, **kwargs: mock_frappe.get_all(*args, **kwargs)
    if "flask" in sys.modules and hasattr(sys.modules["flask"], "request"):
        sys.modules["flask"].request.reset_mock()


def _default_get_value(doctype, filters, field=None):
    """Shared get_value mock used by most tests."""
    if doctype == "DefaultValue":
        # _resolve_company_from_user: no user default → fall back to doc company
        return None
    if doctype == "Item":
        # _resolve_item_code: item already exists — return it as-is
        return filters
    if doctype == "Item Default":
        parent = filters.get("parent") if isinstance(filters, dict) else filters
        if parent == "Fuel":
            return "10000 - Fuel"
        if parent == "Telephony":
            return "12000 - Telephony"
        return None
    if doctype == "Company":
        company = filters if isinstance(filters, str) else None
        if company == "Acme Pty Ltd" and field == "cost_center":
            return "Main - CC"
        return None
    if doctype == "Buying Settings":
        return "General"
    if doctype == "Supplier":
        return None
    return None


GST_TEMPLATE_NAME = "AU Non Capital Purchase - GST - ATX"


def _default_get_all(doctype, filters=None, fields=None, *args, **kwargs):
    """Shared get_all mock: returns GST template name and its rows."""
    if doctype == "Purchase Taxes and Charges Template":
        # _find_gst_template pattern query
        return [{"name": GST_TEMPLATE_NAME}]
    if doctype in ("Purchase Taxes and Charges", "Purchase Taxes and Charges Template Detail"):
        parent = (filters or {}).get("parent") if isinstance(filters, dict) else None
        if parent == GST_TEMPLATE_NAME:
            return [
                {
                    "charge_type": "On Net Total",
                    "account_head": "GST Payable - AC",
                    "description": GST_TEMPLATE_NAME,
                    "rate": 10,
                    "cost_center": None,
                    "included_in_print_rate": 0,
                    "add_deduct_tax": "Add",
                }
            ]
    return []


def _supplier_fallback_values(doctype, filters=None, field=None):
    return _default_get_value(doctype, filters, field)


def test_purchase_invoice_before_validate_sets_default_accounts_and_taxes():
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "taxes_and_charges": "AU GST 10%",  # truthy → wants GST
            "items": [
                {"item_code": "Fuel", "expense_account": None, "rate": 100.0},
                {"item_code": "Telephony", "expense_account": None, "rate": 100.0},
            ],
        }
    )

    doc.before_validate()

    assert doc.items[0]["expense_account"] == "10000 - Fuel"
    assert doc.items[1]["expense_account"] == "12000 - Telephony"
    assert doc.items[0]["cost_center"] == "Main - CC"
    assert doc.items[1]["cost_center"] == "Main - CC"
    assert len(doc.taxes) == 1
    assert doc.taxes[0]["description"] == GST_TEMPLATE_NAME
    assert doc.taxes_and_charges == GST_TEMPLATE_NAME


def test_purchase_invoice_without_gst_keeps_non_gst_taxes():
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "taxes_and_charges": "",  # falsy → no GST
            "items": [{"item_code": "Telephony", "expense_account": None}],
            "taxes": [{"charge_type": "On Net Total", "description": "Freight", "rate": 5}],
        }
    )

    doc.before_validate()

    assert len(doc.taxes) == 1
    assert doc.taxes[0]["description"] == "Freight"


def test_purchase_invoice_internal_cost_center_is_forced():
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "items": [{"item_code": "Fuel", "cost_center": "User Selected"}],
        }
    )

    doc.before_validate()

    assert doc.items[0]["cost_center"] == "Main - CC"


def test_purchase_invoice_auto_creates_supplier():
    mock_supplier_doc = MagicMock()
    mock_supplier_doc.name = "SUP-NEW"
    mock_app.db.insert_doc.return_value = mock_supplier_doc

    def db_get_value(doctype, filters=None, field=None):
        if doctype == "DefaultValue":
            return None
        if doctype == "Item":
            return filters  # items exist
        if doctype == "Supplier":
            return None  # supplier does not exist → auto-create
        if doctype == "Buying Settings":
            return "General"
        if doctype == "Company" and filters == "Acme Pty Ltd":
            if field == "cost_center":
                return "Main - CC"
            return None
        if doctype == "Item Default":
            return "10000 - Fuel"
        return None

    mock_frappe.db.get_value.side_effect = db_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "supplier": "New Supplier",
            "items": [{"item_code": "Fuel", "expense_account": None}],
        }
    )

    doc.before_validate()

    assert doc.supplier == "SUP-NEW"
    call_args = mock_app.db.insert_doc.call_args
    created_data = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get('data', {})
    assert created_data["supplier_name"] == "New Supplier"
    assert created_data["supplier_group"] == "General"


def test_purchase_invoice_auto_creates_item():
    def insert_doc_side_effect(doctype, data, **kwargs):
        doc = MagicMock()
        doc.name = data.get("name", doctype)
        return doc

    mock_app.db.insert_doc.side_effect = insert_doc_side_effect

    def db_get_value(doctype, filters=None, field=None):
        if doctype == "DefaultValue":
            return None
        if doctype == "Item":
            return None  # item does not exist → auto-create
        if doctype == "Item Group":
            return None  # item group does not exist → auto-create
        if doctype == "Company" and filters == "Acme Pty Ltd" and field == "cost_center":
            return "Main - CC"
        if doctype == "Buying Settings":
            return "General"
        if doctype == "Supplier":
            return "BP"
        return None

    mock_frappe.db.get_value.side_effect = db_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "supplier": "BP",
            "items": [{"item_code": "fuel", "item_group": "Travel", "expense_account": None}],
        }
    )

    doc.before_validate()

    item_insert = next(
        call for call in mock_app.db.insert_doc.call_args_list if call.args and call.args[0] == "Item"
    )
    created_data = item_insert.args[1] if len(item_insert.args) > 1 else item_insert.kwargs.get('data', {})
    assert created_data["name"] == "fuel"
    assert created_data["item_group"] == "Travel"
    assert created_data["is_purchase_item"] == 1
    assert doc.items[0]["item_code"] == "fuel"


def test_purchase_invoice_company_resolved_from_session():
    def db_get_value(doctype, filters=None, field=None):
        if doctype == "DefaultValue":
            return "Session Company Pty Ltd"  # user default company
        if doctype == "Item":
            return filters
        if doctype == "Company" and filters == "Session Company Pty Ltd" and field == "cost_center":
            return "Session CC"
        return None

    mock_frappe.db.get_value.side_effect = db_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Wrong Company",  # should be overridden
            "items": [{"item_code": "Fuel", "expense_account": None}],
        }
    )

    doc.before_validate()

    assert doc.company == "Session Company Pty Ltd"
    assert doc.items[0]["cost_center"] == "Session CC"


def test_dashboard_summary_returns_aggregates():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_app.db.get_all.side_effect = [
        [
            {"name": "PI-1", "grand_total": 1400.0, "total_taxes_and_charges": 140.0},
            {"name": "PI-2", "grand_total": 1000.0, "total_taxes_and_charges": 78.18},
        ],
        [
            {"item_group": "Fuel", "amount": 1400.0},
            {"item_group": "Telephony", "amount": 1000.0},
        ],
    ]
    mock_app.db.get_value.return_value = "AUD"

    result = get_dashboard_summary("test_user")

    assert result["total_spend"] == 2400.0
    assert result["gst_total"] == 218.18
    assert result["currency"] == "AUD"
    assert result["period"] == date.today().strftime("%B %Y")
    assert len(result["breakdown"]) == 2
    assert result["breakdown"][0]["item_group"] == "Fuel"


def test_dashboard_summary_uses_user_default_company():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_app.db.get_value.return_value = "AUD"
    mock_app.db.get_all.side_effect = [
        [{"name": "PI-1", "grand_total": 10.0, "total_taxes_and_charges": 1.0}],
        [],
    ]

    result = get_dashboard_summary("test_user")

    assert result["currency"] == "AUD"
    # Verify that the correct tenant-aware DB was used
    assert _app_db() == mock_app.tenant_db
    
    first_query_params = mock_app.tenant_db.get_all.call_args_list[0].kwargs.get("filters")
    assert first_query_params == [
        ["company", "=", "Acme Pty Ltd"],
        ["docstatus", "<", 2],
        ["posting_date", ">=", date.today().replace(day=1)],
        ["posting_date", "<=", date.today()],
    ]


def test_dashboard_summary_falls_back_to_session_user_default():
    """When frappe.defaults is unavailable, resolve company from
    frappe.session.user via the DefaultValue table (raw frappe.db)."""
    mock_frappe.defaults = None
    mock_frappe.session = MagicMock()
    mock_frappe.session.user = "varun@vyogolabs.tech"

    def frappe_db_get_value(doctype, filters, field=None):
        if doctype == "DefaultValue":
            return "Session Co Pty Ltd"
        return None

    mock_frappe.db.get_value.side_effect = frappe_db_get_value
    mock_app.db.get_value.side_effect = lambda doctype, filters, field=None: "AUD" if doctype == "Company" else None
    mock_app.db.get_all.side_effect = [
        [{"name": "PI-1", "grand_total": 50.0, "total_taxes_and_charges": 5.0}],
        [],
    ]

    result = get_dashboard_summary("varun@vyogolabs.tech")

    assert result["total_spend"] == 50.0
    assert result["currency"] == "AUD"

    first_query_filters = mock_app.tenant_db.get_all.call_args_list[0].kwargs.get("filters")
    assert first_query_filters[0] == ["company", "=", "Session Co Pty Ltd"]


def test_purchase_invoice_bootstraps_missing_cost_center():
    # Setup: Company has no default cost center
    def db_get_value(doctype, filters, field=None):
        if doctype == "Company" and filters == "Acme Pty Ltd":
            if field == "cost_center": return None
            if field == "abbr": return "ACME"
        return None

    mock_frappe.db.get_value.side_effect = db_get_value
    mock_frappe.get_all.return_value = [] # No existing cost centers
    mock_app.db.exists.return_value = False
    
    # Clear side_effect from previous tests, then set return_value
    mock_app.db.insert_doc.side_effect = None
    mock_cc_doc = MagicMock()
    mock_cc_doc.name = "Main - ACME"
    mock_app.db.insert_doc.return_value = mock_cc_doc

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [{"item_code": "Fuel"}]
    })

    # Action
    doc.before_validate()

    # Verify: Root and Main CCs should be created
    assert mock_app.db.insert_doc.call_count >= 2
    
    # Verify: Company should be updated with Main CC
    mock_app.db.set_value.assert_any_call("Company", "Acme Pty Ltd", "cost_center", "Main - ACME")


# ── Custom field (expense_item_name / expense_item_group / expense_items_count) tests ──


def test_expense_title_maps_to_what_was_bought():
    assert _expense_title("Milk 2L", 1, None) == "Milk 2L"
    assert _expense_title("Milk 2L", 3, None) == "Milk 2L (+2 more)"
    assert _expense_title("", 0, "Pantry restock") == "Pantry restock"
    assert _expense_title(None, 0, "") == ""


def test_custom_fields_populated_from_single_item():
    """When a PI has one item, the custom fields mirror that item."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [{"item_code": "Fuel", "item_group": "Travel", "rate": 80.0}],
    })

    doc.before_validate()

    assert doc.expense_item_name == "Fuel"
    assert doc.expense_item_group == "Travel"
    assert doc.expense_items_count == 1
    assert doc.title == "Fuel"


def test_custom_fields_populated_from_first_of_multiple_items():
    """When a PI has multiple items, custom fields use the first item."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [
            {"item_code": "Fuel", "item_group": "Travel", "rate": 80.0},
            {"item_code": "Telephony", "item_group": "Office", "rate": 45.0},
        ],
    })

    doc.before_validate()

    assert doc.expense_item_name == "Fuel"
    assert doc.expense_item_group == "Travel"
    assert doc.expense_items_count == 2
    assert doc.title == "Fuel (+1 more)"


def test_custom_fields_empty_when_no_items():
    """When a PI has no items, custom fields default to empty."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [],
    })

    doc.before_validate()

    assert doc.expense_item_name == ""
    assert doc.expense_item_group == ""
    assert doc.expense_items_count == 0


def test_custom_fields_default_item_group_when_missing():
    """When item_group is absent, expense_item_group falls back to 'All Item Groups'."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [{"item_code": "Stationery", "rate": 12.0}],
    })

    doc.before_validate()

    assert doc.expense_item_name == "Stationery"
    assert doc.expense_item_group == "All Item Groups"
    assert doc.expense_items_count == 1


def test_custom_fields_survive_gst_enrichment():
    """Custom fields are set even when GST template enrichment runs."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "taxes_and_charges": "AU GST 10%",
        "items": [{"item_code": "Fuel", "item_group": "Travel", "rate": 100.0}],
    })

    doc.before_validate()

    assert doc.expense_item_name == "Fuel"
    assert doc.expense_item_group == "Travel"
    assert doc.expense_items_count == 1
    assert len(doc.taxes) == 1


def test_custom_fields_with_no_company_returns_early():
    """When company can't be resolved, custom fields are not set (early return)."""
    mock_frappe.db.get_value.return_value = None
    mock_frappe.get_all.return_value = []

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "items": [{"item_code": "Fuel", "rate": 80.0}],
    })

    doc.before_validate()

    assert not hasattr(doc, "expense_item_name") or doc.get("expense_item_name") is None


def test_custom_fields_queryable_via_resource_api_fields():
    """Custom fields are standard attributes, accessible like any field on the doc."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice({
        "doctype": "Purchase Invoice",
        "company": "Acme Pty Ltd",
        "items": [{"item_code": "Printer Paper", "item_group": "Office Supplies", "rate": 25.0}],
    })

    doc.before_validate()

    resource_fields = {
        "expense_item_name": doc.expense_item_name,
        "expense_item_group": doc.expense_item_group,
        "expense_items_count": doc.expense_items_count,
    }
    assert resource_fields["expense_item_name"] == "Printer Paper"
    assert resource_fields["expense_item_group"] == "Office Supplies"
    assert resource_fields["expense_items_count"] == 1


# ── submit_purchase_invoice (draft → confirm → submitted) ────────────────────


def test_submit_purchase_invoice_success():
    sys.modules["flask"].request.get_json.return_value = {"name": "ACC-PINV-2026-00001"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = [
        {
            "docstatus": 0,
            "company": "Acme Pty Ltd",
            "expense_item_name": "Fuel",
            "expense_items_count": 1,
            "remarks": None,
        },
        "Submitted",
    ]

    result = submit_purchase_invoice("user@example.com")

    assert result == {
        "success": True,
        "name": "ACC-PINV-2026-00001",
        "docstatus": 1,
        "status": "Submitted",
    }
    mock_frappe.db.set_value.assert_called_with(
        "Purchase Invoice",
        "ACC-PINV-2026-00001",
        {"docstatus": 1, "status": "Submitted", "title": "Fuel"},
    )
    assert mock_frappe.db.commit.call_count >= 1


def test_submit_purchase_invoice_accepts_invoice_name_alias():
    sys.modules["flask"].request.get_json.return_value = {"invoice_name": "PI-2"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = [
        {
            "docstatus": 0,
            "company": "Acme Pty Ltd",
            "expense_item_name": "Coffee",
            "expense_items_count": 2,
            "remarks": "Team",
        },
        "Submitted",
    ]

    result = submit_purchase_invoice("user@example.com")

    assert result["name"] == "PI-2"
    mock_frappe.db.set_value.assert_called_with(
        "Purchase Invoice",
        "PI-2",
        {"docstatus": 1, "status": "Submitted", "title": "Coffee (+1 more)"},
    )


def test_submit_purchase_invoice_requires_name():
    sys.modules["flask"].request.get_json.return_value = {}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    with pytest.raises(mock_frappe.ValidationError, match="name or invoice_name"):
        submit_purchase_invoice("user@example.com")


def test_submit_purchase_invoice_requires_company():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-1"}
    mock_frappe.defaults = None
    mock_frappe.session = MagicMock()
    mock_frappe.session.user = "Guest"
    mock_frappe.db.get_value.return_value = None
    with pytest.raises(mock_frappe.ValidationError, match="Company is required"):
        submit_purchase_invoice("user@example.com")


def test_submit_purchase_invoice_not_found():
    sys.modules["flask"].request.get_json.return_value = {"name": "missing"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = None
    with pytest.raises(mock_frappe.DoesNotExistError):
        submit_purchase_invoice("user@example.com")


def test_submit_purchase_invoice_wrong_company():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-1"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Other Co",
        "expense_item_name": "X",
        "expense_items_count": 1,
        "remarks": None,
    }
    with pytest.raises(mock_frappe.PermissionError):
        submit_purchase_invoice("user@example.com")


def test_submit_purchase_invoice_rejects_non_draft():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-1"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = {
        "docstatus": 1,
        "company": "Acme Pty Ltd",
        "expense_item_name": "Fuel",
        "expense_items_count": 1,
        "remarks": None,
    }
    with pytest.raises(mock_frappe.ValidationError, match="Only draft"):
        submit_purchase_invoice("user@example.com")


def test_submit_purchase_invoice_retries_status_when_not_submitted():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-9"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = [
        {
            "docstatus": 0,
            "company": "Acme Pty Ltd",
            "expense_item_name": "Paper",
            "expense_items_count": 1,
            "remarks": None,
        },
        "Unpaid",
        "Submitted",
    ]

    submit_purchase_invoice("user@example.com")

    assert mock_frappe.db.set_value.call_count >= 2


# ── cancel_purchase_invoice (submitted → cancelled) ────────────────────────────


def test_cancel_purchase_invoice_success():
    sys.modules["flask"].request.get_json.return_value = {"name": "ACC-PINV-2026-00001"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {
        "docstatus": 1,
        "company": "Acme Pty Ltd",
    }

    result = cancel_purchase_invoice("user@example.com")

    assert result == {
        "success": True,
        "name": "ACC-PINV-2026-00001",
        "docstatus": 2,
        "status": "Cancelled",
    }
    mock_frappe.db.set_value.assert_called_with(
        "Purchase Invoice",
        "ACC-PINV-2026-00001",
        {"docstatus": 2, "status": "Cancelled"},
    )


def test_cancel_purchase_invoice_rejects_draft():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-1"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Acme Pty Ltd",
    }
    with pytest.raises(mock_frappe.ValidationError, match="Only submitted"):
        cancel_purchase_invoice("user@example.com")


