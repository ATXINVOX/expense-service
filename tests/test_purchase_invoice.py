from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch
import pytest
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MockDocumentController:
    """Mirrors DocumentController enough for PurchaseInvoice hooks: .doc, .flags, fields on doc."""

    def __init__(self, data):
        self.flags = type("Flags", (), {})()
        for key, value in data.items():
            setattr(self, key, value)
        self.doc = self

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

stub_frappe_client_submit = MagicMock(
    return_value={"name": "stub-name", "docstatus": 1, "status": "Submitted"}
)


def _frappe_get_attr_side_effect(path):
    if path == "frappe.client.submit":
        return stub_frappe_client_submit
    return MagicMock()


mock_frappe.get_attr.side_effect = _frappe_get_attr_side_effect

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

_utils_mod = ModuleType("frappe.utils")


def _getdate(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


_utils_mod.getdate = _getdate
sys.modules["frappe.utils"] = _utils_mod


def _configure_frappe_throw():
    """Make frappe.throw raise so frappe_client_submit errors are testable."""
    mock_frappe.ValidationError = type("ValidationError", (Exception,), {})
    mock_frappe.LinkExistsError = type(
        "LinkExistsError", (mock_frappe.ValidationError,), {}
    )
    mock_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    mock_frappe.PermissionError = type("PermissionError", (Exception,), {})

    def throw_fn(msg, exc=None, *args, **kwargs):
        if exc is not None:
            raise exc(msg)
        raise RuntimeError(msg)

    mock_frappe.throw = throw_fn


_configure_frappe_throw()


class _EmptyRequestArgs:
    """Default GET args so ``period`` is unset unless a test assigns ``request.args``."""

    def get(self, key, default=None):
        return default


# frappe_client_submit imports flask.request at runtime; provide a stub so tests run without Flask installed.
if "flask" not in sys.modules:
    _flask_stub = ModuleType("flask")
    _flask_stub.request = MagicMock()
    sys.modules["flask"] = _flask_stub

from controllers.purchase_invoice import (
    PurchaseInvoice,
    _expense_title,
    mark_purchase_invoice_paid_after_submit,
    normalize_purchase_invoice_payment_dates,
)
from expense_tracker.api import (
    create_purchase_invoice,
    delete_purchase_invoice,
    frappe_client_submit,
    get_dashboard_summary,
    get_financial_dashboard,
    get_purchase_invoice,
    update_purchase_invoice,
    _add_months,
    _aggregate_by_posting_date,
    _app_db,
    _daily_series,
    _fetch_recent_purchase_invoices,
    _get_recent_quotations,
    _project_purchase_invoice_api,
    _resolve_financial_period,
)


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_app.db.reset_mock()
    mock_app.db.get_all.side_effect = None
    mock_app.db.get_value.side_effect = None
    mock_app.db.exists.return_value = False
    mock_app.tenant_db.reset_mock()
    mock_frappe.db.reset_mock()
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.has_column = MagicMock(return_value=True)
    mock_frappe.get_all.reset_mock()
    mock_frappe.get_all.side_effect = None
    mock_frappe.get_all.return_value = []
    mock_frappe.get_doc.reset_mock()
    mock_app.tenant_db.get_all.side_effect = lambda *args, **kwargs: mock_frappe.get_all(*args, **kwargs)
    mock_app.tenant_db.get_value.side_effect = lambda *args, **kwargs: mock_frappe.db.get_value(*args, **kwargs)
    mock_app.tenant_db.get_tenant_id.return_value = "test-tenant-001"
    if "flask" in sys.modules and hasattr(sys.modules["flask"], "request"):
        sys.modules["flask"].request.reset_mock()
        sys.modules["flask"].request.args = _EmptyRequestArgs()
    stub_frappe_client_submit.reset_mock()
    stub_frappe_client_submit.return_value = {
        "name": "stub-name",
        "docstatus": 1,
        "status": "Submitted",
    }
    mock_frappe.flags = type("Flags", (), {})()
    mock_frappe.get_attr.side_effect = _frappe_get_attr_side_effect


def _default_get_value(doctype, filters, field=None, **kwargs):
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
    assert doc.taxes[0]["included_in_print_rate"] == 1


def test_purchase_invoice_gst_off_leaves_amount_without_tax_rows():
    """Non-GST expenses: no template, no GST rows; line rate is unchanged."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "taxes_and_charges": "",
            "items": [{"item_code": "Fuel", "rate": 90.0}],
        }
    )

    doc.before_validate()

    assert doc.taxes_and_charges == ""
    assert doc.taxes == []
    assert doc.items[0]["rate"] == 90.0


def test_purchase_invoice_mobile_gst_alias_applies_template_rows():
    """Mobile sends taxes_and_charges: GST; server resolves company purchase template."""
    mock_frappe.db.get_value.side_effect = _default_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "taxes_and_charges": "GST",
            "items": [{"item_code": "Fuel", "rate": 100.0}],
        }
    )

    doc.before_validate()

    assert doc.taxes_and_charges == GST_TEMPLATE_NAME
    assert len(doc.taxes) == 1
    assert doc.taxes[0]["included_in_print_rate"] == 1


def test_resolve_purchase_gst_template_creates_template_when_missing():
    with patch(
        "controllers.purchase_invoice._find_gst_template",
        side_effect=[None, GST_TEMPLATE_NAME],
    ), patch(
        "controllers.purchase_invoice._ensure_purchase_gst_template",
        return_value=GST_TEMPLATE_NAME,
    ) as ensure_mock:
        from controllers.purchase_invoice import _resolve_purchase_gst_template

        resolved = _resolve_purchase_gst_template("Acme Pty Ltd", "GST")

    assert resolved == GST_TEMPLATE_NAME
    ensure_mock.assert_called_once_with("Acme Pty Ltd")


def test_ensure_account_nested_set_rebuilds_when_accounts_unset():
    mock_frappe.db.sql.return_value = [[4]]
    rebuild_mock = MagicMock()
    nestedset_mod = ModuleType("frappe.utils.nestedset")
    nestedset_mod.rebuild_tree = rebuild_mock
    sys.modules["frappe.utils.nestedset"] = nestedset_mod

    from controllers.purchase_invoice import _ensure_account_nested_set

    _ensure_account_nested_set("Acme Pty Ltd")
    rebuild_mock.assert_called_once_with("Account")


def test_purchase_invoice_uses_bas_account_1b_for_gst():
    """GST tax rows post to account_1b from AU Simpler BAS Report Setup."""
    mock_frappe.db.table_exists = MagicMock(return_value=True)

    def _gv(doctype, filters=None, field=None, *args, **kwargs):
        if doctype == "AU Simpler BAS Report Setup" and filters == "Acme Pty Ltd":
            if isinstance(field, (list, tuple)):
                return {"account_1a": "GST Collected - AC", "account_1b": "GST Paid BAS - AC"}
        return _default_get_value(doctype, filters, field)

    mock_frappe.db.get_value.side_effect = _gv
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "taxes_and_charges": "AU GST 10%",
            "items": [{"item_code": "Fuel", "expense_account": None, "rate": 100.0}],
        }
    )

    doc.before_validate()

    assert len(doc.taxes) == 1
    assert doc.taxes[0]["account_head"] == "GST Paid BAS - AC"
    assert doc.taxes[0]["included_in_print_rate"] == 1
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
        if doctype == "Company" and field == "abbr":
            if filters == "Wrong Company":
                return "WRONG"
            if filters == "Session Company Pty Ltd":
                return "SESS"
        if doctype == "Company" and filters == "Session Company Pty Ltd" and field == "cost_center":
            return "Session CC"
        if doctype == "Company" and filters == "Wrong Company" and field == "cost_center":
            return "Wrong CC"
        return None

    mock_frappe.db.get_value.side_effect = db_get_value
    mock_frappe.get_all.side_effect = _default_get_all

    doc = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "company": "Wrong Company",  # explicit POST company is kept (Cypress env)
            "items": [{"item_code": "Fuel", "expense_account": None}],
        }
    )

    doc.before_validate()

    assert doc.company == "Wrong Company"

    doc2 = PurchaseInvoice(
        {
            "doctype": "Purchase Invoice",
            "items": [{"item_code": "Fuel", "expense_account": None}],
        }
    )
    doc2.before_validate()
    assert doc2.company == "Session Company Pty Ltd"
    assert doc2.items[0]["cost_center"] == "Session CC"


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
    mock_frappe.db.get_value.return_value = "AUD"

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
    mock_frappe.db.get_value.return_value = "AUD"
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
        ["docstatus", "=", 1],
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


def test_dashboard_summary_fallbacks_to_invoice_level_item_group_when_child_query_empty():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"
    mock_app.db.get_all.side_effect = [
        [
            {
                "name": "PI-1",
                "grand_total": 120.0,
                "total_taxes_and_charges": 10.0,
                "expense_item_group": "Office Supplies",
            },
            {
                "name": "PI-2",
                "grand_total": 80.0,
                "total_taxes_and_charges": 0.0,
                "expense_item_group": "Travel",
            },
        ],
        [],
    ]

    result = get_dashboard_summary("test_user")

    assert result["total_spend"] == 200.0
    assert len(result["breakdown"]) == 2
    assert result["breakdown"][0]["item_group"] == "Office Supplies"
    assert result["breakdown"][0]["total"] == 120.0


class _FakeArgs:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def _date_class_with_fixed_today(fixed: date):
    """Patch target for ``expense_tracker.api.date`` — Python 3.14+ cannot patch ``date.today`` on builtins."""

    class _DateFixedToday(date):
        @classmethod
        def today(cls):
            return fixed

    return _DateFixedToday


def test_dashboard_summary_preset_month_cashflow_and_trend():
    fixed_today = date(2026, 5, 7)
    sys.modules["flask"].request.args = _FakeArgs({"period": "month"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    with patch("expense_tracker.api.date", _date_class_with_fixed_today(fixed_today)), patch(
        "expense_tracker.api._get_frappe_today", return_value=fixed_today
    ):
        mock_app.tenant_db.get_all.side_effect = [
            [
                {
                    "name": "PI-1",
                    "grand_total": 100.0,
                    "total_taxes_and_charges": 10.0,
                    "posting_date": fixed_today.replace(day=5),
                },
            ],
            [{"item_group": "Fuel", "amount": 100.0}],
            [{"grand_total": 40.0}],
        ]
        result = get_dashboard_summary("test_user")

    assert result["preset"] == "month"
    assert result["trend_pct"] == 150.0
    assert result["compare_period_label"] == "vs last month"
    assert result["cashflow"][0]["label"] == "W1"
    assert result["cashflow"][0]["amount"] == 100.0
    assert result["cashflow_stats"]["highest"] == 100.0
    assert result["breakdown"][0]["pct"] == 100.0
    assert result["breakdown"][0]["color"].startswith("#")
    assert result["top_category"]["item_group"] == "Fuel"
    assert "breakdown_top4" in result
    assert len(result["breakdown_top4"]) == 1
    assert result["breakdown_top4"][0]["item_group"] == "Fuel"


def test_dashboard_summary_invalid_period_raises():
    sys.modules["flask"].request.args = _FakeArgs({"period": "week"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    with pytest.raises(RuntimeError, match="period must be one of"):
        get_dashboard_summary("test_user")


def test_dashboard_summary_preset_quarter_cashflow_buckets():
    fixed_today = date(2026, 5, 15)
    sys.modules["flask"].request.args = _FakeArgs({"period": "quarter"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    with patch("expense_tracker.api.date", _date_class_with_fixed_today(fixed_today)), patch(
        "expense_tracker.api._get_frappe_today", return_value=fixed_today
    ):
        mock_app.tenant_db.get_all.side_effect = [
            [
                {
                    "name": "PI-1",
                    "grand_total": 80.0,
                    "total_taxes_and_charges": 8.0,
                    "posting_date": date(2026, 4, 10),
                },
                {
                    "name": "PI-2",
                    "grand_total": 20.0,
                    "total_taxes_and_charges": 2.0,
                    "posting_date": date(2026, 5, 5),
                },
            ],
            [{"item_group": "Fuel", "amount": 100.0}],
            [{"grand_total": 40.0}],
        ]
        result = get_dashboard_summary("test_user")

    assert result["preset"] == "quarter"
    assert result["compare_period_label"] == "vs last quarter"
    assert [b["label"] for b in result["cashflow"]] == ["Apr", "May", "Jun"]
    assert result["cashflow"][0]["amount"] == 80.0
    assert result["cashflow"][1]["amount"] == 20.0


def test_dashboard_summary_preset_year_uses_quarter_buckets():
    fixed_today = date(2026, 8, 20)
    sys.modules["flask"].request.args = _FakeArgs({"period": "year"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    with patch("expense_tracker.api.date", _date_class_with_fixed_today(fixed_today)), patch(
        "expense_tracker.api._get_frappe_today", return_value=fixed_today
    ):
        mock_app.tenant_db.get_all.side_effect = [
            [
                {
                    "name": "PI-1",
                    "grand_total": 50.0,
                    "total_taxes_and_charges": 5.0,
                    "posting_date": date(2026, 2, 10),
                },
                {
                    "name": "PI-2",
                    "grand_total": 70.0,
                    "total_taxes_and_charges": 7.0,
                    "posting_date": date(2026, 7, 1),
                },
            ],
            [{"item_group": "Fuel", "amount": 120.0}],
            [{"grand_total": 30.0}],
        ]
        result = get_dashboard_summary("test_user")

    assert result["preset"] == "year"
    assert [b["label"] for b in result["cashflow"]] == ["Q1", "Q2", "Q3", "Q4"]
    assert result["cashflow"][0]["amount"] == 50.0
    assert result["cashflow"][2]["amount"] == 70.0


def test_dashboard_summary_custom_range_cashflow_and_trend():
    sys.modules["flask"].request.args = _FakeArgs(
        {"from_date": "2026-02-01", "to_date": "2026-02-28"}
    )
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    mock_app.tenant_db.get_all.side_effect = [
        [
            {
                "name": "PI-1",
                "grand_total": 200.0,
                "total_taxes_and_charges": 20.0,
                "posting_date": date(2026, 2, 10),
            },
        ],
        [{"item_group": "Fuel", "amount": 200.0}],
        [{"grand_total": 100.0}],
    ]
    result = get_dashboard_summary("test_user")

    assert result["preset"] == "custom"
    assert result["from_date"] == "2026-02-01"
    assert result["to_date"] == "2026-02-28"
    assert result["trend_pct"] == 100.0
    assert result["compare_period_label"] == "vs prior period"
    assert result["cashflow"][1]["label"] == "W2"
    assert result["cashflow"][1]["amount"] == 200.0
    assert result["top_category"]["item_group"] == "Fuel"


def test_dashboard_summary_custom_period_param_requires_dates():
    sys.modules["flask"].request.args = _FakeArgs({"period": "custom"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    with pytest.raises(RuntimeError, match="from_date and to_date"):
        get_dashboard_summary("test_user")


def test_dashboard_summary_preset_week_rejected():
    sys.modules["flask"].request.args = _FakeArgs({"period": "WEEK"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    with pytest.raises(RuntimeError, match="period must be one of"):
        get_dashboard_summary("test_user")


def test_dashboard_summary_preset_breakdown_top4_merges_remainder_as_others():
    """Donut UX: preset mode returns ``breakdown_top4`` (4 categories + Others)."""
    fixed_today = date(2026, 5, 7)
    sys.modules["flask"].request.args = _FakeArgs({"period": "month"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    inv_rows = [
        {
            "name": f"PI-{i}",
            "grand_total": 10.0,
            "total_taxes_and_charges": 0.0,
            "posting_date": fixed_today.replace(day=5),
        }
        for i in range(6)
    ]
    agg_rows = [{"item_group": f"Cat{i}", "total": 10.0} for i in range(6)]
    prev = [{"grand_total": 50.0}]

    with patch("expense_tracker.api.date", _date_class_with_fixed_today(fixed_today)), patch(
        "expense_tracker.api._get_frappe_today", return_value=fixed_today
    ):
        mock_app.db.get_all.side_effect = [inv_rows, agg_rows, prev]
        result = get_dashboard_summary("test_user")

    assert len(result["breakdown"]) == 6
    top4 = result["breakdown_top4"]
    assert len(top4) == 5
    assert top4[-1]["item_group"] == "Others"
    assert top4[-1]["total"] == 20.0
    pct_sum = sum(float(r["pct"]) for r in top4)
    assert abs(pct_sum - 100.0) < 0.1


def test_dashboard_recent_expenses_outside_period_with_tenant_visibility():
    """Recent list is not limited to the dashboard period and uses tenant or_filters."""
    fixed_today = date(2026, 6, 11)
    sys.modules["flask"].request.args = _FakeArgs({"period": "month", "recent_limit": "5"})
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = "AUD"

    old_posting = fixed_today - timedelta(days=120)
    recent_row = {
        "name": "ACC-PINV-OLD",
        "supplier": "Legacy Supplier",
        "posting_date": old_posting,
        "status": "Draft",
        "grand_total": 888.0,
        "total_taxes_and_charges": 0.0,
        "currency": "AUD",
        "remarks": "Old expense",
        "expense_item_name": "Travel",
        "expense_item_group": "Travel",
        "modified": datetime.combine(fixed_today, datetime.min.time()),
    }

    with patch("expense_tracker.api.date", _date_class_with_fixed_today(fixed_today)), patch(
        "expense_tracker.api._get_frappe_today", return_value=fixed_today
    ):
        mock_app.db.get_all.side_effect = [[], []]
        mock_frappe.get_all.return_value = [recent_row]
        result = get_dashboard_summary("test_user")

    assert result["total_spend"] == 0.0
    assert len(result["recent_expenses"]) == 1
    assert result["recent_expenses"][0]["name"] == "ACC-PINV-OLD"
    assert result["recent_expenses"][0]["supplier"] == "Legacy Supplier"
    assert result["recent_expenses"][0]["amount"] == 888.0

    recent_calls = [
        c
        for c in mock_frappe.get_all.call_args_list
        if c.args and c.args[0] == "Purchase Invoice"
    ]
    assert len(recent_calls) == 1
    assert recent_calls[0].kwargs.get("order_by") == "modified desc"
    assert recent_calls[0].kwargs.get("or_filters") == [
        ["tenant_id", "=", "test-tenant-001"],
        ["tenant_id", "=", "SYSTEM"],
        ["tenant_id", "is", "not set"],
        ["tenant_id", "=", ""],
    ]


def test_fetch_recent_purchase_invoices_without_tenant_uses_db_get_all():
    mock_app.tenant_db.get_tenant_id.return_value = ""
    mock_app.tenant_db.get_all.side_effect = None
    mock_app.tenant_db.get_all.return_value = [{"name": "PI-1"}]
    mock_frappe.get_all.reset_mock()
    rows = _fetch_recent_purchase_invoices(
        "Acme Pty Ltd",
        5,
        ["name", "supplier", "posting_date", "grand_total"],
    )
    assert rows == [{"name": "PI-1"}]
    mock_app.tenant_db.get_all.assert_called_once()
    mock_frappe.get_all.assert_not_called()


def test_financial_dashboard_custom_daily_totals_and_activity():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"

    sys.modules["flask"].request.args = _FakeArgs(
        {
            "preset": "custom",
            "from_date": "2026-05-01",
            "to_date": "2026-05-03",
            "activity_limit": "5",
        }
    )

    def period_get_all(doctype, *args, **kwargs):
        if doctype == "Sales Invoice":
            return [
                {"name": "SINV-1", "posting_date": date(2026, 5, 1), "grand_total": 100.0},
                {"name": "SINV-2", "posting_date": date(2026, 5, 3), "grand_total": 50.0},
            ]
        if doctype == "Purchase Invoice":
            return [
                {"name": "PINV-1", "posting_date": date(2026, 5, 2), "grand_total": 40.0},
            ]
        return []

    mock_app.tenant_db.get_all.side_effect = period_get_all

    def recent_get_all(doctype, *args, **kwargs):
        if kwargs.get("order_by"):
            if doctype == "Sales Invoice":
                return [
                    {
                        "name": "SINV-1",
                        "customer": "Cust A",
                        "posting_date": date(2026, 5, 1),
                        "grand_total": 100.0,
                        "modified": datetime(2026, 5, 4, 10, 0, 0),
                        "status": "Paid",
                    },
                ]
            if doctype == "Purchase Invoice":
                return [
                    {
                        "name": "PINV-1",
                        "supplier": "Sup B",
                        "posting_date": date(2026, 5, 2),
                        "grand_total": 40.0,
                        "modified": datetime(2026, 5, 4, 11, 0, 0),
                        "status": "Draft",
                    },
                ]
        if doctype == "Quotation":
            return [
                {
                    "name": "SAL-QTN-2026-00001",
                    "customer_name": "Prospect Co",
                    "party_name": "Prospect Co",
                    "transaction_date": date(2026, 5, 3),
                    "grand_total": 500.0,
                    "modified": datetime(2026, 5, 4, 12, 0, 0),
                    "status": "Open",
                    "docstatus": 1,
                },
            ]
        return []

    mock_frappe.get_all.side_effect = recent_get_all
    mock_frappe.db.get_value.return_value = "AUD"

    result = get_financial_dashboard("test_user")

    assert result["preset"] == "custom"
    assert result["from_date"] == "2026-05-01"
    assert result["totals"]["income"] == 150.0
    assert result["totals"]["expense"] == 40.0
    assert result["totals"]["net"] == 110.0
    assert len(result["daily"]) == 3
    assert result["daily"][0]["date"] == "2026-05-01"
    assert result["daily"][0]["income"] == 100.0
    assert result["daily"][0]["expense"] == 0.0
    assert result["daily"][1]["expense"] == 40.0
    assert result["daily"][2]["income"] == 50.0
    assert len(result["recent_activity"]) <= 5
    assert result["recent_activity"][0]["doctype"] == "Quotation"
    assert result["resource_api"]["quotation_list"] == "/api/resource/Quotation"
    doctypes = {r["doctype"] for r in result["recent_activity"]}
    assert "Quotation" in doctypes
    assert "Purchase Invoice" in doctypes


def test_get_recent_quotations_matches_saas_platform_tenant_visibility():
    mock_frappe.get_all.return_value = []
    _get_recent_quotations("Acme Pty Ltd", 20, "tenant-abc-001")
    mock_frappe.get_all.assert_called_once_with(
        "Quotation",
        filters=[
            ["company", "=", "Acme Pty Ltd"],
        ],
        or_filters=[
            ["tenant_id", "=", "tenant-abc-001"],
            ["tenant_id", "=", "SYSTEM"],
            ["tenant_id", "is", "not set"],
            ["tenant_id", "=", ""],
        ],
        fields=[
            "name",
            "customer_name",
            "party_name",
            "transaction_date",
            "grand_total",
            "modified",
            "status",
            "docstatus",
        ],
        order_by="modified desc",
        limit=20,
    )


def test_financial_dashboard_includes_cancelled_quotations():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    sys.modules["flask"].request.args = _FakeArgs({"preset": "last_7_days"})

    mock_app.tenant_db.get_all.return_value = []

    def quotation_get_all(doctype, *args, **kwargs):
        if doctype == "Quotation":
            return [
                {
                    "name": "SAL-QTN-CANCELLED",
                    "customer_name": "Lost Co",
                    "party_name": "Lost Co",
                    "transaction_date": date(2026, 6, 1),
                    "grand_total": 99.0,
                    "modified": datetime(2026, 6, 3, 9, 0, 0),
                    "status": "Cancelled",
                    "docstatus": 2,
                },
            ]
        return []

    mock_frappe.get_all.side_effect = quotation_get_all
    mock_frappe.db.get_value.return_value = "AUD"

    result = get_financial_dashboard("test_user")
    q_rows = [r for r in result["recent_activity"] if r["doctype"] == "Quotation"]
    assert len(q_rows) == 1
    assert q_rows[0]["name"] == "SAL-QTN-CANCELLED"
    assert q_rows[0]["status"] == "Cancelled"
    assert q_rows[0]["docstatus"] == 2


def test_financial_dashboard_custom_requires_dates():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    sys.modules["flask"].request.args = _FakeArgs({"preset": "custom"})

    mock_app.tenant_db.get_all.return_value = []

    with pytest.raises(RuntimeError, match="from_date and to_date"):
        get_financial_dashboard("test_user")


def test_resolve_financial_period_last_7_days():
    fd, td, label = _resolve_financial_period("last_7_days", None, None)
    assert label == "last_7_days"
    assert td == date.today()
    assert fd == td - timedelta(days=6)


def test_resolve_financial_period_last_6_months():
    fd, td, label = _resolve_financial_period("last_6_months", None, None)
    assert label == "last_6_months"
    assert td == date.today()
    assert fd == _add_months(date.today(), -6)


def test_resolve_financial_period_preset_aliases_match_canonical():
    a_fd, a_td, _ = _resolve_financial_period("7d", None, None)
    b_fd, b_td, _ = _resolve_financial_period("last_7_days", None, None)
    assert (a_fd, a_td) == (b_fd, b_td)


def test_resolve_financial_period_invalid_preset_raises():
    with pytest.raises(RuntimeError, match="preset must be one of"):
        _resolve_financial_period("year_to_date", None, None)


def test_resolve_financial_period_custom_swaps_inverted_dates():
    fd, td, label = _resolve_financial_period("custom", "2026-05-10", "2026-05-01")
    assert label == "custom"
    assert fd.isoformat() == "2026-05-01"
    assert td.isoformat() == "2026-05-10"


def test_resolve_financial_period_custom_span_over_732_days_raises():
    with pytest.raises(RuntimeError, match="732"):
        _resolve_financial_period("custom", "2024-01-01", "2026-05-05")


def test_aggregate_by_posting_date_handles_iso_datetime_strings():
    buckets = _aggregate_by_posting_date(
        [{"posting_date": "2026-03-01T00:00:00", "grand_total": "10.5"}],
        "grand_total",
    )
    assert buckets.get("2026-03-01") == 10.5


def test_daily_series_includes_all_calendar_days():
    income = {"2026-01-02": 100.0}
    expense = {"2026-01-03": 25.0}
    series = _daily_series(date(2026, 1, 1), date(2026, 1, 3), income, expense)
    assert len(series) == 3
    assert series[0]["date"] == "2026-01-01"
    assert series[0]["income"] == 0.0
    assert series[1]["income"] == 100.0
    assert series[2]["expense"] == 25.0
    assert series[2]["net"] == -25.0


def test_financial_dashboard_activity_limit_is_capped_at_50():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    sys.modules["flask"].request.args = _FakeArgs(
        {
            "preset": "custom",
            "from_date": "2026-05-01",
            "to_date": "2026-05-01",
            "activity_limit": "99",
        }
    )

    def recent_get_all(doctype, *args, **kwargs):
        if kwargs.get("order_by"):
            lim = kwargs.get("limit", 20)
            if doctype == "Sales Invoice":
                return [
                    {
                        "name": f"S-{i}",
                        "customer": "C",
                        "posting_date": date(2026, 5, 1),
                        "grand_total": 1.0,
                        "modified": datetime(2026, 5, 1, 1, i % 60, 0),
                        "status": "Draft",
                    }
                    for i in range(lim)
                ]
            if doctype == "Purchase Invoice":
                return [
                    {
                        "name": f"P-{i}",
                        "supplier": "S",
                        "posting_date": date(2026, 5, 1),
                        "grand_total": 2.0,
                        "modified": datetime(2026, 5, 1, 2, i % 60, 0),
                        "status": "Draft",
                    }
                    for i in range(lim)
                ]
        return []

    mock_app.tenant_db.get_all.return_value = []
    mock_frappe.get_all.side_effect = recent_get_all
    mock_frappe.db.get_value.return_value = "AUD"

    result = get_financial_dashboard("test_user")
    assert len(result["recent_activity"]) == 50


def test_financial_dashboard_malformed_activity_limit_defaults_to_20():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    sys.modules["flask"].request.args = _FakeArgs(
        {
            "preset": "custom",
            "from_date": "2026-05-01",
            "to_date": "2026-05-01",
            "activity_limit": "not-a-number",
        }
    )

    def recent_get_all(doctype, *args, **kwargs):
        if kwargs.get("order_by"):
            lim = kwargs.get("limit", 20)
            if doctype == "Sales Invoice":
                return [
                    {
                        "name": f"S-{i}",
                        "customer": "C",
                        "posting_date": date(2026, 5, 1),
                        "grand_total": 1.0,
                        "modified": datetime(2026, 5, 1, 3, i, 0),
                        "status": "Draft",
                    }
                    for i in range(lim)
                ]
            if doctype == "Purchase Invoice":
                return [
                    {
                        "name": f"P-{i}",
                        "supplier": "S",
                        "posting_date": date(2026, 5, 1),
                        "grand_total": 2.0,
                        "modified": datetime(2026, 5, 1, 4, i, 0),
                        "status": "Draft",
                    }
                    for i in range(lim)
                ]
        return []

    mock_app.tenant_db.get_all.return_value = []
    mock_frappe.get_all.side_effect = recent_get_all
    mock_frappe.db.get_value.return_value = "AUD"

    result = get_financial_dashboard("test_user")
    assert len(result["recent_activity"]) == 20


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
    
    # Verify: Company should be updated with Main CC (via frappe.db, not tenant_db)
    mock_frappe.db.set_value.assert_any_call(
        "Company", "Acme Pty Ltd", "cost_center", "Main - ACME", update_modified=False
    )


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


def test_normalize_purchase_invoice_payment_dates_resets_bill_date_to_posting():
    doc = MockDocumentController({
        "posting_date": "2026-06-15",
        "due_date": "2026-06-15",
        "bill_date": "2026-06-17",
    })

    normalize_purchase_invoice_payment_dates(doc)

    assert doc.bill_date == "2026-06-15"
    assert doc.due_date == "2026-06-15"


def test_normalize_purchase_invoice_payment_dates_keeps_later_due_date():
    doc = MockDocumentController({
        "posting_date": "2026-06-15",
        "due_date": "2026-06-30",
        "bill_date": "2026-06-17",
    })

    normalize_purchase_invoice_payment_dates(doc)

    assert doc.bill_date == "2026-06-15"
    assert doc.due_date == "2026-06-30"


def test_normalize_purchase_invoice_payment_dates_aligns_payment_schedule():
    doc = MockDocumentController({
        "posting_date": "2026-07-15",
        "due_date": "2026-06-17",
        "bill_date": "2026-06-17",
        "payment_schedule": [{"due_date": "2026-06-17"}],
    })

    normalize_purchase_invoice_payment_dates(doc)

    assert doc.bill_date == "2026-07-15"
    assert doc.due_date == "2026-07-15"
    assert doc.payment_schedule[0]["due_date"] == "2026-07-15"


def test_mark_purchase_invoice_paid_after_submit_skips_when_already_paid():
    mock_frappe.db.get_value.return_value = {
        "docstatus": 1,
        "status": "Paid",
        "outstanding_amount": 0,
        "company": "Acme Pty Ltd",
        "posting_date": date(2026, 7, 1),
    }

    mark_purchase_invoice_paid_after_submit("ACC-PINV-2026-00001")
    mock_frappe.db.set_value.assert_not_called()


def test_mark_purchase_invoice_paid_after_submit_creates_payment_entry():
    mock_frappe.db.get_value.side_effect = [
        {
            "docstatus": 1,
            "status": "Unpaid",
            "outstanding_amount": 50.0,
            "company": "Acme Pty Ltd",
            "posting_date": date(2026, 7, 1),
        },
        0,
    ]
    mock_frappe.db.exists.return_value = True
    mock_frappe.db.has_column.return_value = False
    mock_frappe.get_all.return_value = [{"name": "Cash - ACME"}]

    mock_pe = MagicMock()
    mock_get_pe = MagicMock(return_value=mock_pe)

    fake_pe_mod = ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
    fake_pe_mod.get_payment_entry = mock_get_pe
    erpnext_modules = {
        "erpnext": ModuleType("erpnext"),
        "erpnext.accounts": ModuleType("erpnext.accounts"),
        "erpnext.accounts.doctype": ModuleType("erpnext.accounts.doctype"),
        "erpnext.accounts.doctype.payment_entry": ModuleType("erpnext.accounts.doctype.payment_entry"),
        "erpnext.accounts.doctype.payment_entry.payment_entry": fake_pe_mod,
    }
    with patch.dict(sys.modules, erpnext_modules):
        mark_purchase_invoice_paid_after_submit("ACC-PINV-2026-00001")

    mock_get_pe.assert_called_once_with(
        "Purchase Invoice",
        "ACC-PINV-2026-00001",
        bank_account="Cash - ACME",
    )
    mock_pe.insert.assert_called_once_with(ignore_permissions=True)
    mock_pe.submit.assert_called_once_with()


def test_mark_purchase_invoice_paid_after_submit_sets_status_when_no_cash_account():
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = {
        "docstatus": 1,
        "status": "Unpaid",
        "outstanding_amount": 25.0,
        "company": "Acme Pty Ltd",
        "posting_date": date(2026, 7, 1),
    }
    mock_frappe.db.exists.return_value = False
    mock_frappe.db.has_column.return_value = False
    mock_frappe.get_all.return_value = []

    with patch(
        "controllers.purchase_invoice._ensure_account_row",
        return_value=None,
    ):
        mark_purchase_invoice_paid_after_submit("ACC-PINV-2026-00002")

    paid_status_calls = [
        c
        for c in mock_frappe.db.set_value.call_args_list
        if len(c[0]) >= 4 and c[0][2] == "status" and c[0][3] == "Paid"
    ]
    assert paid_status_calls, "expected status=Paid when no cash account is available"


# ── frappe.client.submit wrapper (Purchase Invoice + tenant checks) ───────────


def test_frappe_client_submit_purchase_invoice_success():
    sys.modules["flask"].request.get_json.return_value = {"name": "ACC-PINV-2026-00001"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Acme Pty Ltd",
        "expense_item_name": "Fuel",
        "expense_items_count": 1,
        "remarks": None,
    }
    mock_app.tenant_db.get_doc.return_value = MagicMock()

    # frappe.get_doc("Purchase Invoice", name) returns a mock doc
    mock_pi_doc = MagicMock()
    mock_pi_doc.name = "ACC-PINV-2026-00001"
    mock_pi_doc.status = "Paid"
    mock_pi_doc.docstatus = 1
    mock_frappe.get_doc.return_value = mock_pi_doc

    with patch(
        "expense_tracker.api.mark_purchase_invoice_paid_after_submit",
    ) as mock_mark_paid:
        result = frappe_client_submit("user@example.com")

    assert result["success"] is True
    assert result["name"] == "ACC-PINV-2026-00001"
    assert result["docstatus"] == 1
    assert result["status"] == "Paid"
    mock_frappe.db.set_value.assert_any_call(
        "Purchase Invoice", "ACC-PINV-2026-00001", "title", "Fuel", update_modified=False
    )
    mock_frappe.get_doc.assert_called_once_with("Purchase Invoice", "ACC-PINV-2026-00001")
    mock_pi_doc.submit.assert_called_once()
    mock_mark_paid.assert_called_once_with("ACC-PINV-2026-00001")
    mock_pi_doc.reload.assert_called_once()


def test_frappe_client_submit_accepts_doc_and_invoice_name_alias():
    sys.modules["flask"].request.get_json.return_value = {"invoice_name": "PI-2"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Acme Pty Ltd",
        "expense_item_name": "Coffee",
        "expense_items_count": 2,
        "remarks": "Team",
    }
    mock_app.tenant_db.get_doc.return_value = MagicMock()

    mock_pi_doc = MagicMock()
    mock_pi_doc.name = "PI-2"
    mock_pi_doc.status = "Submitted"
    mock_frappe.get_doc.return_value = mock_pi_doc

    frappe_client_submit("user@example.com")

    mock_frappe.db.set_value.assert_any_call(
        "Purchase Invoice", "PI-2", "title", "Coffee (+1 more)", update_modified=False
    )
    assert mock_frappe.get_doc.call_count == 2
    mock_frappe.get_doc.assert_any_call("Purchase Invoice", "PI-2")
    mock_pi_doc.submit.assert_called_once()


def test_frappe_client_submit_skips_title_when_no_db_column():
    """Sites without ``tabPurchase Invoice.title`` (older ERPNext) must still submit."""
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-NO-TITLE-COL"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Acme Pty Ltd",
        "expense_item_name": "Fuel",
        "expense_items_count": 1,
        "remarks": None,
    }
    mock_frappe.db.has_column = MagicMock(return_value=False)
    mock_app.tenant_db.get_doc.return_value = MagicMock()

    mock_pi_doc = MagicMock()
    mock_pi_doc.name = "PI-NO-TITLE-COL"
    mock_pi_doc.docstatus = 1
    mock_frappe.get_doc.return_value = mock_pi_doc

    result = frappe_client_submit("user@example.com")

    assert result["success"] is True
    title_updates = [
        c
        for c in mock_frappe.db.set_value.call_args_list
        if c[0] and c[0][0] == "Purchase Invoice" and len(c[0]) > 2 and c[0][2] == "title"
    ]
    assert not title_updates
    mock_pi_doc.submit.assert_called_once()


def test_frappe_client_submit_accepts_frappe_doc_payload():
    sys.modules["flask"].request.get_json.return_value = {
        "doc": {"doctype": "Purchase Invoice", "name": "PI-DOC-1"}
    }
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {
        "docstatus": 0,
        "company": "Acme Pty Ltd",
        "expense_item_name": "X",
        "expense_items_count": 1,
        "remarks": None,
    }
    mock_app.tenant_db.get_doc.return_value = MagicMock()

    mock_pi_doc = MagicMock()
    mock_pi_doc.name = "PI-DOC-1"
    mock_pi_doc.status = "Submitted"
    mock_frappe.get_doc.return_value = mock_pi_doc

    frappe_client_submit("user@example.com")

    assert mock_frappe.get_doc.call_count == 2
    mock_frappe.get_doc.assert_any_call("Purchase Invoice", "PI-DOC-1")
    mock_pi_doc.submit.assert_called_once()


def test_frappe_client_submit_requires_name():
    sys.modules["flask"].request.get_json.return_value = {}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    body, code = frappe_client_submit("user@example.com")
    assert code == 400
    assert body["status"] == "error"
    assert body["type"] == "ValidationError"
    assert "doc" in body["message"].lower() or "name" in body["message"].lower()


def test_frappe_client_submit_requires_company():
    sys.modules["flask"].request.get_json.return_value = {"name": "PI-1"}
    mock_frappe.defaults = None
    mock_frappe.session = MagicMock()
    mock_frappe.session.user = "Guest"
    mock_frappe.db.get_value.return_value = None
    body, code = frappe_client_submit("user@example.com")
    assert code == 400
    assert body["type"] == "ValidationError"
    assert "Company is required" in body["message"]


def test_frappe_client_submit_not_found():
    sys.modules["flask"].request.get_json.return_value = {"name": "missing"}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.side_effect = None
    mock_frappe.db.get_value.return_value = None
    body, code = frappe_client_submit("user@example.com")
    assert code == 404
    assert body["type"] == "DoesNotExistError"
    assert "missing" in body["message"]


def test_frappe_client_submit_wrong_company():
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
    body, code = frappe_client_submit("user@example.com")
    assert code == 403
    assert body["type"] == "PermissionError"
    assert "do not have access" in body["message"]


def test_frappe_client_submit_rejects_non_draft():
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
    body, code = frappe_client_submit("user@example.com")
    assert code == 400
    assert body["type"] == "ValidationError"
    assert "Only draft" in body["message"]


# ── slim GET/POST projection (resource API) ─────────────────────────────────


def test_project_purchase_invoice_api_coerces_types():
    doc = MagicMock()
    doc.as_dict.return_value = {
        "name": "ACC-PINV-1",
        "company": "Acme Pty Ltd",
        "supplier": "aavin",
        "posting_date": date(2026, 4, 8),
        "remarks": "Grocery",
        "items": [
            {
                "item_code": "Milk 2L",
                "item_group": "Groceries",
                "qty": 3,
                "rate": 4.5,
                "amount": 13.5,
            }
        ],
        "status": "Draft",
        "docstatus": 0,
        "grand_total": 13.5,
        "currency": "AUD",
        "expense_item_name": "Milk 2L",
        "expense_item_group": "Groceries",
        "expense_items_count": 1,
    }
    out = _project_purchase_invoice_api(doc)
    assert out["id"] == "ACC-PINV-1"
    assert out["posting_date"] == "2026-04-08"
    assert out["items"][0]["qty"] == 3.0
    assert out["grand_total"] == 13.5
    assert out["expense_item_name"] == "Milk 2L"
    assert out["expense_item_group"] == "Groceries"
    assert out["expense_items_count"] == 1


def test_get_purchase_invoice_returns_slim_json():
    mock_doc = MagicMock()
    mock_doc.as_dict.return_value = {
        "name": "ACC-PINV-1",
        "supplier": "S",
        "posting_date": "2026-04-08",
        "remarks": None,
        "items": [],
        "status": "Draft",
        "docstatus": 0,
        "grand_total": 0.0,
        "currency": "AUD",
    }
    mock_app.tenant_db.get_doc.return_value = mock_doc

    out = get_purchase_invoice("u@x.com", "ACC-PINV-2026-00001")

    assert out["name"] == "ACC-PINV-1"
    assert out["id"] == "ACC-PINV-1"
    mock_app.tenant_db.get_doc.assert_called_once_with(
        "Purchase Invoice", "ACC-PINV-2026-00001"
    )


def test_create_purchase_invoice_returns_201_with_slim_body():
    sys.modules["flask"].request.json = {
        "supplier": "aavin",
        "posting_date": "2026-04-08",
        "remarks": "Grocery",
        "items": [{"item_code": "Milk", "item_group": "G", "qty": 1, "rate": 2.0}],
    }
    mock_doc = MagicMock()
    mock_doc.as_dict.return_value = {
        "name": "ACC-NEW",
        "supplier": "aavin",
        "posting_date": "2026-04-08",
        "remarks": "Grocery",
        "items": [
            {
                "item_code": "Milk",
                "item_group": "G",
                "qty": 1.0,
                "rate": 2.0,
                "amount": 2.0,
            }
        ],
        "status": "Draft",
        "docstatus": 0,
        "grand_total": 2.0,
        "currency": "AUD",
    }
    mock_app.tenant_db.insert_doc.return_value = mock_doc

    out, code = create_purchase_invoice("u@x.com")

    assert code == 201
    assert out["success"] is True
    assert out["doctype"] == "Purchase Invoice"
    assert out["id"] == "ACC-NEW"
    mock_app.tenant_db.insert_doc.assert_called_once()


def test_update_purchase_invoice_normalizes_dates_before_save(monkeypatch):
    sys.modules["flask"].request.get_json.return_value = {
        "posting_date": "2026-07-15",
        "supplier": "Test Supplier",
        "items": [{"item_code": "Miscellaneous", "qty": 1, "rate": 400}],
        "company": "Acme Pty Ltd",
    }
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {"docstatus": 0, "company": "Acme Pty Ltd"}

    mock_doc = MagicMock()
    mock_doc.flags = MagicMock()
    mock_doc.update = MagicMock()
    mock_doc.save = MagicMock()
    mock_doc.get = MagicMock(side_effect=lambda key, default=None: getattr(mock_doc, key, default))
    mock_doc.name = "ACC-PINV-2026-00185"
    mock_doc.docstatus = 0
    mock_doc.grand_total = 400.0
    mock_doc.currency = "AUD"
    mock_doc.status = "Draft"
    mock_app.tenant_db.get_doc.return_value = mock_doc
    mock_app.tenant_db.hooks.run_hooks = MagicMock()

    normalize_calls = []

    def _track_normalize(doc):
        normalize_calls.append(doc)

    monkeypatch.setattr(
        "expense_tracker.api.normalize_purchase_invoice_payment_dates",
        _track_normalize,
    )
    monkeypatch.setattr(
        "expense_tracker.api._project_purchase_invoice_api",
        lambda doc: {"id": doc.name, "name": doc.name},
    )
    monkeypatch.setattr(
        "expense_tracker.api.ensure_purchase_invoice_item_defaults",
        MagicMock(),
    )

    result = update_purchase_invoice("user@example.com", "ACC-PINV-2026-00185")

    assert result["success"] is True
    assert result["name"] == "ACC-PINV-2026-00185"
    assert len(normalize_calls) >= 2
    mock_doc.update.assert_called_once()
    mock_doc.save.assert_called_once()
    mock_app.tenant_db.hooks.run_hooks.assert_any_call(mock_doc, "before_validate")
    mock_app.tenant_db.hooks.run_hooks.assert_any_call(mock_doc, "validate")


def test_update_purchase_invoice_submit_flag_calls_submit_helper(monkeypatch):
    sys.modules["flask"].request.get_json.return_value = {"submit": 1}
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_frappe.db.get_value.return_value = {"docstatus": 0, "company": "Acme Pty Ltd"}

    submitted_doc = MagicMock()
    submitted_doc.name = "ACC-PINV-2026-00212"
    submitted_doc.docstatus = 1
    submitted_doc.status = "Paid"

    submit_mock = MagicMock(return_value=submitted_doc)
    monkeypatch.setattr("expense_tracker.api._submit_purchase_invoice_by_name", submit_mock)
    monkeypatch.setattr(
        "expense_tracker.api._project_purchase_invoice_api",
        lambda doc: {
            "id": doc.name,
            "name": doc.name,
            "docstatus": doc.docstatus,
            "status": "Paid",
        },
    )

    result = update_purchase_invoice("user@example.com", "ACC-PINV-2026-00212")

    submit_mock.assert_called_once_with("user@example.com", "ACC-PINV-2026-00212")
    assert result["success"] is True
    assert result["status"] == "Paid"
    assert result["docstatus"] == 1


# ── delete_purchase_invoice (resource DELETE: cancel if submitted, then delete) ─


def test_delete_purchase_invoice_submitted_cancels_then_deletes():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"

    def _gv(*args, **kwargs):
        if args and args[0] == "Purchase Invoice":
            return {"docstatus": 1, "company": "Acme Pty Ltd"}
        return None

    mock_frappe.db.get_value.side_effect = _gv

    mock_pi = MagicMock()
    mock_pi.flags = MagicMock()
    mock_pi.cancel = MagicMock()
    mock_app.tenant_db.get_doc.return_value = mock_pi

    result = delete_purchase_invoice("user@example.com", "ACC-PINV-2026-00001")

    assert result == {
        "success": True,
        "doctype": "Purchase Invoice",
        "message": "Purchase Invoice deleted",
    }
    mock_app.tenant_db.get_doc.assert_called_once_with(
        "Purchase Invoice", "ACC-PINV-2026-00001"
    )
    mock_pi.cancel.assert_called_once()
    assert mock_pi.flags.ignore_permissions is True
    mock_frappe.db.set_value.assert_not_called()
    mock_frappe.db.commit.assert_called()
    mock_app.tenant_db.delete_doc.assert_called_once_with(
        "Purchase Invoice",
        "ACC-PINV-2026-00001",
        force=True,
        ignore_permissions=True,
    )


def test_delete_purchase_invoice_draft_deletes_without_cancel():
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"

    def _gv(*args, **kwargs):
        if args and args[0] == "Purchase Invoice":
            return {"docstatus": 0, "company": "Acme Pty Ltd"}
        return None

    mock_frappe.db.get_value.side_effect = _gv

    result = delete_purchase_invoice("user@example.com", "PI-1")

    assert result["success"] is True
    mock_frappe.db.set_value.assert_not_called()
    mock_app.tenant_db.delete_doc.assert_called_once_with("Purchase Invoice", "PI-1")


def test_delete_purchase_invoice_cancelled_uses_force_delete():
    """Cancelled PI still links to GL Entry in ERPNext; delete must use force=True."""
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"

    def _gv(*args, **kwargs):
        if args and args[0] == "Purchase Invoice":
            return {"docstatus": 2, "company": "Acme Pty Ltd"}
        return None

    mock_frappe.db.get_value.side_effect = _gv

    result = delete_purchase_invoice("user@example.com", "PI-CXL")

    assert result["success"] is True
    mock_app.tenant_db.delete_doc.assert_called_once_with(
        "Purchase Invoice",
        "PI-CXL",
        force=True,
        ignore_permissions=True,
    )


