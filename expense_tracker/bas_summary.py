"""AU Simpler BAS summary for mobile reporting (INX-643 MVP).

Prefers ERPNext Australian Localisation ``get_gst`` when the app module is installed;
otherwise aggregates GL entries using the same Simpler BAS rules as ``update_simpler_bas_report``.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe

_GET_GST_PATH = (
    "erpnext_australian_localisation.erpnext_australian_localisation"
    ".doctype.au_bas_report.au_bas_report.get_gst"
)


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def _floor_currency(value: Any) -> float:
    return float(math.floor(_as_number(value)))


def _table_exists(doctype: str) -> bool:
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _quarter_bounds(today: date) -> Tuple[date, date]:
    quarter = (today.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    from_date = date(today.year, start_month, 1)
    end_month = start_month + 2
    last_day = calendar.monthrange(today.year, end_month)[1]
    to_date = date(today.year, end_month, last_day)
    return from_date, to_date


def resolve_bas_period(
    preset: str,
    from_raw: Any,
    to_raw: Any,
    today: date,
) -> Tuple[date, date, str, str]:
    """Return (from_date, to_date, period_label, preset_key)."""
    p = (preset or "quarter").strip().lower()

    if p == "custom":
        fd = _safe_date(from_raw)
        td = _safe_date(to_raw)
        if not fd or not td:
            frappe.throw("custom period requires from_date and to_date (YYYY-MM-DD)")
        if fd > td:
            fd, td = td, fd
        if td - fd > timedelta(days=366):
            frappe.throw("Custom BAS date range cannot exceed 366 days")
        return fd, td, _period_label(fd, td), "custom"

    if p == "month":
        fd = today.replace(day=1)
        td = today
        return fd, td, today.strftime("%B %Y"), "month"

    if p in ("quarter", "q"):
        fd, td = _quarter_bounds(today)
        q = (fd.month - 1) // 3 + 1
        return fd, td, f"Q{q} {fd.year}", "quarter"

    frappe.throw("period must be one of: quarter, month, custom")


def _period_label(from_date: date, to_date: date) -> str:
    if from_date.month == to_date.month and from_date.year == to_date.year:
        return from_date.strftime("%B %Y")
    return f"{from_date.isoformat()} to {to_date.isoformat()}"


def ensure_bas_accounts_configured(company: str) -> Dict[str, Any]:
    """Validate AU Simpler BAS Report Setup before generating a report."""
    if not _table_exists("AU Simpler BAS Report Setup"):
        frappe.throw(
            "AU GST localisation is not installed on this site. "
            "Contact support to enable BAS reporting.",
            frappe.ValidationError,
        )
    if not frappe.db.exists("AU Simpler BAS Report Setup", company):
        frappe.throw(
            f"AU Simpler BAS Report Setup is missing for {company}. "
            "Complete GST account configuration in settings.",
            frappe.ValidationError,
        )

    row = frappe.db.get_value(
        "AU Simpler BAS Report Setup",
        company,
        ["account_1a", "account_1b"],
        as_dict=True,
    ) or {}
    g1_accounts: list[str] = []
    if _table_exists("Income Account for Simpler BAS"):
        g1_accounts = frappe.get_all(
            "Income Account for Simpler BAS",
            filters={
                "parent": company,
                "parenttype": "AU Simpler BAS Report Setup",
                "parentfield": "accounts_g1",
            },
            pluck="account",
        ) or []

    if not g1_accounts or not row.get("account_1a") or not row.get("account_1b"):
        frappe.throw(
            "BAS accounts (G1, 1A, 1B) are not fully configured. "
            "Complete AU Simpler BAS Report Setup for your company.",
            frappe.ValidationError,
        )
    return {
        "account_1a": row["account_1a"],
        "account_1b": row["account_1b"],
        "g1_accounts": g1_accounts,
    }


def _can_load_get_gst() -> bool:
    try:
        frappe.get_attr(_GET_GST_PATH)
        return True
    except Exception:
        return False


def _fetch_gl_rows(
    start_date: str,
    end_date: str,
    company: str,
    accounts: Union[str, List[str]],
) -> List[Dict[str, Any]]:
    if isinstance(accounts, str):
        accounts = [accounts]
    if not accounts:
        return []
    return frappe.get_all(
        "GL Entry",
        filters=[
            ["posting_date", ">=", start_date],
            ["posting_date", "<=", end_date],
            ["company", "=", company],
            ["account", "in", accounts],
            ["is_cancelled", "=", 0],
        ],
        fields=[
            "posting_date",
            "voucher_type",
            "voucher_no",
            "account",
            "credit_in_account_currency",
            "debit_in_account_currency",
        ],
        order_by="posting_date asc",
    ) or []


def _row_credit_minus_debit(row: Dict[str, Any]) -> float:
    return _as_number(row.get("credit_in_account_currency")) - _as_number(
        row.get("debit_in_account_currency")
    )


def _row_debit_minus_credit(row: Dict[str, Any]) -> float:
    return _as_number(row.get("debit_in_account_currency")) - _as_number(
        row.get("credit_in_account_currency")
    )


def compute_simpler_bas_from_gl(
    company: str,
    from_date: date,
    to_date: date,
    accounts: Dict[str, Any],
) -> Dict[str, float]:
    """Simpler BAS totals from GL (mirrors ``update_simpler_bas_report``)."""
    if not _table_exists("GL Entry"):
        frappe.throw(
            "GL Entry is not available on this site.",
            frappe.ValidationError,
        )

    start_s = from_date.isoformat()
    end_s = to_date.isoformat()
    account_1a = accounts["account_1a"]
    account_1b = accounts["account_1b"]
    g1_accounts = accounts["g1_accounts"]

    entries_1a = _fetch_gl_rows(start_s, end_s, company, account_1a)
    amount_1a = sum(_row_credit_minus_debit(row) for row in entries_1a)

    entries_1b = _fetch_gl_rows(start_s, end_s, company, account_1b)
    amount_1b = sum(_row_debit_minus_credit(row) for row in entries_1b)

    entries_g1 = _fetch_gl_rows(start_s, end_s, company, g1_accounts)
    if entries_1a:
        entries_g1 = list(entries_g1) + list(entries_1a)
    entries_g1.sort(
        key=lambda row: (
            str(row.get("posting_date") or ""),
            str(row.get("voucher_no") or ""),
        )
    )
    amount_g1 = sum(_row_credit_minus_debit(row) for row in entries_g1)

    amount_1a_f = float(math.floor(amount_1a))
    amount_1b_f = float(math.floor(amount_1b))
    amount_g1_f = float(math.floor(amount_g1))
    return {
        "g1": amount_g1_f,
        "1a": amount_1a_f,
        "1b": amount_1b_f,
        "net_gst": float(math.floor(abs(amount_1a_f - amount_1b_f))),
        "g11": 0.0,
    }


def find_or_create_bas_report(company: str, from_date: date, to_date: date) -> str:
    """Return AU BAS Report name for the exact period (create when missing)."""
    if not _table_exists("AU BAS Report"):
        frappe.throw(
            "AU BAS Report is not available on this site.",
            frappe.ValidationError,
        )

    start_s = from_date.isoformat()
    end_s = to_date.isoformat()
    existing = frappe.get_all(
        "AU BAS Report",
        filters={"company": company, "start_date": start_s, "end_date": end_s},
        fields=["name"],
        limit=1,
    )
    if existing:
        return str(existing[0].get("name") or "")

    doc = frappe.new_doc("AU BAS Report")
    doc.company = company
    doc.start_date = start_s
    doc.end_date = end_s
    doc.reporting_status = "In Review"
    doc.reporting_method = "Simpler BAS reporting method"
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return str(doc.name)


def refresh_bas_report(report_name: str):
    """Populate BAS labels via ERPNext Australian Localisation."""
    get_gst = frappe.get_attr(_GET_GST_PATH)
    get_gst(report_name)
    return frappe.get_doc("AU BAS Report", report_name)


def _payload_from_amounts(
    amounts: Dict[str, float],
    *,
    company: str,
    from_date: date,
    to_date: date,
    period_label: str,
    preset: str,
    currency: str,
    bas_report: str = "",
    updated_at: Optional[str] = None,
    source: str = "gl",
) -> Dict[str, Any]:
    amount_1a = _floor_currency(amounts.get("1a"))
    amount_1b = _floor_currency(amounts.get("1b"))
    net_gst = _floor_currency(amounts.get("net_gst", abs(amount_1a - amount_1b)))
    gst_to_pay = max(0.0, amount_1a - amount_1b)
    gst_refund = max(0.0, amount_1b - amount_1a)
    return {
        "company": company,
        "period": period_label,
        "preset": preset,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "currency": currency,
        "g1": _floor_currency(amounts.get("g1")),
        "g11": _floor_currency(amounts.get("g11")),
        "gst_collected_1a": amount_1a,
        "gst_paid_1b": amount_1b,
        "net_gst": net_gst,
        "gst_to_pay": round(gst_to_pay, 2),
        "gst_refund": round(gst_refund, 2),
        "reporting_method": "Simpler BAS reporting method",
        "bas_report": bas_report or None,
        "updated_at": updated_at,
        "source": source,
        "note": "Figures based on submitted GL entries in the selected period.",
    }


def serialize_bas_summary(
    doc: Any,
    *,
    company: str,
    from_date: date,
    to_date: date,
    period_label: str,
    preset: str,
    currency: str,
) -> Dict[str, Any]:
    g1 = _floor_currency(doc.get("g1"))
    amount_1a = _floor_currency(doc.get("1a"))
    amount_1b = _floor_currency(doc.get("1b"))
    net_gst = _floor_currency(doc.get("net_gst"))
    g11 = _floor_currency(doc.get("g11"))
    gst_to_pay = max(0.0, amount_1a - amount_1b)
    gst_refund = max(0.0, amount_1b - amount_1a)

    return _payload_from_amounts(
        {
            "g1": g1,
            "g11": g11,
            "1a": amount_1a,
            "1b": amount_1b,
            "net_gst": net_gst,
        },
        company=company,
        from_date=from_date,
        to_date=to_date,
        period_label=period_label,
        preset=preset,
        currency=currency,
        bas_report=str(getattr(doc, "name", "") or doc.get("name") or ""),
        updated_at=doc.get("bas_updation_datetime"),
        source="au_bas_report",
    )


def build_bas_summary(
    db: Any,
    company: str,
    preset: str,
    from_raw: Any,
    to_raw: Any,
    *,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Compute BAS summary for the tenant company and period."""
    if not company:
        frappe.throw("Company is required")

    today = today or date.today()
    from_date, to_date, period_label, preset_key = resolve_bas_period(
        preset, from_raw, to_raw, today
    )
    accounts = ensure_bas_accounts_configured(company)
    currency = db.get_value("Company", company, "default_currency") or "AUD"

    if _can_load_get_gst() and _table_exists("AU BAS Report"):
        try:
            report_name = find_or_create_bas_report(company, from_date, to_date)
            doc = refresh_bas_report(report_name)
            return serialize_bas_summary(
                doc,
                company=company,
                from_date=from_date,
                to_date=to_date,
                period_label=period_label,
                preset=preset_key,
                currency=currency,
            )
        except Exception:
            pass

    amounts = compute_simpler_bas_from_gl(company, from_date, to_date, accounts)
    return _payload_from_amounts(
        amounts,
        company=company,
        from_date=from_date,
        to_date=to_date,
        period_label=period_label,
        preset=preset_key,
        currency=currency,
        source="gl",
    )
