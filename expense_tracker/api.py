from __future__ import annotations

from datetime import date, datetime
import frappe
from frappe_microservice import get_app


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


def _default_from_date():
    today = date.today()
    return today.replace(day=1)


def _period_label(from_date: date, to_date: date) -> str:
    if from_date.month == to_date.month and from_date.year == to_date.year:
        return from_date.strftime("%B %Y")
    return f"{from_date.strftime('%B %Y')} - {to_date.strftime('%B %Y')}"


def _invoice_filters(company: str, from_date: date, to_date: date):
    return [
        ["company", "=", company],
        ["docstatus", "<", 2],
        ["posting_date", ">=", from_date],
        ["posting_date", "<=", to_date],
    ]


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


@get_app().secure_route('/api/method/expense_tracker.api.get_dashboard_summary', methods=['GET'])
def get_dashboard_summary(user, from_date=None, to_date=None):
    db = _app_db()
    company = _resolve_company()
    if not company:
        frappe.throw("Company is required")

    from_date = _safe_date(from_date, _default_from_date)
    to_date = _safe_date(to_date, date.today)

    if not from_date or not to_date:
        from_date = _default_from_date()
        to_date = date.today()

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    filters = _invoice_filters(company, from_date, to_date)
    invoices = db.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=["name", "grand_total", "total_taxes_and_charges"],
    )

    breakdown = {}
    if invoices:
        invoice_names = [row.get("name") for row in invoices]
        invoice_items = db.get_all(
            "Purchase Invoice Item",
            filters=[["parent", "in", invoice_names]],
            fields=["item_group", "amount"],
        )
        for row in invoice_items or []:
            item_group = row.get("item_group") or "Uncategorised"
            breakdown[item_group] = breakdown.get(item_group, 0.0) + _as_number(row.get("amount"))

    total_spend = sum(_as_number(row.get("grand_total")) for row in invoices or [])
    gst_total = sum(_as_number(row.get("total_taxes_and_charges")) for row in invoices or [])

    currency = db.get_value("Company", company, "default_currency") or "AUD"

    return {
        "total_spend": total_spend,
        "gst_total": gst_total,
        "currency": currency,
        "period": _period_label(from_date, to_date),
        "breakdown": [
            {"item_group": item_group, "total": total}
            for item_group, total in sorted(
                breakdown.items(), key=lambda item: item[1], reverse=True
            )
        ],
    }
