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
_GET_QUARTER_DATES_PATH = (
    "erpnext_australian_localisation.erpnext_australian_localisation"
    ".doctype.au_bas_report.au_bas_report.get_quaterly_start_end_date"
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


def _au_financial_year_bounds(today: date) -> Tuple[date, date]:
    """Australian financial year: 1 July – 30 June."""
    if today.month >= 7:
        return date(today.year, 7, 1), date(today.year + 1, 6, 30)
    return date(today.year - 1, 7, 1), date(today.year, 6, 30)


def _validate_bas_period_in_financial_year(from_date: date, to_date: date, today: date) -> None:
    fy_start, fy_end = _au_financial_year_bounds(today)
    if from_date < fy_start or to_date > fy_end:
        frappe.throw(
            "BAS period must fall within the current financial year (1 July – 30 June).",
            frappe.ValidationError,
        )


def resolve_bas_period(
    preset: str,
    from_raw: Any,
    to_raw: Any,
    today: date,
) -> Tuple[date, date, str, str]:
    """Return (from_date, to_date, period_label, preset_key).

    BAS supports **monthly** or **quarterly** periods only, within the current AU
    financial year (July–June). Explicit ``from_date``/``to_date`` are accepted when
    they denote a full calendar month or quarter inside that window.
    """
    p = (preset or "quarter").strip().lower()
    if p not in ("month", "quarter", "q"):
        frappe.throw("period must be one of: quarter, month", frappe.ValidationError)

    fd = _safe_date(from_raw)
    td = _safe_date(to_raw)
    if fd and td:
        if fd > td:
            fd, td = td, fd
        _validate_bas_period_in_financial_year(fd, td, today)
        preset_key = "month" if p == "month" else "quarter"
        if preset_key == "month":
            last_day = calendar.monthrange(fd.year, fd.month)[1]
            fd = date(fd.year, fd.month, 1)
            td = date(fd.year, fd.month, last_day)
        else:
            fd, td = _quarter_bounds(fd)
        _validate_bas_period_in_financial_year(fd, td, today)
        label = (
            fd.strftime("%B %Y")
            if preset_key == "month"
            else f"Q{(fd.month - 1) // 3 + 1} {fd.year}"
        )
        return fd, td, label, preset_key

    if p == "month":
        last_day = calendar.monthrange(today.year, today.month)[1]
        fd = date(today.year, today.month, 1)
        td = date(today.year, today.month, last_day)
        _validate_bas_period_in_financial_year(fd, td, today)
        return fd, td, today.strftime("%B %Y"), "month"

    fd, td = _quarter_bounds(today)
    _validate_bas_period_in_financial_year(fd, td, today)
    q = (fd.month - 1) // 3 + 1
    return fd, td, f"Q{q} {fd.year}", "quarter"


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


def find_or_create_bas_report(
    company: str,
    from_date: date,
    to_date: date,
    *,
    today: Optional[date] = None,
) -> str:
    """Return AU BAS Report name for the company period (create when missing).

    Dates are normalised to the company's Monthly or Quarterly BAS cycle before
    lookup/insert, matching AU Localisation desk behaviour. Overlapping reports are
    reused instead of failing insert with "BAS Report found for this period".
    """
    if not _table_exists("AU BAS Report"):
        frappe.throw(
            "AU BAS Report is not available on this site.",
            frappe.ValidationError,
        )

    today = today or date.today()
    norm_from, norm_to = normalize_bas_report_dates(company, from_date, to_date, today=today)

    _validate_bas_period_in_financial_year(norm_from, norm_to, today)

    existing = find_bas_report_name_exact(company, norm_from, norm_to)
    if existing:
        return existing

    overlap = find_overlapping_bas_report(company, norm_from, norm_to)
    if overlap:
        return overlap

    doc = frappe.new_doc("AU BAS Report")
    doc.company = company
    doc.start_date = norm_from.isoformat()
    doc.end_date = norm_to.isoformat()
    doc.reporting_status = "In Review"
    doc.reporting_method = "Simpler BAS reporting method"
    try:
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return str(doc.name)
    except frappe.ValidationError as exc:
        if "BAS Report found for this period" in str(exc):
            recovered = find_overlapping_bas_report(company, norm_from, norm_to)
            if recovered:
                return recovered
        raise


def _periods_overlap(
    start_a: date,
    end_a: date,
    start_b: date,
    end_b: date,
) -> bool:
    return start_a <= end_b and end_a >= start_b


def get_company_bas_reporting_period(company: str) -> str:
    if not _table_exists("AU BAS Reporting Period"):
        return "Quarterly"
    period = frappe.db.get_value(
        "AU BAS Reporting Period",
        {"company": company},
        "reporting_period",
    )
    if period in ("Monthly", "Quarterly"):
        return period
    return "Quarterly"


def normalize_bas_report_dates(
    company: str,
    from_date: date,
    to_date: date,
    *,
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """Snap to calendar month or AU quarter per company BAS settings."""
    today = today or date.today()
    reporting_period = get_company_bas_reporting_period(company)
    anchor = from_date if from_date <= to_date else to_date

    if reporting_period == "Monthly":
        month_start = date(anchor.year, anchor.month, 1)
        last_day = calendar.monthrange(anchor.year, anchor.month)[1]
        month_end = date(anchor.year, anchor.month, last_day)
        _validate_bas_period_in_financial_year(month_start, month_end, today)
        return month_start, month_end

    try:
        get_quarter_dates = frappe.get_attr(_GET_QUARTER_DATES_PATH)
        result = get_quarter_dates(anchor.isoformat())
        if isinstance(result, (list, tuple)) and len(result) >= 2:
            quarter_start = _safe_date(result[0])
            quarter_end = _safe_date(result[1])
            if quarter_start and quarter_end:
                norm_from, norm_to = quarter_start, quarter_end
                _validate_bas_period_in_financial_year(norm_from, norm_to, today)
                return norm_from, norm_to
    except Exception:
        pass

    norm_from, norm_to = _quarter_bounds(anchor)
    _validate_bas_period_in_financial_year(norm_from, norm_to, today)
    return norm_from, norm_to


def find_bas_report_name_exact(
    company: str,
    from_date: date,
    to_date: date,
) -> Optional[str]:
    existing = frappe.get_all(
        "AU BAS Report",
        filters={
            "company": company,
            "start_date": from_date.isoformat(),
            "end_date": to_date.isoformat(),
        },
        fields=["name"],
        limit=1,
    )
    if existing:
        return str(existing[0].get("name") or "")
    return None


def find_overlapping_bas_report(
    company: str,
    from_date: date,
    to_date: date,
) -> Optional[str]:
    years = {from_date.year, to_date.year}
    rows: List[Dict[str, Any]] = []
    for year in sorted(years):
        rows.extend(
            frappe.get_all(
                "AU BAS Report",
                filters={
                    "company": company,
                    "start_date": ["like", f"{year}%"],
                },
                fields=["name", "start_date", "end_date"],
            )
        )

    matches: List[Tuple[str, date, date]] = []
    for row in rows:
        name = str(row.get("name") or "")
        row_start = _safe_date(row.get("start_date"))
        row_end = _safe_date(row.get("end_date"))
        if not name or not row_start or not row_end:
            continue
        if _periods_overlap(from_date, to_date, row_start, row_end):
            matches.append((name, row_start, row_end))

    if not matches:
        return None

    for name, row_start, row_end in matches:
        if row_start == from_date and row_end == to_date:
            return name

    for name, row_start, row_end in matches:
        if row_start <= from_date and row_end >= to_date:
            return name

    if len(matches) == 1:
        return matches[0][0]

    return matches[0][0]


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
        "g2": _floor_currency(amounts.get("g2")),
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
    g2 = _floor_currency(doc.get("g2"))
    gst_to_pay = max(0.0, amount_1a - amount_1b)
    gst_refund = max(0.0, amount_1b - amount_1a)

    return _payload_from_amounts(
        {
            "g1": g1,
            "g2": g2,
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
            report_name = find_or_create_bas_report(
                company, from_date, to_date, today=today
            )
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


def _is_gst_tax_row(row: Dict[str, Any]) -> bool:
    desc = str(row.get("description") or "").lower()
    head = str(row.get("account_head") or "").lower()
    return "gst" in desc or "gst" in head


def count_flagged_gst_transactions(
    company: str,
    from_date: date,
    to_date: date,
) -> Tuple[int, str]:
    """Count purchase invoices in the period with missing or inconsistent GST rows."""
    if not _table_exists("Purchase Invoice"):
        return 0, ""

    invoices = frappe.get_all(
        "Purchase Invoice",
        filters={
            "company": company,
            "docstatus": ["<", 2],
            "posting_date": ["between", [from_date.isoformat(), to_date.isoformat()]],
        },
        fields=["name", "taxes_and_charges"],
    ) or []

    flagged = 0
    for inv in invoices:
        name = inv.get("name")
        if not name:
            continue
        taxes = frappe.get_all(
            "Purchase Taxes and Charges",
            filters={"parent": name, "parenttype": "Purchase Invoice"},
            fields=["description", "account_head", "rate", "tax_amount"],
        ) or []
        if inv.get("taxes_and_charges") and not taxes:
            flagged += 1
            continue
        for row in taxes:
            if not _is_gst_tax_row(row):
                continue
            rate = _as_number(row.get("rate"))
            tax_amount = _as_number(row.get("tax_amount"))
            account_head = str(row.get("account_head") or "").strip()
            if not account_head or (rate > 0 and tax_amount <= 0):
                flagged += 1
                break

    if flagged:
        return flagged, "GST validation issues detected"
    return 0, ""


def serialize_bas_report(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Shape BAS summary for the mobile BAS Report screen."""
    amount_1a = _floor_currency(summary.get("gst_collected_1a"))
    amount_1b = _floor_currency(summary.get("gst_paid_1b"))
    net_gst = _floor_currency(summary.get("net_gst", abs(amount_1a - amount_1b)))
    gst_to_pay = _floor_currency(summary.get("gst_to_pay", max(0.0, amount_1a - amount_1b)))
    gst_refund = _floor_currency(summary.get("gst_refund", max(0.0, amount_1b - amount_1a)))
    g2 = _floor_currency(summary.get("g2"))
    g11 = _floor_currency(summary.get("g11"))
    flagged_count = int(summary.get("flagged_transactions_count") or 0)
    validation_message = str(summary.get("validation_message") or "")

    return {
        "company": summary.get("company"),
        "period": summary.get("period"),
        "preset": summary.get("preset"),
        "from_date": summary.get("from_date"),
        "to_date": summary.get("to_date"),
        "currency": summary.get("currency"),
        "sales": {
            "g1": _floor_currency(summary.get("g1")),
            "g2": g2,
            "gst_on_sales_1a": amount_1a,
        },
        "purchases": {
            "g11": g11,
            "gst_on_purchases_1b": amount_1b,
        },
        "summary": {
            "net_gst_payable": net_gst,
            "gst_to_pay": gst_to_pay,
            "gst_refund": gst_refund,
        },
        "alerts": {
            "flagged_transactions_count": flagged_count,
            "validation_message": validation_message,
        },
        "bas_report": summary.get("bas_report"),
        "updated_at": summary.get("updated_at"),
        "source": summary.get("source"),
        "reporting_method": summary.get("reporting_method"),
        # Flat BAS codes for clients that prefer a single-level map.
        "g1": _floor_currency(summary.get("g1")),
        "g2": g2,
        "g11": g11,
        "gst_collected_1a": amount_1a,
        "gst_paid_1b": amount_1b,
        "net_gst": net_gst,
        "flagged_transactions_count": flagged_count,
        "validation_message": validation_message,
    }


def build_bas_report(
    db: Any,
    company: str,
    preset: str,
    from_raw: Any,
    to_raw: Any,
    *,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """BAS report payload for the mobile screen (sales, purchases, summary, alerts)."""
    summary = build_bas_summary(
        db,
        company,
        preset,
        from_raw,
        to_raw,
        today=today,
    )
    from_date = _safe_date(summary.get("from_date"))
    to_date = _safe_date(summary.get("to_date"))
    if not from_date or not to_date:
        frappe.throw("Invalid BAS report period")

    flagged_count, validation_message = count_flagged_gst_transactions(
        company, from_date, to_date
    )
    summary["flagged_transactions_count"] = flagged_count
    summary["validation_message"] = validation_message
    return serialize_bas_report(summary)
