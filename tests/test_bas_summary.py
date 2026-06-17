from datetime import date
from unittest.mock import MagicMock
import sys

import pytest

# Initialise frappe / microservice stubs via test_purchase_invoice first so
# expense_tracker.api binds get_app() to the same tenant_db mock used elsewhere.
import tests.test_purchase_invoice as pi_test_env

mock_frappe = pi_test_env.mock_frappe
mock_app = pi_test_env.mock_app
_EmptyRequestArgs = pi_test_env._EmptyRequestArgs

from expense_tracker.api import get_bas_report, get_bas_summary, _app_db  # noqa: E402
from expense_tracker.bas_summary import (  # noqa: E402
    build_bas_report,
    build_bas_summary,
    compute_simpler_bas_from_gl,
    count_flagged_gst_transactions,
    ensure_bas_accounts_configured,
    find_or_create_bas_report,
    normalize_bas_report_dates,
    resolve_bas_period,
    serialize_bas_report,
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
    mock_app.db.get_all.side_effect = None
    mock_app.db.get_value.side_effect = None
    mock_app.db.exists.return_value = False
    mock_app.tenant_db.reset_mock()
    mock_frappe.db.reset_mock()
    mock_frappe.db.has_column = MagicMock(return_value=True)
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
    mock_frappe.get_attr.side_effect = pi_test_env._frappe_get_attr_side_effect
    mock_frappe.defaults = MagicMock()
    mock_frappe.defaults.get_user_default.return_value = "Acme Pty Ltd"
    mock_app.tenant_db.get_all.side_effect = lambda *args, **kwargs: mock_frappe.get_all(*args, **kwargs)
    mock_app.tenant_db.get_value.side_effect = lambda *args, **kwargs: mock_frappe.db.get_value(*args, **kwargs)
    mock_app.tenant_db.get_tenant_id.return_value = "test-tenant-001"
    if "flask" in sys.modules and hasattr(sys.modules["flask"], "request"):
        sys.modules["flask"].request.reset_mock()
        sys.modules["flask"].request.args = _EmptyRequestArgs()


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
    assert td == date(2026, 6, 30)
    assert label == "June 2026"


def test_resolve_bas_period_accepts_explicit_month_in_financial_year():
    fd, td, label, preset = resolve_bas_period(
        "month", "2026-05-01", "2026-05-31", date(2026, 6, 1)
    )
    assert preset == "month"
    assert fd == date(2026, 5, 1)
    assert td == date(2026, 5, 31)
    assert label == "May 2026"


def test_resolve_bas_period_rejects_dates_outside_financial_year():
    with pytest.raises(mock_frappe.ValidationError, match="financial year"):
        resolve_bas_period(
            "month", "2025-05-01", "2025-05-31", date(2026, 6, 1)
        )


def test_resolve_bas_period_invalid_raises():
    with pytest.raises(mock_frappe.ValidationError, match="period must be one of"):
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


def test_normalize_bas_report_dates_quarterly_snaps_june_to_q2():
    mock_frappe.db.get_value.return_value = "Quarterly"
    fd, td = normalize_bas_report_dates("Acme Pty Ltd", date(2026, 6, 1), date(2026, 6, 30))
    assert fd == date(2026, 4, 1)
    assert td == date(2026, 6, 30)


def test_normalize_bas_report_dates_monthly_keeps_calendar_month():
    mock_frappe.db.get_value.return_value = "Monthly"
    fd, td = normalize_bas_report_dates("Acme Pty Ltd", date(2026, 6, 1), date(2026, 6, 30))
    assert fd == date(2026, 6, 1)
    assert td == date(2026, 6, 30)


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


def test_find_or_create_bas_report_reuses_overlapping_quarter_for_june():
    mock_frappe.db.get_value.return_value = "Quarterly"
    mock_frappe.get_all.side_effect = [
        [],
        [{"name": "BAS-Q2", "start_date": "2026-04-01", "end_date": "2026-06-30"}],
    ]

    name = find_or_create_bas_report("Acme Pty Ltd", date(2026, 6, 1), date(2026, 6, 30))

    assert name == "BAS-Q2"
    mock_frappe.new_doc.assert_not_called()


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
    mock_frappe.get_attr.side_effect = (
        lambda path: get_gst
        if path.endswith("get_gst")
        else pi_test_env._frappe_get_attr_side_effect(path)
    )

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


def test_serialize_bas_report_groups_mobile_sections():
    payload = serialize_bas_report(
        {
            "company": "Acme Pty Ltd",
            "period": "Q1 2026",
            "preset": "quarter",
            "from_date": "2026-01-01",
            "to_date": "2026-03-31",
            "currency": "AUD",
            "g1": 82300.0,
            "g2": 1500.0,
            "g11": 57800.0,
            "gst_collected_1a": 7660.0,
            "gst_paid_1b": 5254.55,
            "net_gst": 2405.45,
            "gst_to_pay": 2405.45,
            "gst_refund": 0.0,
            "flagged_transactions_count": 3,
            "validation_message": "GST validation issues detected",
            "source": "au_bas_report",
        }
    )
    assert payload["sales"]["g1"] == 82300.0
    assert payload["sales"]["g2"] == 1500.0
    assert payload["sales"]["gst_on_sales_1a"] == 7660.0
    assert payload["purchases"]["g11"] == 57800.0
    assert payload["purchases"]["gst_on_purchases_1b"] == 5254.0
    assert payload["summary"]["net_gst_payable"] == 2405.0
    assert payload["alerts"]["flagged_transactions_count"] == 3
    assert payload["alerts"]["validation_message"] == "GST validation issues detected"
    assert payload["from_date"] == "2026-01-01"
    assert payload["to_date"] == "2026-03-31"


def test_count_flagged_gst_transactions_detects_missing_tax_rows():
    mock_frappe.get_all.side_effect = [
        [{"name": "PI-1", "taxes_and_charges": "GST Template"}],
        [],
    ]
    count, message = count_flagged_gst_transactions(
        "Acme Pty Ltd", date(2026, 1, 1), date(2026, 3, 31)
    )
    assert count == 1
    assert message == "GST validation issues detected"


def test_build_bas_report_includes_alerts_and_sections():
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
        [],
    ]
    mock_app.db.get_value.return_value = "AUD"

    result = build_bas_report(
        mock_app.db,
        "Acme Pty Ltd",
        "quarter",
        "2026-01-01",
        "2026-03-31",
        today=date(2026, 6, 15),
    )

    assert result["sales"]["g1"] == 600.0
    assert result["sales"]["gst_on_sales_1a"] == 100.0
    assert result["purchases"]["gst_on_purchases_1b"] == 40.0
    assert result["summary"]["net_gst_payable"] == 60.0
    assert result["alerts"]["flagged_transactions_count"] == 0
    assert result["from_date"] == "2026-01-01"
    assert result["to_date"] == "2026-03-31"


def test_get_bas_report_http_handler_uses_explicit_quarter_dates():
    sys.modules["flask"].request.args = {
        "period": "quarter",
        "from_date": "2026-01-01",
        "to_date": "2026-03-31",
    }
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
        [],
    ]

    result = get_bas_report("test_user")

    assert result["preset"] == "quarter"
    assert result["sales"]["g1"] == 250.0
    assert "alerts" in result
    assert "summary" in result
