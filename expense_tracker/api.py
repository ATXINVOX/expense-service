from __future__ import annotations

import calendar
import datetime as _dt
import logging
import re
from datetime import date, datetime, timedelta
import frappe
from frappe_microservice import get_app

from controllers.purchase_invoice import (
    _company_default_currency,
    _expense_title,
    clear_account_cache_for_company,
    clear_company_currency_cache,
    ensure_purchase_invoice_item_defaults,
    ensure_purchase_invoice_submit_prereqs,
)

logger = logging.getLogger(__name__)

_MAX_NAME_LENGTH = 140
_VALID_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-_. ]{0,139}$')


def _build_error(message, code, error_type=None):
    """Build a consistent error response."""
    resp = {"status": "error", "code": code, "message": message}
    if error_type:
        resp["type"] = error_type
    return resp, code


def _validate_name(name_raw):
    """Validate and sanitize invoice name. Returns (name, error_response)."""
    import urllib.parse
    if not name_raw:
        return None, _build_error(
            "name or invoice_name is required in JSON body",
            400, "ValidationError"
        )
    name = urllib.parse.unquote(str(name_raw)).strip()
    if not name:
        return None, _build_error(
            "Invoice name cannot be empty",
            400, "ValidationError"
        )
    if len(name) > _MAX_NAME_LENGTH:
        return None, _build_error(
            f"Invoice name too long (max {_MAX_NAME_LENGTH} characters)",
            400, "ValidationError"
        )
    if not _VALID_NAME_RE.match(name):
        return None, _build_error(
            "Invalid invoice name format",
            400, "ValidationError"
        )
    return name, None


def _safe_date(value, default_factory):
    if value is None:
        return default_factory()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        # Keep compatibility if an explicit date parser is not available in this context.
        return default_factory()


def _get_frappe_today():
    try:
        from datetime import date
        d = frappe.utils.getdate(frappe.utils.nowdate())
        if type(d).__name__ == 'MagicMock':
            return date.today()
        return d
    except Exception:
        from datetime import date
        return date.today()

def _default_from_date():
    today = _get_frappe_today()
    return today.replace(day=1)


def _period_label(from_date: date, to_date: date) -> str:
    if from_date.month == to_date.month and from_date.year == to_date.year:
        return from_date.strftime("%B %Y")
    return f"{from_date.strftime('%B %Y')} - {to_date.strftime('%B %Y')}"


_CATEGORY_COLOR_PALETTE = (
    "#2563EB",
    "#DC2626",
    "#9333EA",
    "#EA580C",
    "#16A34A",
    "#06B6D4",
    "#64748B",
    "#F59E0B",
)

# Donut chart: rolled-up slice when more than dashboard category limit.
_OTHERS_SLICE_COLOR = "#94A3B8"
_BREAKDOWN_TOP_N = 4


def _breakdown_top_categories(
    enriched_breakdown: list, total_spend: float, top_n: int = _BREAKDOWN_TOP_N
) -> list:
    """Top ``top_n`` categories by amount; remainder combined as ``Others``."""
    if not enriched_breakdown:
        return []
    n = max(1, min(int(top_n), 50))
    if len(enriched_breakdown) <= n:
        return [{**row} for row in enriched_breakdown]
    head = [{**row} for row in enriched_breakdown[:n]]
    rest_total = sum(_as_number(r.get("total")) for r in enriched_breakdown[n:])
    rest_pct = (
        round((rest_total / total_spend * 100.0), 2) if total_spend > 0 else 0.0
    )
    head.append(
        {
            "item_group": "Others",
            "total": round(rest_total, 2),
            "pct": rest_pct,
            "color": _OTHERS_SLICE_COLOR,
        }
    )
    return head


def _parse_posting_date_value(pd):
    """Normalize Purchase Invoice ``posting_date`` to a ``date`` or ``None``.

    Uses ``datetime.date`` from the stdlib module for ``isinstance`` so tests can
    patch ``expense_tracker.api.date`` without breaking recognition of DB dates.
    """
    if pd is None:
        return None
    if isinstance(pd, _dt.datetime):
        return pd.date()
    if isinstance(pd, _dt.date):
        return pd
    if isinstance(pd, str):
        try:
            return _dt.datetime.fromisoformat(pd.replace("Z", "+00:00")).date()
        except Exception:
            return None
    return None


def _monday_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _dashboard_week_bounds(d: date):
    mon = _monday_week_start(d)
    sun = mon + timedelta(days=6)
    return mon, sun


def _dashboard_prior_week_bounds(d: date):
    mon, _ = _dashboard_week_bounds(d)
    prev_mon = mon - timedelta(days=7)
    prev_sun = mon - timedelta(days=1)
    return prev_mon, prev_sun


def _dashboard_month_mtd_bounds(d: date):
    return d.replace(day=1), d


def _dashboard_prior_month_mtd_bounds(d: date):
    first_this = d.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    py, pm = last_prev.year, last_prev.month
    prev_start = date(py, pm, 1)
    last_dom = calendar.monthrange(py, pm)[1]
    target_day = min(d.day, last_dom)
    prev_end = date(py, pm, target_day)
    return prev_start, prev_end


def _dashboard_year_ytd_bounds(d: date):
    return date(d.year, 1, 1), d


def _dashboard_prior_year_ytd_bounds(d: date):
    y = d.year - 1
    last_dom = calendar.monthrange(y, d.month)[1]
    dom = min(d.day, last_dom)
    return date(y, 1, 1), date(y, d.month, dom)


def _resolve_dashboard_period(preset: str, today: date):
    """Main window, comparison window, and UI labels (preset is week|month|year)."""
    p = (preset or "").strip().lower()
    if p == "week":
        fd, td = _dashboard_week_bounds(today)
        pfd, ptd = _dashboard_prior_week_bounds(today)
        label = f"Week of {fd.strftime('%d %b %Y')}"
        cmp_label = "vs last week"
        return fd, td, pfd, ptd, label, cmp_label
    if p == "month":
        fd, td = _dashboard_month_mtd_bounds(today)
        pfd, ptd = _dashboard_prior_month_mtd_bounds(today)
        label = td.strftime("%B %Y")
        cmp_label = "vs last month"
        return fd, td, pfd, ptd, label, cmp_label
    if p == "year":
        fd, td = _dashboard_year_ytd_bounds(today)
        pfd, ptd = _dashboard_prior_year_ytd_bounds(today)
        label = str(today.year)
        cmp_label = "vs last year"
        return fd, td, pfd, ptd, label, cmp_label
    raise AssertionError("invalid preset for _resolve_dashboard_period")


def _trend_pct_vs_previous(current_total: float, previous_total: float) -> float:
    if previous_total > 0:
        return round((current_total - previous_total) / previous_total * 100.0, 2)
    if current_total > 0:
        return 100.0
    return 0.0


def _cashflow_stats_from_amounts(amounts: list[float]) -> dict:
    if not amounts:
        return {"highest": 0.0, "lowest": 0.0, "average": 0.0}
    return {
        "highest": round(max(amounts), 2),
        "lowest": round(min(amounts), 2),
        "average": round(sum(amounts) / len(amounts), 2),
    }


def _cashflow_week_series(rows, week_start: date) -> list[dict]:
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    buckets = [0.0] * 7
    for row in rows or []:
        pd = _parse_posting_date_value(row.get("posting_date"))
        if not pd:
            continue
        idx = (pd - week_start).days
        if 0 <= idx <= 6:
            buckets[idx] += _as_number(row.get("grand_total"))
    return [{"label": labels[i], "amount": round(buckets[i], 2)} for i in range(7)]


def _cashflow_month_week_segments(rows, range_start: date, range_end: date) -> list[dict]:
    labels = ["W1", "W2", "W3", "W4"]
    buckets = [0.0, 0.0, 0.0, 0.0]
    for row in rows or []:
        pd = _parse_posting_date_value(row.get("posting_date"))
        if not pd or pd < range_start or pd > range_end:
            continue
        dom = pd.day
        if dom <= 7:
            buckets[0] += _as_number(row.get("grand_total"))
        elif dom <= 14:
            buckets[1] += _as_number(row.get("grand_total"))
        elif dom <= 21:
            buckets[2] += _as_number(row.get("grand_total"))
        else:
            buckets[3] += _as_number(row.get("grand_total"))
    return [{"label": labels[i], "amount": round(buckets[i], 2)} for i in range(4)]


def _cashflow_year_months(rows, year: int, range_end: date) -> list[dict]:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    buckets = [0.0] * 12
    for row in rows or []:
        pd = _parse_posting_date_value(row.get("posting_date"))
        if not pd or pd.year != year or pd > range_end:
            continue
        buckets[pd.month - 1] += _as_number(row.get("grand_total"))
    return [{"label": months[i], "amount": round(buckets[i], 2)} for i in range(12)]


def _cashflow_daily_range(rows, range_start: date, range_end: date) -> list[dict]:
    span = (range_end - range_start).days + 1
    buckets = [0.0] * span
    labels = []
    for i in range(span):
        d = range_start + timedelta(days=i)
        labels.append(d.strftime("%d %b"))
    for row in rows or []:
        pd = _parse_posting_date_value(row.get("posting_date"))
        if not pd or pd < range_start or pd > range_end:
            continue
        idx = (pd - range_start).days
        buckets[idx] += _as_number(row.get("grand_total"))
    return [{"label": labels[i], "amount": round(buckets[i], 2)} for i in range(span)]


def _cashflow_monthly_range(rows, range_start: date, range_end: date) -> list[dict]:
    months: list[tuple[int, int]] = []
    y, m = range_start.year, range_start.month
    while (y, m) <= (range_end.year, range_end.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    labels = [calendar.month_abbr[m] for _y, m in months]
    buckets = [0.0] * len(months)
    month_index = {(yy, mm): i for i, (yy, mm) in enumerate(months)}
    for row in rows or []:
        pd = _parse_posting_date_value(row.get("posting_date"))
        if not pd or pd < range_start or pd > range_end:
            continue
        key = (pd.year, pd.month)
        if key in month_index:
            buckets[month_index[key]] += _as_number(row.get("grand_total"))
    return [{"label": labels[i], "amount": round(buckets[i], 2)} for i in range(len(months))]


def _cashflow_custom_range(rows, range_start: date, range_end: date) -> list[dict]:
    span = (range_end - range_start).days + 1
    if span <= 7:
        return _cashflow_daily_range(rows, range_start, range_end)
    if span <= 31:
        return _cashflow_month_week_segments(rows, range_start, range_end)
    if range_start.year == range_end.year:
        return _cashflow_year_months(rows, range_start.year, range_end)
    return _cashflow_monthly_range(rows, range_start, range_end)


def _resolve_dashboard_custom_bounds(from_raw, to_raw):
    """Explicit from/to window and equal-length prior window for trend."""
    fd = _safe_date(from_raw, lambda: None)
    td = _safe_date(to_raw, lambda: None)
    if not fd or not td:
        frappe.throw("custom range requires from_date and to_date (YYYY-MM-DD)")
    if fd > td:
        fd, td = td, fd
    span_days = (td - fd).days + 1
    if span_days > 366:
        frappe.throw("Custom date range cannot exceed 366 days")
    prev_to = fd - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span_days - 1)
    if fd.month == td.month and fd.year == td.year:
        label = fd.strftime("%B %Y")
    else:
        label = f"{fd.strftime('%d %b %Y')} – {td.strftime('%d %b %Y')}"
    return fd, td, prev_from, prev_to, label, "vs prior period"


def _resolve_tenant_id(db) -> str:
    tenant_id = ""
    try:
        get_tid = getattr(db, "get_tenant_id", None)
        if callable(get_tid):
            raw_tid = get_tid()
            if isinstance(raw_tid, str):
                tenant_id = raw_tid.strip()
    except Exception:
        pass
    return tenant_id


def _tenant_or_filters(tenant_id: str) -> list:
    return [
        ["tenant_id", "=", tenant_id],
        ["tenant_id", "=", "SYSTEM"],
        ["tenant_id", "is", "not set"],
        ["tenant_id", "=", ""],
    ]


def _fetch_recent_purchase_invoices(company: str, limit: int, fields: list[str]):
    """Latest purchase invoices for dashboard — not limited to the chart period window.

    Uses saas_platform tenant visibility (own tenant + SYSTEM + legacy unset rows).
  """
    db = _app_db()
    tenant_id = _resolve_tenant_id(db)
    base_filters = [
        ["company", "=", company],
        ["docstatus", "<", 2],
    ]
    row_limit = max(1, min(int(limit or 10), 50))

    if tenant_id:
        return frappe.get_all(
            "Purchase Invoice",
            filters=base_filters,
            or_filters=_tenant_or_filters(tenant_id),
            fields=fields,
            order_by="modified desc",
            limit=row_limit,
        )

    return db.get_all(
        "Purchase Invoice",
        filters=base_filters,
        fields=fields,
        order_by="modified desc",
        limit=row_limit,
    )


def _fetch_recent_sales_invoices(company: str, limit: int):
    """Latest sales invoices for financial-dashboard activity (no period window)."""
    db = _app_db()
    tenant_id = _resolve_tenant_id(db)
    base_filters = [
        ["company", "=", company],
        ["docstatus", "<", 2],
    ]
    fields = [
        "name",
        "customer",
        "posting_date",
        "grand_total",
        "modified",
        "status",
    ]
    row_limit = max(1, min(int(limit or 20), 50))

    if tenant_id:
        return frappe.get_all(
            "Sales Invoice",
            filters=base_filters,
            or_filters=_tenant_or_filters(tenant_id),
            fields=fields,
            order_by="modified desc",
            limit=row_limit,
        )

    return db.get_all(
        "Sales Invoice",
        filters=base_filters,
        fields=fields,
        order_by="modified desc",
        limit=row_limit,
    )


def _recent_expenses_from_rows(rows, limit: int = 10) -> list[dict]:
    """Project dashboard recent expenses (rows already ordered by modified desc)."""
    out = []
    for row in (rows or [])[: max(1, min(int(limit or 10), 50))]:
        out.append(
            {
                "id": row.get("name"),
                "name": row.get("name"),
                "supplier": row.get("supplier"),
                "posting_date": _fmt_api_date(row.get("posting_date")),
                "status": row.get("status"),
                "amount": _as_number(row.get("grand_total")),
                "currency": row.get("currency"),
                "remarks": row.get("remarks"),
                "item_name": row.get("expense_item_name"),
                "item_group": row.get("expense_item_group") or "Uncategorised",
            }
        )
    return out


def _invoice_filters(company: str, from_date: date, to_date: date):
    return [
        ["company", "=", company],
        ["docstatus", "<", 2],
        ["posting_date", ">=", from_date],
        ["posting_date", "<=", to_date],
    ]


def _sales_invoice_filters(company: str, from_date: date, to_date: date):
    """Same visibility rules as Purchase Invoice: company-scoped, exclude cancelled."""
    return [
        ["company", "=", company],
        ["docstatus", "<", 2],
        ["posting_date", ">=", from_date],
        ["posting_date", "<=", to_date],
    ]


def _get_recent_quotations(company: str, act_limit: int, tenant_id: str):
    """Recent quotations for financial dashboard activity (all lifecycle states).

    Includes draft, submitted, and cancelled quotations — dashboard activity is
    informational, not limited to open/active docs.

    Uses saas_platform-aligned visibility (own tenant + SYSTEM + unset legacy rows).
    ``tenant_db.get_all`` only matches an exact ``tenant_id``, which hides quotations
    created on central-site or before tenant backfill (often ``SYSTEM`` / NULL).
    """
    return frappe.get_all(
        "Quotation",
        filters=[
            ["company", "=", company],
        ],
        or_filters=[
            ["tenant_id", "=", tenant_id],
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
        limit=act_limit,
    )


def _add_months(d: date, months: int) -> date:
    """Shift date by calendar months (day clipped to last day of target month)."""
    month_idx = d.month - 1 + months
    year = d.year + month_idx // 12
    month = month_idx % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def _resolve_financial_period(preset: str, from_raw, to_raw):
    """Resolve (from_date, to_date, preset_label) from preset or explicit dates."""
    today = _get_frappe_today()
    p = (preset or "last_7_days").strip().lower()

    if p in ("custom",):
        fd = _safe_date(from_raw, lambda: None)
        td = _safe_date(to_raw, lambda: None)
        if not fd or not td:
            frappe.throw("custom preset requires from_date and to_date (YYYY-MM-DD)")
        if fd > td:
            fd, td = td, fd
        max_span = timedelta(days=732)
        if td - fd > max_span:
            frappe.throw("Custom date range cannot exceed 732 days")
        return fd, td, "custom"

    if p in ("last_7_days", "7d", "week"):
        to_d = today
        from_d = today - timedelta(days=6)
        return from_d, to_d, "last_7_days"

    if p in ("last_6_months", "6m", "six_months"):
        to_d = today
        from_d = _add_months(today, -6)
        return from_d, to_d, "last_6_months"

    frappe.throw(
        "preset must be one of: last_7_days, last_6_months, custom "
        f"(got {preset!r})"
    )


def _aggregate_by_posting_date(rows, amount_field: str):
    """Sum numeric amounts keyed by posting_date (ISO date string)."""
    buckets: dict[str, float] = {}
    for row in rows or []:
        pd = row.get("posting_date")
        if isinstance(pd, datetime):
            pd = pd.date()
        elif hasattr(pd, "year"):
            pass
        elif isinstance(pd, str):
            try:
                pd = datetime.fromisoformat(pd.replace("Z", "+00:00")).date()
            except Exception:
                continue
        else:
            continue
        key = pd.isoformat()
        buckets[key] = buckets.get(key, 0.0) + _as_number(row.get(amount_field))
    return buckets


def _daily_series(
    from_date: date,
    to_date: date,
    income_by_day: dict[str, float],
    expense_by_day: dict[str, float],
):
    """One row per calendar day in range (inclusive), sorted ascending."""
    out = []
    d = from_date
    while d <= to_date:
        key = d.isoformat()
        inc = income_by_day.get(key, 0.0)
        exp = expense_by_day.get(key, 0.0)
        out.append(
            {
                "date": key,
                "income": round(inc, 2),
                "expense": round(exp, 2),
                "net": round(inc - exp, 2),
            }
        )
        d += timedelta(days=1)
    return out


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _app_db():
    return get_app().tenant_db


def _resolve_company():
    """Resolve the current user's company.

    Checks in order:
    1. frappe.defaults (if available in context)
    2. DefaultValue table (user-level, defkey=company)
    3. User Permission (allow=Company) — set during signup provisioning
    4. System-level default (__default)
    """
    defaults = getattr(frappe, "defaults", None)
    if defaults:
        get_user_default = getattr(defaults, "get_user_default", None)
        if callable(get_user_default):
            for key in ("Company", "company"):
                user_company = get_user_default(key)
                if user_company:
                    return user_company

    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest":
        return None

    user_company = frappe.db.get_value(
        "DefaultValue",
        {"parent": user, "defkey": "company"},
        "defvalue",
    )
    if user_company:
        return user_company

    permitted = frappe.db.get_value(
        "User Permission",
        {"user": user, "allow": "Company"},
        "for_value",
    )
    if permitted:
        return permitted

    return frappe.db.get_value(
        "DefaultValue",
        {"parent": "__default", "defkey": "company"},
        "defvalue",
    ) or None


def _log_pi_currency_debug(pinv_name: str) -> None:
    """Log PI fields + ERPNext company currency when submit fails with FX-style errors (e.g. AUD→None)."""
    pi = frappe.get_doc("Purchase Invoice", pinv_name)
    gcc = None
    try:
        import erpnext

        gcc = erpnext.get_company_currency(pi.company) if pi.company else None
    except Exception as exc:
        gcc = f"<err {exc!r}>"
    co_row = None
    if pi.company:
        co_row = frappe.db.get_value(
            "Company",
            pi.company,
            ["default_currency", "default_payable_account"],
            as_dict=True,
        )
    logger.warning(
        "SUBMIT PI currency debug name=%r pi.company=%r pi.currency=%r pi.conversion_rate=%r "
        "pi.credit_to=%r pi.party_account_currency=%r erpnext.get_company_currency=%r tabCompany=%r",
        pinv_name,
        pi.company,
        pi.currency,
        pi.conversion_rate,
        getattr(pi, "credit_to", None),
        getattr(pi, "party_account_currency", None),
        gcc,
        co_row,
    )


@get_app().secure_route("/api/method/frappe.client.submit", methods=["POST"])
def frappe_client_submit(user):
    """Submit a document on this site.

    For **Purchase Invoice**, validates company ownership, tenant access, and draft state,
    sets the expense title without bumping ``modified`` (so ``check_if_latest`` matches the
    client’s last save), then calls ``doc.submit()`` for ERPNext’s full submission lifecycle.
    Returns ``{"success": true, "docstatus": 1, "name": "<id>"}``.

    For other doctypes, delegates to ``frappe.client.submit``.

    Body (Frappe standard):

    - ``{"doc": {"doctype": "Purchase Invoice", "name": "<id>"}}`` — ``doc`` may be a JSON string.
    - Shorthand for PI only: ``{"name": "<id>"}`` or ``{"invoice_name": "<id>"}``.
    """
    import json
    from flask import request

    name_raw = ""
    last_pi_submit = None
    try:
        payload = request.get_json(silent=True) or {}
        doc_arg = payload.get("doc")
        if isinstance(doc_arg, str):
            doc_arg = json.loads(doc_arg)
        if not isinstance(doc_arg, dict) or not doc_arg.get("doctype") or not doc_arg.get("name"):
            name_raw = (payload.get("name") or payload.get("invoice_name") or "").strip()
            if name_raw:
                doc_arg = {"doctype": "Purchase Invoice", "name": name_raw}
        if not isinstance(doc_arg, dict) or not doc_arg.get("doctype") or not doc_arg.get("name"):
            return _build_error(
                "Request must include `doc` with `doctype` and `name`, or `name` / `invoice_name` "
                "for Purchase Invoice.",
                400,
                "ValidationError",
            )

        doctype = doc_arg["doctype"]
        name = str(doc_arg["name"]).strip()
        name_raw = name

        if doctype == "Purchase Invoice":
            name, err = _validate_name(name)
            if err:
                return err

            company = _resolve_company()
            if not company:
                return _build_error(
                    "Company is required. No company found for the current user.",
                    400,
                    "ValidationError",
                )

            row = frappe.db.get_value(
                "Purchase Invoice",
                name,
                [
                    "docstatus",
                    "company",
                    "supplier",
                    "expense_item_name",
                    "expense_items_count",
                    "remarks",
                ],
                as_dict=True,
            )
            if not row:
                logger.info("SUBMIT: not found name=%r user=%s", name, user)
                return _build_error(
                    f"Purchase Invoice '{name}' not found",
                    404,
                    "DoesNotExistError",
                )

            inv_company = (row.get("company") or "").strip()
            if inv_company and inv_company != company:
                logger.warning(
                    "SUBMIT: permission denied name=%r user=%s company=%s", name, user, company
                )
                return _build_error(
                    "You do not have access to this expense",
                    403,
                    "PermissionError",
                )

            # ERPNext get_company_currency(doc.company) drives conversion_rate / exchange lookups.
            # Empty PI.company → None "to" currency (AUD→None) even when Company master is AUD.
            co = inv_company or company
            if not inv_company:
                frappe.db.set_value(
                    "Purchase Invoice",
                    name,
                    "company",
                    co,
                    update_modified=False,
                )

            docstatus = int(row.get("docstatus") or 0)
            if docstatus != 0:
                status_label = "Submitted" if docstatus == 1 else "Cancelled"
                return _build_error(
                    f"Only draft expenses (docstatus 0) can be submitted. "
                    f"This invoice is {status_label} (docstatus={docstatus}).",
                    400,
                    "ValidationError",
                )

            _app_db().get_doc("Purchase Invoice", name, verify_tenant=True)
            last_pi_submit = name

            # Round-off CC, payable account_currency, supplier party currency — avoids
            # submit-time validation errors on partially provisioned tenants.
            sup = (row.get("supplier") or "").strip() or None
            ensure_purchase_invoice_submit_prereqs(co, sup)

            expense_title = _expense_title(
                row.get("expense_item_name"),
                int(row.get("expense_items_count") or 0),
                row.get("remarks"),
            )
            # ERPNext / site schema may omit ``title`` on Purchase Invoice (no DB column).
            if expense_title and frappe.db.has_column("Purchase Invoice", "title"):
                try:
                    frappe.db.set_value(
                        "Purchase Invoice",
                        name,
                        "title",
                        expense_title,
                        update_modified=False,
                    )
                    frappe.db.commit()
                    frappe.clear_document_cache("Purchase Invoice", name)
                except Exception as exc:
                    logger.warning(
                        "SUBMIT: could not set Purchase Invoice title name=%r: %s",
                        name,
                        exc,
                    )
                    frappe.db.rollback()

            doc = frappe.get_doc("Purchase Invoice", name)
            doc.flags.ignore_permissions = True
            ensure_purchase_invoice_item_defaults(doc)
            ensure_purchase_invoice_submit_prereqs(co, sup, doc.get("posting_date"))
            # Flush + drop stale Account cache (get_cached_value used for party_account_currency).
            frappe.db.commit()
            clear_account_cache_for_company(co)

            # Defence-in-depth: ensure the PI itself has currency & conversion_rate
            # and clear the company-currency cache one final time so ERPNext's
            # get_company_currency() can never return None during GL entry creation.
            if not doc.get("company"):
                doc.company = co
            if not doc.get("currency"):
                doc.currency = _company_default_currency(co)
            if not doc.get("conversion_rate"):
                doc.conversion_rate = 1.0
            try:
                if hasattr(doc, "set_missing_values"):
                    doc.set_missing_values(for_validate=True)
            except Exception as exc:
                logger.info("SUBMIT: set_missing_values(for_validate=True) skipped: %s", exc)
            clear_company_currency_cache(co)
            # Force request-level cache used by erpnext.get_company_currency() (must match doc.company).
            cur_master = frappe.db.get_value("Company", co, "default_currency")
            if not (cur_master and str(cur_master).strip()):
                cur_master = _company_default_currency(co)
            if not getattr(frappe.flags, "company_currency", None):
                frappe.flags.company_currency = {}
            frappe.flags.company_currency[co] = str(cur_master).strip()

            doc.submit()
            frappe.db.commit()
            logger.info("SUBMIT: success name=%s docstatus=%s user=%s", name, doc.docstatus, user)
            # ERPNext sets doc.status to payment workflow (e.g. "Unpaid"); clients use docstatus.
            return {"success": True, "docstatus": int(doc.docstatus), "name": doc.name}

        _app_db().get_doc(doctype, name, verify_tenant=True)
        logger.info("SUBMIT (frappe.client.submit): %s %s user=%s", doctype, name, user)
        client_submit = frappe.get_attr("frappe.client.submit")
        return client_submit({"doctype": doctype, "name": name})

    except frappe.DoesNotExistError:
        return _build_error(
            f"Document '{name_raw}' not found" if name_raw else "Document not found",
            404,
            "DoesNotExistError",
        )

    except frappe.PermissionError:
        return _build_error("You do not have permission to submit this document", 403, "PermissionError")

    except frappe.ValidationError as e:
        if last_pi_submit and "exchange rate" in str(e).lower():
            try:
                _log_pi_currency_debug(last_pi_submit)
            except Exception:
                logger.exception("SUBMIT: PI currency debug failed for %r", last_pi_submit)
        logger.warning("SUBMIT: validation error name=%r user=%s: %s", name_raw, user, e)
        return _build_error(f"Invalid input: {e}", 400, "ValidationError")

    except Exception as e:
        logger.exception("SUBMIT: unexpected error name=%r user=%s", name_raw, user)
        return _build_error("An unexpected error occurred while submitting", 500, "ServerError")


def _fmt_api_date(val):
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _to_api_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _project_purchase_invoice_api(doc):
    """Slim JSON for mobile: same fields clients send on POST + id and a few total."""
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    items_out = []
    for row in d.get("items") or []:
        if not row:
            continue
        items_out.append(
            {
                "item_code": row.get("item_code"),
                "item_group": row.get("item_group"),
                "qty": _to_api_float(row.get("qty")),
                "rate": _to_api_float(row.get("rate")),
                "amount": _to_api_float(row.get("amount")),
            }
        )
    taxes_out = []
    for row in d.get("taxes") or []:
        if not row:
            continue
        taxes_out.append(
            {
                "account_head": row.get("account_head"),
                "description": row.get("description"),
                "rate": _to_api_float(row.get("rate")),
                "tax_amount": _to_api_float(row.get("tax_amount")),
            }
        )
    name = d.get("name")
    return {
        "id": name,
        "name": name,
        "supplier": d.get("supplier"),
        "posting_date": _fmt_api_date(d.get("posting_date")),
        "remarks": d.get("remarks"),
        "items": items_out,
        "taxes_and_charges": d.get("taxes_and_charges"),
        "taxes": taxes_out,
        "status": d.get("status"),
        "docstatus": d.get("docstatus"),
        "grand_total": _to_api_float(d.get("grand_total")),
        "currency": d.get("currency"),
        "receipt_image": getattr(doc, "receipt_image", None) or d.get("receipt_image"),
    }


def get_purchase_invoice(user, name):
    """GET /api/resource/Purchase Invoice/<name> — slim document (not full as_dict)."""
    import urllib.parse

    name_raw = urllib.parse.unquote(str(name or "")).strip()
    nm, err = _validate_name(name_raw)
    if err:
        return err

    try:
        doc = _app_db().get_doc("Purchase Invoice", nm)
        return _project_purchase_invoice_api(doc)
    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
    except frappe.DoesNotExistError:
        return {"error": "Purchase Invoice not found"}, 404


def create_purchase_invoice(user):
    """POST /api/resource/Purchase Invoice — create and return the same slim shape as GET."""
    from flask import request

    data = request.json
    if not data:
        return {"error": "Request body required"}, 400

    try:
        # Session auth is enforced by secure_route; Frappe desk roles are not required.
        doc = _app_db().insert_doc(
            "Purchase Invoice", data, ignore_permissions=True
        )
        out = _project_purchase_invoice_api(doc)
        out["success"] = True
        out["doctype"] = "Purchase Invoice"
        return out, 201
    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
    except frappe.ValidationError as e:
        return _build_error(str(e), 400, "ValidationError")
    except Exception as e:
        logger.exception("POST Purchase Invoice failed: %s", e)
        return _build_error(str(e), 400, "ValidationError")


def delete_purchase_invoice(user, name):
    """DELETE /api/resource/Purchase Invoice/<name>: cancel if submitted, then delete.

    Draft (0) is deleted normally. Submitted (1) uses ``doc.cancel()`` so GL is
    reversed, then ``delete_doc(..., force=True)`` because ERPNext keeps GL Entry
    links to the voucher and Frappe would otherwise raise ``LinkExistsError``.
    Cancelled (2), including retries after a partial failure, uses the same forced
    delete path.
    """
    import urllib.parse

    name_raw = urllib.parse.unquote(str(name or "")).strip()
    name, err = _validate_name(name_raw)
    if err:
        return err

    try:
        company = _resolve_company()
        if not company:
            return _build_error(
                "Company is required. No company found for the current user.",
                400, "ValidationError"
            )

        row = frappe.db.get_value(
            "Purchase Invoice",
            name,
            ["docstatus", "company"],
            as_dict=True,
        )
        if not row:
            logger.info("DELETE_PI: not found name=%r user=%s", name, user)
            return _build_error(
                f"Purchase Invoice '{name}' not found",
                404, "DoesNotExistError"
            )

        inv_company = (row.get("company") or "").strip()
        if inv_company and inv_company != company:
            logger.warning(
                "DELETE_PI: permission denied name=%r user=%s company=%s",
                name, user, company,
            )
            return _build_error(
                "You do not have access to this expense",
                403, "PermissionError"
            )

        docstatus = int(row.get("docstatus") or 0)
        # Cancelled PIs remain dynamically linked to GL Entry rows in ERPNext; link checks
        # block delete unless force=True (after a real cancel(), GL is reversed — safe to remove).
        delete_kwargs = {}
        if docstatus == 1:
            doc = _app_db().get_doc("Purchase Invoice", name)
            doc.flags.ignore_permissions = True
            doc.cancel()
            frappe.db.commit()
            logger.info(
                "DELETE_PI: cancelled via doc.cancel() name=%s user=%s",
                name,
                user,
            )
            delete_kwargs = {"force": True, "ignore_permissions": True}
        elif docstatus == 2:
            delete_kwargs = {"force": True, "ignore_permissions": True}
        elif docstatus != 0:
            return _build_error(
                f"Unexpected docstatus={docstatus} for Purchase Invoice {name}.",
                400, "ValidationError"
            )

        _app_db().delete_doc("Purchase Invoice", name, **delete_kwargs)
        logger.info("DELETE_PI: success name=%s user=%s", name, user)

        return {
            "success": True,
            "doctype": "Purchase Invoice",
            "message": "Purchase Invoice deleted",
        }

    except frappe.LinkExistsError as e:
        logger.warning("DELETE_PI: link exists name=%r user=%s: %s", name, user, e)
        return _build_error(str(e), 400, "LinkExistsError")
    except frappe.ValidationError as e:
        logger.warning("DELETE_PI: validation name=%r user=%s: %s", name, user, e)
        return _build_error(f"Invalid input data: {e}", 400, "ValidationError")
    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
    except frappe.DoesNotExistError:
        return {"error": "Purchase Invoice not found"}, 404


@get_app().secure_route('/api/method/expense_tracker.api.get_dashboard_summary', methods=['GET'])
def get_dashboard_summary(user, from_date=None, to_date=None):
    """Purchase Invoice aggregates for the tenant company.

    Without ``period``: legacy month-to-date (1st of month → today) via ``from_date`` /
    ``to_date`` defaults.

    With ``GET ... ?period=week|month|year``: preset windows, trend vs prior window,
    cashflow buckets for charts, ``pct`` / ``color`` on breakdown rows.

    Custom range: ``period=custom`` with ``from_date`` and ``to_date``, or both dates
    without ``period``. Returns the same enriched payload (cashflow, trend, dates).
    """
    from flask import request

    db = _app_db()
    company = _resolve_company()
    if not company:
        frappe.throw("Company is required")

    raw_period = request.args.get("period")
    if isinstance(raw_period, str):
        period_preset = raw_period.strip().lower()
    else:
        period_preset = ""

    from_raw = request.args.get("from_date") or from_date
    to_raw = request.args.get("to_date") or to_date

    if period_preset and period_preset not in ("week", "month", "year", "custom"):
        frappe.throw("period must be one of: week, month, year, custom")

    use_preset = period_preset in ("week", "month", "year")
    use_custom = period_preset == "custom" or (
        not use_preset and from_raw and to_raw
    )

    try:
        recent_limit = int(request.args.get("recent_limit") or 10)
    except (TypeError, ValueError):
        recent_limit = 10
    recent_limit = max(1, min(recent_limit, 50))

    if use_preset:
        today = _get_frappe_today()
        from_date, to_date, prev_from, prev_to, period_display, compare_label = (
            _resolve_dashboard_period(period_preset, today)
        )
    elif use_custom:
        from_date, to_date, prev_from, prev_to, period_display, compare_label = (
            _resolve_dashboard_custom_bounds(from_raw, to_raw)
        )
        period_preset = "custom"
    else:
        prev_from = prev_to = None
        compare_label = None
        period_display = None

        from_date = _safe_date(from_date, _default_from_date)
        to_date = _safe_date(to_date, _get_frappe_today)

        if not from_date or not to_date:
            from_date = _default_from_date()
            to_date = _get_frappe_today()

        if from_date > to_date:
            from_date, to_date = to_date, from_date

    filters = _invoice_filters(company, from_date, to_date)
    inv_fields = [
        "name",
        "supplier",
        "posting_date",
        "status",
        "grand_total",
        "total_taxes_and_charges",
        "currency",
        "remarks",
        "expense_item_name",
        "expense_item_group",
    ]

    invoices = db.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=inv_fields,
    )

    recent_rows = _fetch_recent_purchase_invoices(company, recent_limit, inv_fields)

    breakdown = {}
    if invoices:
        invoice_names = [row.get("name") for row in invoices]
        invoice_items = db.get_all(
            "Purchase Invoice Item",
            filters=[
                ["parenttype", "=", "Purchase Invoice"],
                ["parent", "in", invoice_names],
            ],
            # Let DB perform category aggregation; Python keeps only normalization.
            fields=["item_group", {"SUM": "amount", "as": "total"}],
            group_by="item_group",
        )
        for row in invoice_items or []:
            item_group = row.get("item_group") or "Uncategorised"
            total = row.get("total")
            if total is None:
                # Backward-compat for mocks/older adapters that may still return raw rows.
                total = row.get("amount")
            breakdown[item_group] = breakdown.get(item_group, 0.0) + _as_number(total)
        if not breakdown:
            # Fallback for environments where child table aggregation is restricted/empty:
            # aggregate by the controller-populated primary item group on Purchase Invoice.
            for row in invoices or []:
                item_group = row.get("expense_item_group") or "Uncategorised"
                breakdown[item_group] = breakdown.get(item_group, 0.0) + _as_number(
                    row.get("grand_total")
                )

    total_spend = sum(_as_number(row.get("grand_total")) for row in invoices or [])
    gst_total = sum(_as_number(row.get("total_taxes_and_charges")) for row in invoices or [])

    currency = db.get_value("Company", company, "default_currency") or "AUD"

    breakdown_rows = [
        {"item_group": item_group, "total": total}
        for item_group, total in sorted(
            breakdown.items(), key=lambda item: item[1], reverse=True
        )
    ]

    period_label_out = (
        period_display if use_preset else _period_label(from_date, to_date)
    )

    result = {
        "total_spend": total_spend,
        "gst_total": gst_total,
        "currency": currency,
        "period": period_label_out,
        "breakdown": breakdown_rows,
        "recent_expenses": _recent_expenses_from_rows(recent_rows, recent_limit),
    }

    if use_preset or use_custom:
        prev_total = 0.0
        if prev_from and prev_to:
            prev_filters = _invoice_filters(company, prev_from, prev_to)
            prev_rows = db.get_all(
                "Purchase Invoice",
                filters=prev_filters,
                fields=["grand_total"],
            )
            prev_total = sum(_as_number(r.get("grand_total")) for r in prev_rows or [])

        trend_pct = _trend_pct_vs_previous(total_spend, prev_total)
        top_row = breakdown_rows[0] if breakdown_rows else None

        enriched_breakdown = []
        for idx, row in enumerate(breakdown_rows):
            tot = _as_number(row.get("total"))
            pct = round((tot / total_spend * 100.0), 2) if total_spend > 0 else 0.0
            color = _CATEGORY_COLOR_PALETTE[idx % len(_CATEGORY_COLOR_PALETTE)]
            enriched_breakdown.append({**row, "pct": pct, "color": color})

        if period_preset == "week":
            week_start, _ = _dashboard_week_bounds(from_date)
            cashflow = _cashflow_week_series(invoices, week_start)
        elif period_preset == "month":
            cashflow = _cashflow_month_week_segments(invoices, from_date, to_date)
        elif period_preset == "year":
            cashflow = _cashflow_year_months(invoices, from_date.year, to_date)
        else:
            cashflow = _cashflow_custom_range(invoices, from_date, to_date)

        cf_amounts = [b["amount"] for b in cashflow]

        result.update(
            {
                "preset": period_preset,
                "from_date": from_date.isoformat(),
                "to_date": to_date.isoformat(),
                "compare_period_label": compare_label,
                "trend_pct": trend_pct,
                "previous_period_total": round(prev_total, 2),
                "top_category": (
                    {"item_group": top_row["item_group"], "total": top_row["total"]}
                    if top_row
                    else None
                ),
                "cashflow": cashflow,
                "cashflow_stats": _cashflow_stats_from_amounts(cf_amounts),
                "breakdown": enriched_breakdown,
                "breakdown_top4": _breakdown_top_categories(
                    enriched_breakdown, total_spend, _BREAKDOWN_TOP_N
                ),
            }
        )

    return result


@get_app().secure_route(
    "/api/method/expense_tracker.api.get_financial_dashboard",
    methods=["GET"],
)
def get_financial_dashboard(user):
    """Date-wise income (Sales Invoice) vs expense (Purchase Invoice), presets, and recent activity.

    Uses the same tenant DB access patterns as the Resource API (``get_all`` on SI / PI / Quotation).

    ``recent_activity`` merges the latest Sales Invoices, Purchase Invoices, and Quotations
    for the company (by ``modified``). Quotations include all states (draft, submitted,
    cancelled, open, lost, etc.) and do not affect income/expense totals or ``daily``.

    Query parameters:

    - ``preset``: ``last_7_days`` (default), ``last_6_months``, or ``custom``
    - ``from_date`` / ``to_date``: required when ``preset=custom`` (YYYY-MM-DD)
    - ``activity_limit``: max rows for ``recent_activity`` (default 20, max 50)
    """
    from flask import request
    import urllib.parse

    db = _app_db()
    company = _resolve_company()
    if not company:
        frappe.throw("Company is required")

    preset_q = request.args.get("preset") or request.args.get("period")
    from_raw = request.args.get("from_date")
    to_raw = request.args.get("to_date")
    from_date, to_date, preset_used = _resolve_financial_period(
        preset_q or "last_7_days", from_raw, to_raw
    )

    try:
        act_limit = int(request.args.get("activity_limit") or 20)
    except (TypeError, ValueError):
        act_limit = 20
    act_limit = max(1, min(act_limit, 50))

    tenant_id = _resolve_tenant_id(db)

    si_filters = _sales_invoice_filters(company, from_date, to_date)
    pi_filters = _invoice_filters(company, from_date, to_date)

    si_rows = db.get_all(
        "Sales Invoice",
        filters=si_filters,
        fields=["name", "posting_date", "grand_total"],
    )
    pi_rows = db.get_all(
        "Purchase Invoice",
        filters=pi_filters,
        fields=["name", "posting_date", "grand_total"],
    )

    income_by_day = _aggregate_by_posting_date(si_rows, "grand_total")
    expense_by_day = _aggregate_by_posting_date(pi_rows, "grand_total")

    daily = _daily_series(from_date, to_date, income_by_day, expense_by_day)
    total_income = sum(_as_number(r.get("grand_total")) for r in si_rows or [])
    total_expense = sum(_as_number(r.get("grand_total")) for r in pi_rows or [])

    currency = db.get_value("Company", company, "default_currency") or "AUD"

    # Recent activity: latest modified SI + PI (no period window; tenant-aware visibility).
    si_recent = _fetch_recent_sales_invoices(company, act_limit)
    pi_recent_fields = [
        "name",
        "supplier",
        "posting_date",
        "grand_total",
        "modified",
        "status",
    ]
    pi_recent = _fetch_recent_purchase_invoices(company, act_limit, pi_recent_fields)
    q_recent = _get_recent_quotations(company, act_limit, tenant_id)

    merged = []
    for row in si_recent or []:
        name = row.get("name")
        merged.append(
            {
                "doctype": "Sales Invoice",
                "name": name,
                "party": row.get("customer"),
                "posting_date": row.get("posting_date"),
                "amount": _as_number(row.get("grand_total")),
                "modified": row.get("modified"),
                "status": row.get("status"),
                "resource_path": "/api/resource/"
                + urllib.parse.quote("Sales Invoice")
                + "/"
                + urllib.parse.quote(name or "", safe=""),
            }
        )
    for row in pi_recent or []:
        name = row.get("name")
        merged.append(
            {
                "doctype": "Purchase Invoice",
                "name": name,
                "party": row.get("supplier"),
                "posting_date": row.get("posting_date"),
                "amount": _as_number(row.get("grand_total")),
                "modified": row.get("modified"),
                "status": row.get("status"),
                "resource_path": "/api/resource/"
                + urllib.parse.quote("Purchase Invoice")
                + "/"
                + urllib.parse.quote(name or "", safe=""),
            }
        )
    for row in q_recent or []:
        name = row.get("name")
        docstatus = int(row.get("docstatus") or 0)
        merged.append(
            {
                "doctype": "Quotation",
                "name": name,
                "party": row.get("customer_name") or row.get("party_name"),
                "posting_date": row.get("transaction_date"),
                "amount": _as_number(row.get("grand_total")),
                "modified": row.get("modified"),
                "status": row.get("status"),
                "docstatus": docstatus,
                "resource_path": "/api/resource/"
                + urllib.parse.quote("Quotation")
                + "/"
                + urllib.parse.quote(name or "", safe=""),
            }
        )

    def _modified_sort_key(row):
        m = row.get("modified")
        if isinstance(m, datetime):
            return m
        if isinstance(m, date) and not isinstance(m, datetime):
            return datetime.combine(m, datetime.min.time())
        if isinstance(m, str):
            try:
                return datetime.fromisoformat(m.replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.min

    merged.sort(key=_modified_sort_key, reverse=True)
    recent_activity = merged[:act_limit]

    return {
        "company": company,
        "currency": currency,
        "preset": preset_used,
        "period_label": _period_label(from_date, to_date),
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "daily": daily,
        "totals": {
            "income": round(total_income, 2),
            "expense": round(total_expense, 2),
            "net": round(total_income - total_expense, 2),
        },
        "recent_activity": recent_activity,
        "resource_api": {
            "sales_invoice_list": "/api/resource/"
            + urllib.parse.quote("Sales Invoice"),
            "purchase_invoice_list": "/api/resource/"
            + urllib.parse.quote("Purchase Invoice"),
            "quotation_list": "/api/resource/" + urllib.parse.quote("Quotation"),
        },
    }
