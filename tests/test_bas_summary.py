from datetime import date
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


mock_frappe = MagicMock()
mock_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
mock_frappe.ValidationError = type("ValidationError", (Exception,), {})

mock_app = MagicMock()
mock_app.db = MagicMock()
mock_app.tenant_db = mock_app.db

mock_microservice = MagicMock()
mock_microservice_controller = MagicMock()
mock_microservice.get_app.return_value = mock_app


def mock_secure_route(rule, **options):
    def decorator(f):
        return f

    return decorator


mock_app.secure_route.side_effect = mock_secure_route

sys.modules["frappe"] = mock_frappe
sys.modules["frappe_microservice"] = mock_microservice
sys.modules["frappe_microservice.controller"] = mock_microservice_controller

if "flask" not in sys.modules:
    _flask_stub = ModuleType("flask")
    _flask_stub.request = MagicMock()
    sys.modules["flask"] = _flask_stub


def _configure_frappe_throw():
    def _throw(msg, exc=Exception):
        if isinstance(exc, type) and issubclass(exc, Exception):
            raise exc(msg)
        raise Exception(msg)

    mock_frappe.throw.side_effect = _throw


_configure_frappe_throw()

from expense_tracker.api import get_bas_summary, _app_db  # noqa: E402
from expense_tracker.bas_summary import (  # noqa: E402
    build_bas_summary,
    compute_simpler_bas_from_gl,
    ensure_bas_accounts_configured,
    find_or_create_bas_report,
    resolve_bas_period,
    serialize_bas_summary,
)


class _FakeDoc(dict):
    def __init__(self, name="BAS-2026-04-01-00001", **fields):
        super().__init__(**fields)
        self.name = name
        self.insert = MagicMock()

    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture(autouse=True)
def reset_mocks():
    mock_app.db.reset_mock()
    mock_app.tenant_db.reset_mock()
    mock_frappe.db.reset_mock()
    mock_frappe.get_all.reset_mock()
    mock_frappe.get_all.side_effect = None
    mock_frappe.get_all.return_value = []
    mock_frappe.get_doc.reset_mock()
    mock_frappe.new_doc.reset_mock()
    mock_frappe.db.exists.return_value = True
    mock_frappe.db.table_exists.return_value = True
    mock_frappe.db.get_value.return_value = None
    mock_frappe.db.commit.reset_mock()
    mock_frappe.get_attr.reset_mock()
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    if hasattr(sys.modules["flask"], "request"):
        sys.modules["flask"].request.reset_mock()
        sys.modules["flask"].request.args = {}


def test_resolve_bas_period_quarter():
    today = date(2026, 6, 15)
    fd, td, label, preset = resolve_bas_period("quarter", None, None, today)
    assert preset == "quarter"
    assert fd == date(2026, 4, 1)
    assert td == date(2026, 6, 30)
    assert label == "Q2 2026"


def test_resolve_bas_period_month():
    today = date(2026, 6, 15)
    fd, td, label, preset = resolve_bas_period("month", None, None, today)
    assert preset == "month"
    assert fd == date(2026, 6, 1)
    assert td == today
    assert label == "June 2026"


def test_resolve_bas_period_custom_swaps_dates():
    fd, td, label, preset = resolve_bas_period(
        "custom", "2026-05-10", "2026-05-01", date(2026, 6, 1)
    )
    assert preset == "custom"
    assert fd == date(2026, 5, 1)
    assert td == date(2026, 5, 10)
    assert label == "May 2026"


def test_resolve_bas_period_invalid_raises():
    with pytest.raises(Exception, match="period must be one of"):
        resolve_bas_period("year", None, None, date(2026, 6, 1))


def test_ensure_bas_accounts_configured_missing_setup_raises():
    mock_frappe.db.exists.return_value = False
    with pytest.raises(mock_frappe.ValidationError, match="missing for"):
        ensure_bas_accounts_configured("Acme Pty Ltd")


def test_ensure_bas_accounts_configured_incomplete_accounts_raises():
    mock_frappe.db.get_value.return_value = {"account_1a": "", "account_1b": "GST Paid - A"}
    mock_frappe.get_all.return_value = []
    with pytest.raises(mock_frappe.ValidationError, match="not fully configured"):
        ensure_bas_accounts_configured("Acme Pty Ltd")


def test_find_or_create_bas_report_returns_existing():
    mock_frappe.get_all.return_value = [{"name": "BAS-EXISTING"}]
    name = find_or_create_bas_report("Acme Pty Ltd", date(2026, 4, 1), date(2026, 6, 30))
    assert name == "BAS-EXISTING"
    mock_frappe.new_doc.assert_not_called()


def test_find_or_create_bas_report_inserts_when_missing():
    mock_frappe.get_all.return_value = []
    created = _FakeDoc(name="BAS-NEW")
    mock_frappe.new_doc.return_value = created

    name = find_or_create_bas_report("Acme Pty Ltd", date(2026, 4, 1), date(2026, 6, 30))

    assert name == "BAS-NEW"
    created.insert.assert_called_once()
    mock_frappe.db.commit.assert_called_once()


def test_serialize_bas_summary_maps_labels():
    doc = _FakeDoc(
        g1=1100,
        **{"1a": 100, "1b": 40, "g11": 550},
        net_gst=60,
        reporting_method="Simpler BAS reporting method",
        bas_updation_datetime="2026-06-15 10:00:00",
    )
    payload = serialize_bas_summary(
        doc,
        company="Acme Pty Ltd",
        from_date=date(2026, 4, 1),
        to_date=date(2026, 6, 30),
        period_label="Q2 2026",
        preset="quarter",
        currency="AUD",
    )
    assert payload["g1"] == 1100.0
    assert payload["gst_collected_1a"] == 100.0
    assert payload["gst_paid_1b"] == 40.0
    assert payload["net_gst"] == 60.0
    assert payload["gst_to_pay"] == 60.0
    assert payload["gst_refund"] == 0.0
    assert payload["g11"] == 550.0
    assert payload["preset"] == "quarter"


def test_build_bas_summary_calls_get_gst_and_returns_payload():
    mock_frappe.db.get_value.return_value = {
        "account_1a": "GST Collected - A",
        "account_1b": "GST Paid - A",
    }
    mock_frappe.db.table_exists.return_value = True
    mock_frappe.get_all.side_effect = [
        ["Sales - A"],
        [{"name": "BAS-2026-04-01-00001"}],
    ]
    refreshed = _FakeDoc(
        g1=2000,
        **{"1a": 200, "1b": 80, "g11": 0},
        net_gst=120,
        reporting_method="Simpler BAS reporting method",
    )
    mock_frappe.get_doc.return_value = refreshed
    get_gst = MagicMock()
    mock_frappe.get_attr.return_value = get_gst
    mock_app.db.get_value.return_value = "AUD"

    result = build_bas_summary(
        mock_app.db,
        "Acme Pty Ltd",
        "quarter",
        None,
        None,
        today=date(2026, 6, 15),
    )

    get_gst.assert_called_once_with("BAS-2026-04-01-00001")
    assert result["company"] == "Acme Pty Ltd"
    assert result["gst_collected_1a"] == 200.0
    assert result["currency"] == "AUD"
    assert result["preset"] == "quarter"
    assert result["source"] == "au_bas_report"


def test_build_bas_summary_falls_back_to_gl_when_get_gst_unavailable():
    mock_frappe.get_attr.side_effect = Exception("module missing")
    mock_frappe.db.get_value.return_value = {
        "account_1a": "GST Collected - A",
        "account_1b": "GST Paid - A",
    }
    mock_frappe.db.table_exists.return_value = True
    mock_frappe.get_all.side_effect = [
        ["Sales - A"],
        [{"credit_in_account_currency": 100, "debit_in_account_currency": 0}],
        [{"debit_in_account_currency": 40, "credit_in_account_currency": 0}],
        [{"credit_in_account_currency": 500, "debit_in_account_currency": 0}],
    ]
    mock_app.db.get_value.return_value = "AUD"

    result = build_bas_summary(
        mock_app.db,
        "Acme Pty Ltd",
        "month",
        None,
        None,
        today=date(2026, 6, 15),
    )

    assert result["source"] == "gl"
    assert result["gst_collected_1a"] == 100.0
    assert result["gst_paid_1b"] == 40.0
    assert result["g1"] == 600.0
    assert result["bas_report"] is None


def test_compute_simpler_bas_from_gl_sums_accounts():
    mock_frappe.get_all.side_effect = [
        [{"credit_in_account_currency": 10, "debit_in_account_currency": 0}],
        [{"debit_in_account_currency": 4, "credit_in_account_currency": 0}],
        [{"credit_in_account_currency": 100, "debit_in_account_currency": 0}],
    ]
    amounts = compute_simpler_bas_from_gl(
        "Acme Pty Ltd",
        date(2026, 6, 1),
        date(2026, 6, 30),
        {
            "account_1a": "GST Collected - A",
            "account_1b": "GST Paid - A",
            "g1_accounts": ["Sales - A"],
        },
    )
    assert amounts["1a"] == 10.0
    assert amounts["1b"] == 4.0
    assert amounts["g1"] == 110.0
    assert amounts["net_gst"] == 6.0


def test_get_bas_summary_http_handler_uses_company_default():
    sys.modules["flask"].request.args = {"period": "month"}
    mock_frappe.get_attr.side_effect = Exception("module missing")
    mock_frappe.db.get_value.side_effect = lambda doctype, filters, field=None, **kw: (
        {"account_1a": "GST Collected - A", "account_1b": "GST Paid - A"}
        if doctype == "AU Simpler BAS Report Setup"
        else "AUD"
        if doctype == "Company"
        else None
    )
    mock_frappe.db.table_exists.return_value = True
    mock_frappe.get_all.side_effect = [
        ["Sales - A"],
        [{"credit_in_account_currency": 50, "debit_in_account_currency": 0}],
        [{"debit_in_account_currency": 20, "credit_in_account_currency": 0}],
        [{"credit_in_account_currency": 200, "debit_in_account_currency": 0}],
    ]

    result = get_bas_summary("test_user")

    assert result["preset"] == "month"
    assert result["g1"] == 250.0
    assert result["source"] == "gl"
    assert _app_db() == mock_app.tenant_db
