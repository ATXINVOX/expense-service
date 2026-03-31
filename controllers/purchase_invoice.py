from __future__ import annotations

from typing import Any, Dict, List

import frappe
from frappe_microservice import get_app
from frappe_microservice.controller import DocumentController


def _app_db():
    return get_app().db


DEFAULT_GST_TEMPLATE = "AU GST 10%"
GST_MARKER = "gst"


def _resolve_company_from_user():
    """Resolve the user's default company from Frappe session defaults.

    Checks in order:
    1. User-level default (parent = session user email, defkey = 'company')
    2. User Permission record (allow = 'Company') for the current user
    3. System-level default (parent = '__default', defkey = 'company')
    """
    user = getattr(getattr(frappe, 'session', None), 'user', None)

    if user and user != 'Guest':
        user_company = frappe.db.get_value(
            "DefaultValue",
            {"parent": user, "defkey": "company"},
            "defvalue",
        )
        if user_company:
            return user_company

        # Check User Permission for 'Company' — set during onboarding.
        permitted = frappe.db.get_value(
            "User Permission",
            {"user": user, "allow": "Company"},
            "for_value",
        )
        if permitted:
            return permitted

    # Fall back to the global Frappe system default.
    return frappe.db.get_value(
        "DefaultValue",
        {"parent": "__default", "defkey": "company"},
        "defvalue",
    ) or None


def _value(row: Any, field: str, default=None):
    if isinstance(row, dict):
        return row.get(field, default)
    return getattr(row, field, default)


def _set_value(row: Any, field: str, value) -> None:
    if isinstance(row, dict):
        row[field] = value
        return
    setattr(row, field, value)


def _serialise(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        key: _value(row, key)
        for key in row.__dict__
        if not key.startswith("_")
    }


def _is_gst_row(tax_row: Dict[str, Any]) -> bool:
    desc = str(_value(tax_row, "description", "")).lower()
    charge = str(_value(tax_row, "account_head", "")).lower()
    return GST_MARKER in desc or GST_MARKER in charge


def _get_default_expense_account(item_code: str, company: str):
    return frappe.db.get_value(
        "Item Default",
        {"parent": item_code, "company": company},
        "default_expense_account",
    )


def _get_default_cost_center(company: str):
    for field in ("default_cost_center", "cost_center", "default_cost_center_name"):
        value = frappe.db.get_value("Company", company, field)
        if value:
            return value
    return None


def _find_gst_template():
    """Return the actual GST purchase tax template name for this ERPNext instance."""
    for pattern in ("%Non Capital%GST%", "%Capital%GST%", "%GST%"):
        rows = frappe.get_all(
            "Purchase Taxes and Charges Template",
            filters={"name": ("like", pattern)},
            fields=["name"],
            limit=1,
        )
        if rows:
            name = _value(rows[0], "name", None)
            if name and "import" not in str(name).lower():
                return name
    return None


def _gst_template_rows(company: str, template_name: str = DEFAULT_GST_TEMPLATE) -> List[Dict[str, Any]]:
    template_rows = frappe.get_all(
        "Purchase Taxes and Charges",
        filters={"parent": template_name},
        fields=[
            "charge_type",
            "account_head",
            "description",
            "rate",
            "cost_center",
            "included_in_print_rate",
            "add_deduct_tax",
        ],
        order_by="idx asc",
    )
    if not template_rows:
        template_rows = frappe.get_all(
            "Purchase Taxes and Charges Template Detail",
            filters={"parent": template_name},
            fields=[
                "charge_type",
                "account_head",
                "description",
                "rate",
                "cost_center",
                "included_in_print_rate",
                "add_deduct_tax",
            ],
            order_by="idx asc",
        )
    if not template_rows:
        return []

    cost_center = _get_default_cost_center(company)
    rows = []
    for row in template_rows or []:
        rows.append(
            {
                "charge_type": _value(row, "charge_type", "On Net Total"),
                "account_head": _value(row, "account_head"),
                "description": _value(row, "description", DEFAULT_GST_TEMPLATE),
                "rate": _value(row, "rate"),
                "cost_center": _value(row, "cost_center", cost_center),
                "included_in_print_rate": int(bool(_value(row, "included_in_print_rate", 0))),
                "add_deduct_tax": _value(row, "add_deduct_tax", "Add"),
            }
        )
    return rows


def _default_supplier_group():
    supplier_group = None
    for filters in (
        None,
        "Buying Settings",
        {"name": "Buying Settings"},
        {},
    ):
        try:
            supplier_group = frappe.db.get_value("Buying Settings", filters, "default_supplier_group")
        except Exception:
            supplier_group = None
        if supplier_group:
            return supplier_group

    if supplier_group:
        return supplier_group

    groups = frappe.get_all("Supplier Group", fields=["name"], limit=1, order_by="name asc")
    return _value(groups[0], "name") if groups else None


def _resolve_supplier_name(supplier_value):
    supplier_value = str(supplier_value).strip() if supplier_value else None
    if not supplier_value:
        return None

    existing = frappe.db.get_value("Supplier", supplier_value, "name")
    if existing:
        return existing

    named = frappe.db.get_value("Supplier", {"supplier_name": supplier_value}, "name")
    if named:
        return named

    supplier_group = _default_supplier_group()
    return _create_supplier(supplier_value, supplier_group)


def _normalize_name(value: str, fallback: str = "Supplier", max_length: int = 100) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        normalized = fallback
    return normalized[:max_length]


def _create_supplier(supplier_name: str, supplier_group: str | None):
    if not supplier_group:
        supplier_group = _normalize_name("General", fallback="General")

    base_name = _normalize_name(supplier_name, fallback="Supplier", max_length=95)
    if not base_name:
        base_name = "Supplier"

    name = base_name

    for i in range(1, 20):
        existing = frappe.db.get_value("Supplier", name, "name")
        if not existing:
            break
        name = _normalize_name(f"{base_name}-{i}", fallback="Supplier", max_length=95)

    doc = _app_db().insert_doc("Supplier", {
        "name": name,
        "supplier_name": supplier_name,
        "supplier_group": supplier_group,
    }, ignore_permissions=True)
    return doc.name


def _resolve_item_code(item_name: str, item_group: str) -> str:
    """Return existing item code or auto-create an Item under item_group."""
    item_name = str(item_name).strip() if item_name else None
    if not item_name:
        return item_name

    existing = frappe.db.get_value("Item", item_name, "name")
    if existing:
        return existing

    doc = _app_db().insert_doc("Item", {
        "name": item_name,
        "item_name": item_name,
        "item_group": item_group or "All Item Groups",
        "is_purchase_item": 1,
        "is_sales_item": 0,
        "stock_uom": "Nos",
    }, ignore_permissions=True)
    return doc.name


class PurchaseInvoice(DocumentController):
    """
    Auto-populate accounting values for Purchase Invoice records created by the mobile app.
    """

    def before_validate(self):
        # Resolve company from session user defaults; fall back to payload value.
        # This runs before ERPNext's own validate() so link fields are fixed in time.
        company = _resolve_company_from_user() or _value(self, "company", None)
        if not company:
            return
        _set_value(self, "company", company)

        # Auto-create supplier so ERPNext's link validation passes.
        supplier = _resolve_supplier_name(_value(self, "supplier", None))
        if supplier:
            _set_value(self, "supplier", supplier)

        cost_center = _get_default_cost_center(company)
        items = _value(self, "items", []) or []

        for item in items:
            item_code = _value(item, "item_code")
            item_group = _value(item, "item_group", None) or "All Item Groups"
            if not item_code:
                continue

            # Auto-create Item so ERPNext's link validation passes.
            resolved_code = _resolve_item_code(item_code, item_group)
            _set_value(item, "item_code", resolved_code)

            expense_account = _get_default_expense_account(resolved_code, company)
            if expense_account:
                _set_value(item, "expense_account", expense_account)
            if cost_center:
                _set_value(item, "cost_center", cost_center)

        # Resolve the real GST template that exists in this ERPNext instance.
        # Mobile sends taxes_and_charges as non-empty string to signal GST intent.
        wants_gst = bool(_value(self, "taxes_and_charges", None))
        existing_taxes = [
            _serialise(tax) for tax in _value(self, "taxes", []) or []
        ]
        manual_taxes = [tax for tax in existing_taxes if not _is_gst_row(tax)]

        if wants_gst:
            gst_template = _find_gst_template()
            if gst_template:
                _set_value(self, "taxes_and_charges", gst_template)
                self.taxes = manual_taxes + _gst_template_rows(company, gst_template)
            else:
                _set_value(self, "taxes_and_charges", "")
                self.taxes = manual_taxes
        else:
            _set_value(self, "taxes_and_charges", "")
            self.taxes = manual_taxes
