from datetime import date
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MockDocumentController:
    def __init__(self, data):
        for key, value in data.items():
            setattr(self, key, value)


mock_frappe = MagicMock()
mock_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)

mock_app = MagicMock()
mock_app.db = MagicMock()
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


from controllers.purchase_invoice import PurchaseInvoice
from expense_tracker.api import get_dashboard_summary, _app_db


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_app.db.reset_mock()
    mock_app.tenant_db.reset_mock()


def _supplier_fallback_values(doctype, filters, field):
    if doctype == "Item Default":
        if filters.get("parent") == "Fuel":
            return "10000 - Fuel"
        if filters.get("parent") == "Telephony":
            return "12000 - Telephony"
        return None

    if doctype == "Company":
        if filters == "Acme Pty Ltd" and field == "default_cost_center":
            return "Main - CC"
        return None

    if doctype == "Buying Settings":
        return "General"

    return None


def _supplier_tax_rows(doctype, filters, fields, *args, **kwargs):
    if doctype == "Item Tax":
        if filters.get("parent") == "Fuel":
            return [{"tax_type": "GST", "tax_rate": 10}]
    return []


def _gst_template_rows(doctype, filters, fields, *args, **kwargs):
    if doctype == "Purchase Taxes and Charges" and filters.get("parent") == "AU GST 10%":
        return [
            {
                "charge_type": "On Net Total",
                "account_head": "GST Payable - AC",
                "description": "AU GST 10%",
                "rate": 10,
                "cost_center": "Template CC",
                "included_in_print_rate": 0,
                "add_deduct_tax": "Add",
            }
        ]
    return []


def test_purchase_invoice_before_save_sets_default_accounts_and_taxes():
    mock_app.db.get_value.side_effect = _supplier_fallback_values
    mock_app.db.get_all.side_effect = lambda doctype, filters, fields, *args, **kwargs: (
        _supplier_tax_rows(doctype, filters, fields, *args, **kwargs)
        if doctype == "Item Tax"
        else _gst_template_rows(doctype, filters, fields, *args, **kwargs)
    )

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "items": [
                {"item_code": "Fuel", "expense_account": None, "rate": 100.0},
                {"item_code": "Telephony", "expense_account": None, "rate": 100.0},
            ],
        }
    )

    doc.before_save()

    assert doc.items[0]["expense_account"] == "10000 - Fuel"
    assert doc.items[1]["expense_account"] == "12000 - Telephony"
    assert doc.items[0]["cost_center"] == "Main - CC"
    assert doc.items[1]["cost_center"] == "Main - CC"
    assert len(doc.taxes) == 1
    assert doc.taxes[0]["description"] == "AU GST 10%"


def test_purchase_invoice_without_gst_keeps_non_gst_taxes():
    mock_app.db.get_value.side_effect = _supplier_fallback_values
    mock_app.db.get_all.side_effect = lambda doctype, filters, fields, *args, **kwargs: []

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "items": [{"item_code": "Telephony", "expense_account": None}],
            "taxes": [{"charge_type": "On Net Total", "description": "Freight", "rate": 5}],
        }
    )

    doc.before_save()

    assert len(doc.taxes) == 1
    assert doc.taxes[0]["description"] == "Freight"


def test_purchase_invoice_internal_cost_center_is_forced():
    mock_app.db.get_value.side_effect = _supplier_fallback_values
    mock_app.db.get_all.side_effect = lambda doctype, filters, fields, *args, **kwargs: []

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "items": [{"item_code": "Fuel", "cost_center": "User Selected"}],
        }
    )

    doc.before_save()

    assert doc.items[0]["cost_center"] == "Main - CC"


def test_purchase_invoice_auto_creates_supplier():
    mock_app.db.insert.return_value = {"name": "SUP-NEW"}

    def db_get_value(doctype, filters, field):
        if doctype == "Supplier":
            return None
        if doctype == "Buying Settings":
            return "General"
        if doctype == "Company" and filters == "Acme Pty Ltd":
            if field == "default_cost_center":
                return "Main - CC"
            return None
        if doctype == "Item Default":
            return "10000 - Fuel"
        return None

    mock_app.db.get_value.side_effect = db_get_value
    mock_app.db.get_all.side_effect = lambda doctype, filters, fields, *args, **kwargs: []

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "supplier": "New Supplier",
            "items": [{"item_code": "Fuel", "expense_account": None}],
        }
    )

    doc.before_save()

    assert doc.supplier == "SUP-NEW"
    created_supplier = mock_app.db.insert.call_args.args[0]
    assert created_supplier["supplier_name"] == "New Supplier"
    assert created_supplier["supplier_group"] == "General"


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
