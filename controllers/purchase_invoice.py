from __future__ import annotations

from typing import Any, Dict, List

from frappe_microservice import get_app
from frappe_microservice.controller import DocumentController


DEFAULT_GST_TEMPLATE = "AU GST 10%"
GST_MARKER = "gst"


def _app_db():
    return get_app().db


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


def _item_has_gst(db, item_code: str) -> bool:
    tax_rows = db.get_all(
        "Item Tax",
        filters={"parent": item_code},
        fields=["tax_type", "tax_type_name", "tax_rate"],
    )
    for row in tax_rows or []:
        tax_type = str(_value(row, "tax_type", "")).lower()
        tax_type_name = str(_value(row, "tax_type_name", "")).lower()
        if GST_MARKER in tax_type or GST_MARKER in tax_type_name:
            return True
        rate = _value(row, "tax_rate", 0)
        try:
            if float(rate) == 10:
                # GST at 10% is the required Australian tax rate.
                return True
        except (TypeError, ValueError):
            continue
    return False


def _get_default_expense_account(db, item_code: str, company: str):
    return db.get_value(
        "Item Default",
        {"parent": item_code, "company": company},
        "default_expense_account",
    )


def _get_default_cost_center(db, company: str):
    for field in ("default_cost_center", "cost_center", "default_cost_center_name"):
        value = db.get_value("Company", company, field)
        if value:
            return value
    return None


def _gst_template_rows(db, company: str) -> List[Dict[str, Any]]:
    template_rows = db.get_all(
        "Purchase Taxes and Charges",
        filters={"parent": DEFAULT_GST_TEMPLATE},
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
        template_rows = db.get_all(
            "Purchase Taxes and Charges Template Detail",
            filters={"parent": DEFAULT_GST_TEMPLATE},
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

    cost_center = _get_default_cost_center(db, company)
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


def _default_supplier_group(db):
    supplier_group = None
    for filters in (
        None,
        "Buying Settings",
        {"name": "Buying Settings"},
        {},
    ):
        try:
            supplier_group = db.get_value("Buying Settings", filters, "default_supplier_group")
        except Exception:
            supplier_group = None
        if supplier_group:
            return supplier_group

    if supplier_group:
        return supplier_group

    groups = db.get_all("Supplier Group", fields=["name"], limit=1, order_by="name asc")
    return _value(groups[0], "name") if groups else None


def _resolve_supplier_name(db, supplier_value):
    supplier_value = str(supplier_value).strip() if supplier_value else None
    if not supplier_value:
        return None

    existing = db.get_value("Supplier", supplier_value, "name")
    if existing:
        return existing

    named = db.get_value("Supplier", {"supplier_name": supplier_value}, "name")
    if named:
        return named

    supplier_group = _default_supplier_group(db)
    return _create_supplier(db, supplier_value, supplier_group)


def _normalize_name(value: str, fallback: str = "Supplier", max_length: int = 100) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        normalized = fallback
    return normalized[:max_length]


def _create_supplier(db, supplier_name: str, supplier_group: str | None):
    if not supplier_group:
        supplier_group = _normalize_name("General", fallback="General")

    base_name = _normalize_name(supplier_name, fallback="Supplier", max_length=95)
    if not base_name:
        base_name = "Supplier"

    name = base_name

    for i in range(1, 20):
        existing = db.get_value("Supplier", name, "name")
        if not existing:
            break
        name = _normalize_name(f"{base_name}-{i}", fallback="Supplier", max_length=95)

    payload = {
        "name": name,
        "supplier_name": supplier_name,
        "supplier_group": supplier_group,
    }
    insert = getattr(db, "insert", None)
    if not callable(insert):
        insert = getattr(db, "create", None)
    if callable(insert):
        created = insert(payload)
        return _value(created, "name", name)

    raise RuntimeError("Tenant DB adapter does not support document insert/create")


class PurchaseInvoice(DocumentController):
    """
    Auto-populate accounting values for Purchase Invoice records created by the mobile app.
    """

    def before_save(self):
        db = _app_db()
        company = _value(self, "company", None)
        if not company:
            return

        supplier = _resolve_supplier_name(db, _value(self, "supplier", None))
        if supplier:
            _set_value(self, "supplier", supplier)

        items = _value(self, "items", []) or []
        has_gst_item = False
        cost_center = _get_default_cost_center(db, company)

        for item in items:
            item_code = _value(item, "item_code")
            if not item_code:
                continue

            expense_account = _get_default_expense_account(db, item_code, company)
            if expense_account:
                _set_value(item, "expense_account", expense_account)
            if cost_center:
                _set_value(item, "cost_center", cost_center)

            if not has_gst_item and _item_has_gst(db, item_code):
                has_gst_item = True

        existing_taxes = [
            _serialise(tax) for tax in _value(self, "taxes", []) or []
        ]
        manual_taxes = [tax for tax in existing_taxes if not _is_gst_row(tax)]

        if has_gst_item:
            self.taxes = manual_taxes + _gst_template_rows(db, company)
        else:
            self.taxes = manual_taxes
