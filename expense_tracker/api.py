from __future__ import annotations

import logging
import re
from datetime import date, datetime
import frappe
from frappe_microservice import get_app

from controllers.purchase_invoice import _expense_title

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


@get_app().secure_route('/api/method/expense_tracker.api.get_expenses', methods=['GET'])
def get_expenses(user, from_date=None, to_date=None, limit=None, offset=None):
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

    try:
        limit = int(limit) if limit else 20
    except (ValueError, TypeError):
        limit = 20
    try:
        offset = int(offset) if offset else 0
    except (ValueError, TypeError):
        offset = 0

    filters = _invoice_filters(company, from_date, to_date)
    invoices = db.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=[
            "name", "company", "supplier", "posting_date",
            "grand_total", "total_taxes_and_charges",
            "remarks", "docstatus", "status",
        ],
        limit_start=offset,
        limit_page_length=limit,
        order_by="posting_date desc",
    )

    if invoices:
        invoice_names = [row.get("name") for row in invoices]
        items = db.get_all(
            "Purchase Invoice Item",
            filters=[["parent", "in", invoice_names]],
            fields=[
                "parent", "item_code", "item_name", "item_group",
                "qty", "rate", "amount", "expense_account",
            ],
            order_by="idx asc",
        )

        items_by_parent = {}
        for item in items or []:
            parent = item.get("parent")
            items_by_parent.setdefault(parent, []).append(item)

        for inv in invoices:
            inv["items"] = items_by_parent.get(inv.get("name"), [])
    else:
        invoices = []

    return {
        "company": company,
        "count": len(invoices),
        "data": invoices,
    }


@get_app().secure_route(
    "/api/method/expense_tracker.api.submit_purchase_invoice", methods=["POST"]
)
def submit_purchase_invoice(user):
    """Confirm a draft expense: set docstatus 1 and status Submitted (direct DB update)."""
    from flask import request

    try:
        payload = request.get_json(silent=True) or {}

        # 1. Validate name
        name_raw = (payload.get("name") or payload.get("invoice_name") or "").strip()
        name, err = _validate_name(name_raw)
        if err:
            return err

        # 2. Resolve company
        company = _resolve_company()
        if not company:
            return _build_error(
                "Company is required. No company found for the current user.",
                400, "ValidationError"
            )

        # 3. Fetch invoice
        row = frappe.db.get_value(
            "Purchase Invoice",
            name,
            [
                "docstatus",
                "company",
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
                404, "DoesNotExistError"
            )

        # 4. Company ownership check
        inv_company = (row.get("company") or "").strip()
        if inv_company and inv_company != company:
            logger.warning("SUBMIT: permission denied name=%r user=%s company=%s", name, user, company)
            return _build_error(
                "You do not have access to this expense",
                403, "PermissionError"
            )

        # 5. Docstatus check — only drafts (0) can be submitted
        docstatus = int(row.get("docstatus") or 0)
        if docstatus != 0:
            status_label = "Submitted" if docstatus == 1 else "Cancelled"
            return _build_error(
                f"Only draft expenses (docstatus 0) can be submitted. "
                f"This invoice is {status_label} (docstatus={docstatus}).",
                400, "ValidationError"
            )

        # 6. Submit
        expense_title = _expense_title(
            row.get("expense_item_name"),
            int(row.get("expense_items_count") or 0),
            row.get("remarks"),
        )
        updates: dict = {"docstatus": 1, "status": "Submitted"}
        if expense_title:
            updates["title"] = expense_title

        frappe.db.set_value("Purchase Invoice", name, updates)
        frappe.db.commit()

        saved_status = frappe.db.get_value("Purchase Invoice", name, "status")
        if saved_status != "Submitted":
            frappe.db.set_value("Purchase Invoice", name, "status", "Submitted")
            frappe.db.commit()

        logger.info("SUBMIT: success name=%s user=%s", name, user)

        return {
            "success": True,
            "name": name,
            "docstatus": 1,
            "status": "Submitted",
        }

    except frappe.DoesNotExistError:
        return _build_error(f"Purchase Invoice '{name}' not found", 404, "DoesNotExistError")

    except frappe.PermissionError:
        return _build_error("You do not have permission to submit this invoice", 403, "PermissionError")

    except frappe.ValidationError as e:
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
    """Slim JSON for mobile: same fields clients send on POST + id and a few totals."""
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
    name = d.get("name")
    return {
        "id": name,
        "name": name,
        "supplier": d.get("supplier"),
        "posting_date": _fmt_api_date(d.get("posting_date")),
        "remarks": d.get("remarks"),
        "items": items_out,
        "status": d.get("status"),
        "docstatus": d.get("docstatus"),
        "grand_total": _to_api_float(d.get("grand_total")),
        "currency": d.get("currency"),
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
        doc = _app_db().insert_doc("Purchase Invoice", data)
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

    Draft (0) and cancelled (2) invoices are deleted directly. Submitted (1) are
    moved to cancelled via DB update first, then removed.
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
        if docstatus == 1:
            frappe.db.set_value(
                "Purchase Invoice",
                name,
                {"docstatus": 2, "status": "Cancelled"},
            )
            frappe.db.commit()
            logger.info("DELETE_PI: cancelled before delete name=%s user=%s", name, user)
        elif docstatus not in (0, 2):
            return _build_error(
                f"Unexpected docstatus={docstatus} for Purchase Invoice {name}.",
                400, "ValidationError"
            )

        get_app().tenant_db.delete_doc("Purchase Invoice", name)
        logger.info("DELETE_PI: success name=%s user=%s", name, user)

        return {
            "success": True,
            "doctype": "Purchase Invoice",
            "message": "Purchase Invoice deleted",
        }

    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
    except frappe.DoesNotExistError:
        return {"error": "Purchase Invoice not found"}, 404


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
