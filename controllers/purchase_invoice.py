from __future__ import annotations

import logging
from typing import Any, Dict, List

import frappe
from frappe_microservice import get_app
from frappe_microservice.controller import DocumentController

logger = logging.getLogger(__name__)


def _app_db():
    return get_app().tenant_db


DEFAULT_GST_TEMPLATE = "AU GST 10%"
GST_MARKER = "gst"

EXPENSE_CUSTOM_COLUMNS = [
    ("expense_item_name", "varchar(140)"),
    ("expense_item_group", "varchar(140)"),
    ("expense_items_count", "int(11) DEFAULT 0"),
]


def _ensure_expense_custom_columns():
    """Add custom columns to tabPurchase Invoice if they don't exist yet.

    Runs once at import time. The columns are also created by the Frappe
    fixture system on the central-site bench, but this guarantees they
    exist even if the microservice starts before bench migrate runs.
    """
    for col_name, col_type in EXPENSE_CUSTOM_COLUMNS:
        try:
            frappe.db.sql(
                f"ALTER TABLE `tabPurchase Invoice` ADD COLUMN `{col_name}` {col_type}",
            )
            frappe.db.commit()
            logger.info("ensure_expense_custom_columns: added column %s to tabPurchase Invoice", col_name)
        except Exception:
            pass

    for col_name, col_type in EXPENSE_CUSTOM_COLUMNS:
        try:
            fieldtype = "Link" if col_name == "expense_item_group" else ("Int" if "count" in col_name else "Data")
            options = "Item Group" if col_name == "expense_item_group" else ""
            frappe.db.sql("""
                INSERT IGNORE INTO `tabCustom Field`
                (name, dt, fieldname, fieldtype, label, module, read_only, options,
                 creation, modified, modified_by, owner, docstatus, idx)
                VALUES (%s, 'Purchase Invoice', %s, %s, %s, 'Saas Platform', 1, %s,
                        NOW(), NOW(), 'Administrator', 'Administrator', 0, 0)
            """, (
                f"Purchase Invoice-{col_name}",
                col_name,
                fieldtype,
                col_name.replace("expense_", "").replace("_", " ").title(),
                options,
            ))
        except Exception:
            pass
    try:
        frappe.db.commit()
    except Exception:
        pass


try:
    _ensure_expense_custom_columns()
except Exception as e:
    logger.warning("ensure_expense_custom_columns: skipped (%s)", e)


def _create_system_doc(doctype: str, values: Dict[str, Any], **insert_kwargs):
    logger.debug("_create_system_doc: %s keys=%s", doctype, list(values.keys()))
    doc = _app_db().insert_doc(doctype, values, ignore_permissions=True, **insert_kwargs)
    frappe.db.commit()
    return doc


def _ensure_fiscal_year(posting_date: str | None, company: str):
    """Create a Fiscal Year covering the posting_date if none exists."""
    from datetime import date as _date, datetime as _datetime

    if not posting_date:
        dt = _date.today()
    elif isinstance(posting_date, str):
        try:
            dt = _datetime.fromisoformat(posting_date).date()
        except ValueError:
            dt = _date.today()
    elif isinstance(posting_date, _datetime):
        dt = posting_date.date()
    elif isinstance(posting_date, _date):
        dt = posting_date
    else:
        dt = _date.today()

    fy_start = dt.replace(month=7, day=1) if dt.month >= 7 else dt.replace(year=dt.year - 1, month=7, day=1)
    fy_end = fy_start.replace(year=fy_start.year + 1, month=6, day=30)
    fy_name = f"{fy_start.year}-{fy_end.year}"

    existing = frappe.db.get_value("Fiscal Year", fy_name, "name")
    if existing:
        return existing

    try:
        fy_doc = _create_system_doc("Fiscal Year", {
            "name": fy_name,
            "year": fy_name,
            "year_start_date": str(fy_start),
            "year_end_date": str(fy_end),
        }, ignore_mandatory=True)
        frappe.db.sql("""
            INSERT IGNORE INTO `tabFiscal Year Company`
            (name, parent, parenttype, parentfield, company, creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%s, %s, 'Fiscal Year', 'companies', %s, NOW(), NOW(), 'Administrator', 'Administrator', 0, 1)
        """, (f"{fy_name}-{company}", fy_name, company))
        frappe.db.commit()
        return fy_doc.name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return fy_name


def _company_abbr(company: str) -> str:
    abbr = _app_db().get_value("Company", company, "abbr")
    if abbr:
        return str(abbr)

    payable = _app_db().get_value(
        "Account",
        {"company": company, "account_type": "Payable"},
        "name",
    )
    if payable and " - " in str(payable):
        return str(payable).rsplit(" - ", 1)[-1]

    return "COMP"


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
    try:
        account = _app_db().get_value(
            "Item Default",
            {"parent": item_code, "company": company},
            "default_expense_account",
        )
        if account:
            return account
    except Exception:
        pass

    try:
        account = _app_db().get_value("Company", company, "default_expense_account")
        if account:
            return account
    except Exception:
        pass

    rows = _app_db().get_all(
        "Account",
        filters={"company": company, "root_type": "Expense", "is_group": 0},
        fields=["name"],
        limit=1,
        order_by="name asc",
    )
    if rows:
        return _value(rows[0], "name")

    abbr = _company_abbr(company)
    root_name = f"Expenses - {abbr}"
    if not _app_db().get_value("Account", root_name, "name"):
        _create_system_doc(
            "Account",
            {
                "name": root_name,
                "account_name": "Expenses",
                "company": company,
                "root_type": "Expense",
                "report_type": "Profit and Loss",
                "is_group": 1,
            },
            ignore_mandatory=True,
        )

    account_name = f"General Expenses - {abbr}"
    if not _app_db().get_value("Account", account_name, "name"):
        _create_system_doc(
            "Account",
            {
                "name": account_name,
                "account_name": "General Expenses",
                "company": company,
                "root_type": "Expense",
                "report_type": "Profit and Loss",
                "parent_account": root_name,
                "account_type": "Expense Account",
                "is_group": 0,
            },
            ignore_mandatory=True,
        )

    return account_name


def _get_default_cost_center(company: str):
    for field in ("default_cost_center", "cost_center", "default_cost_center_name"):
        try:
            value = _app_db().get_value("Company", company, field)
        except Exception:
            value = None
        if value:
            return value

    rows = _app_db().get_all(
        "Cost Center",
        filters={"company": company, "is_group": 0},
        fields=["name"],
        limit=1,
        order_by="name asc",
    )
    if rows:
        # Auto-set the found cost center as the default for the company
        cc_name = _value(rows[0], "name")
        _app_db().set_value("Company", company, "cost_center", cc_name)
        return cc_name

    abbr = _company_abbr(company)
    root_name = f"{company} - {abbr}"
    print(f"DEBUG: root_name={root_name}")
    if not _app_db().get_value("Cost Center", root_name, "name"):
        print(f"DEBUG: Creating root cost center")
        root_doc = _create_system_doc(
            "Cost Center",
            {
                "cost_center_name": company,
                "company": company,
                "is_group": 1,
            },
            ignore_mandatory=True,
        )
        root_name = root_doc.name

    cost_center_name = f"Main - {abbr}"
    print(f"DEBUG: cost_center_name={cost_center_name}")
    if not _app_db().get_value("Cost Center", cost_center_name, "name"):
        print(f"DEBUG: Creating main cost center")
        leaf_doc = _create_system_doc(
            "Cost Center",
            {
                "cost_center_name": "Main",
                "company": company,
                "parent_cost_center": root_name,
                "is_group": 0,
            },
        )
        cost_center_name = leaf_doc.name

    # Set as default for the company
    _app_db().set_value("Company", company, "cost_center", cost_center_name)
    return cost_center_name


def _ensure_default_payable_account(company: str):
    """
    Ensure the company has a default payable account set.
    If missing, finds or creates 'Accounts Payable'.
    """
    fields = ["default_payable_account", "abbr", "default_currency"]
    company_data = _app_db().get_all("Company", filters={"name": company}, fields=fields, limit=1)
    if not company_data:
        return
        
    data = company_data[0]
    if data.get("default_payable_account"):
        return

    abbr = data.get("abbr") or _company_abbr(company)
    
    # Try find existing
    existing = _app_db().get_all(
        "Account",
        filters={"company": company, "account_type": "Payable", "is_group": 0},
        fields=["name"],
        limit=1
    )
    if not existing:
        # Fallback to name search
        existing = _app_db().get_all(
            "Account",
            filters={"company": company, "account_name": ["like", "Accounts Payable%"], "is_group": 0},
            fields=["name"],
            limit=1
        )
        
    if existing:
        _app_db().set_value("Company", company, "default_payable_account", existing[0].get("name"))
        return

    # Not found, create it
    _create_system_doc(
        "Account",
        {
            "account_name": "Accounts Payable",
            "company": company,
            "root_type": "Liability",
            "report_type": "Balance Sheet",
            "account_type": "Payable",
            "is_group": 0,
            "parent_account": f"Current Liabilities - {abbr}",
        },
        ignore_mandatory=True,
    )
    # Note: We don't set it on company here, as the NEXT save/validate will find it


def _find_gst_template(company: str | None = None):
    """Return the GST purchase tax template for the given company.

    Searches with a company filter first so we never pick up another
    tenant's template (e.g. ATX accounts on a 'Test Pty Ltd' invoice).
    Falls back to an unfiltered search only if no company is supplied.
    """
    for pattern in ("%Non Capital%GST%", "%Capital%GST%", "%GST%"):
        filters = {"name": ("like", pattern)}
        if company:
            filters["company"] = company

        rows = frappe.get_all(
            "Purchase Taxes and Charges Template",
            filters=filters,
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

    abbr = _company_abbr(company)
    cost_center = _get_default_cost_center(company)
    rows = []
    for row in template_rows or []:
        account_head = _value(row, "account_head", "")
        if account_head and " - " in account_head:
            account_base = account_head.rsplit(" - ", 1)[0]
            account_head = f"{account_base} - {abbr}"

        rows.append(
            {
                "charge_type": _value(row, "charge_type", "On Net Total"),
                "account_head": account_head,
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

    existing = _app_db().get_value("Supplier", supplier_value, "name")
    if existing:
        return existing

    named = _app_db().get_value("Supplier", {"supplier_name": supplier_value}, "name")
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
        existing = _app_db().get_value("Supplier", name, "name")
        if not existing:
            break
        name = _normalize_name(f"{base_name}-{i}", fallback="Supplier", max_length=95)

    try:
        doc = _app_db().insert_doc("Supplier", {
            "name": name,
            "supplier_name": supplier_name,
            "supplier_group": supplier_group,
        }, ignore_permissions=True)
        # Commit immediately so ERPNext link validation (run during Purchase Invoice
        # doc.insert()) can find this record on the same DB connection.
        frappe.db.commit()
        return doc.name
    except frappe.DuplicateEntryError:
        # Row exists for another tenant — tenant-aware get_value missed it.
        # Safe to reuse: the name is globally unique and ERPNext link validation
        # only checks the primary key.
        frappe.db.rollback()
        return name


def _ensure_item_group(group_name: str) -> str:
    """Ensure the Item Group exists; auto-create under 'All Item Groups' if missing."""
    group_name = str(group_name).strip() if group_name else "All Item Groups"
    if not group_name or group_name == "All Item Groups":
        return "All Item Groups"

    existing = _app_db().get_value("Item Group", group_name, "name")
    if existing:
        return existing

    try:
        doc = _app_db().insert_doc("Item Group", {
            "name": group_name,
            "item_group_name": group_name,
            "parent_item_group": "All Item Groups",
            "is_group": 0,
        }, ignore_permissions=True)
        frappe.db.commit()
        return doc.name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return group_name


def _resolve_item_code(item_name: str, item_group: str) -> str:
    """Return existing item code or auto-create an Item under item_group."""
    item_name = str(item_name).strip() if item_name else None
    if not item_name:
        return item_name

    existing = _app_db().get_value("Item", item_name, "name")
    if existing:
        return existing

    resolved_group = _ensure_item_group(item_group)

    try:
        doc = _app_db().insert_doc("Item", {
            "name": item_name,
            "item_code": item_name,
            "item_name": item_name,
            "item_group": resolved_group,
            "is_purchase_item": 1,
            "is_sales_item": 0,
            "is_stock_item": 0,
            "is_fixed_asset": 0,
            "stock_uom": "Nos",
        }, ignore_permissions=True)
        frappe.db.commit()
        return doc.name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return item_name


def _resolve_item_identity(item: Any) -> tuple[str | None, str | None]:
    item_group = _value(item, "item_group", None) or "All Item Groups"
    item_code = _value(item, "item_code")
    item_name = _value(item, "item_name")

    preferred_name = str(item_name).strip() if item_name else None
    candidate_code = str(item_code).strip() if item_code else None

    if preferred_name:
        if not candidate_code or candidate_code == item_group:
            return preferred_name, item_group
        return preferred_name, item_group

    return candidate_code, item_group


class PurchaseInvoice(DocumentController):
    """
    Auto-populate accounting values for Purchase Invoice records created by the mobile app.

    Custom fields populated on every save:
      - expense_item_name  : primary item's display name
      - expense_item_group : primary item's group (Link → Item Group)
      - expense_items_count: total number of line items
    """

    def before_validate(self):
        company = _resolve_company_from_user() or _value(self, "company", None)
        if not company:
            logger.warning("before_validate: no company resolved, skipping enrichment")
            return
        _set_value(self, "company", company)
        logger.info("before_validate: company=%s", company)

        _get_default_cost_center(company)
        _ensure_default_payable_account(company)
        _ensure_fiscal_year(_value(self, "posting_date", None), company)

        supplier_value = _value(self, "supplier", None)
        if not supplier_value or not str(supplier_value).strip():
            supplier_value = "General Supplier"
        supplier = _resolve_supplier_name(supplier_value)
        if supplier:
            _set_value(self, "supplier", supplier)
            logger.debug("before_validate: supplier=%s", supplier)

        cost_center = _get_default_cost_center(company)
        raw_items = list(_value(self, "items", []) or [])
        self.set("items", [])

        primary_item_name = None
        primary_item_group = None

        for idx, item_data in enumerate(raw_items):
            item_code, item_group = _resolve_item_identity(item_data)
            if not item_code:
                self.append("items", item_data)
                continue

            resolved_code = _resolve_item_code(item_code, item_group)
            _set_value(item_data, "item_code", resolved_code)
            if _value(item_data, "item_name"):
                _set_value(item_data, "item_name", item_code)

            expense_account = _get_default_expense_account(resolved_code, company)
            if expense_account:
                _set_value(item_data, "expense_account", expense_account)
            if cost_center:
                _set_value(item_data, "cost_center", cost_center)

            self.append("items", item_data)

            if idx == 0:
                primary_item_name = item_code
                primary_item_group = item_group
                logger.debug("before_validate: primary item=%s group=%s", item_code, item_group)

        _set_value(self, "expense_item_name", primary_item_name or "")
        _set_value(self, "expense_item_group", primary_item_group or "")
        _set_value(self, "expense_items_count", len(raw_items))
        logger.info(
            "before_validate: expense_item_name=%s, expense_item_group=%s, expense_items_count=%d",
            primary_item_name, primary_item_group, len(raw_items),
        )

        wants_gst = bool(_value(self, "taxes_and_charges", None))
        existing_taxes = [
            _serialise(tax) for tax in _value(self, "taxes", []) or []
        ]
        manual_taxes = [tax for tax in existing_taxes if not _is_gst_row(tax)]

        if wants_gst:
            gst_template = _find_gst_template(company)
            if gst_template:
                _set_value(self, "taxes_and_charges", gst_template)
                self.set("taxes", [])
                for row in manual_taxes + _gst_template_rows(company, gst_template):
                    self.append("taxes", row)
                logger.debug("before_validate: GST template=%s applied", gst_template)
            else:
                _set_value(self, "taxes_and_charges", "")
                self.set("taxes", [])
                for row in manual_taxes:
                    self.append("taxes", row)
                logger.debug("before_validate: no GST template found for company=%s", company)
        else:
            _set_value(self, "taxes_and_charges", "")
            self.set("taxes", [])
            for row in manual_taxes:
                self.append("taxes", row)

    def after_insert(self):
        """Auto-submit the Purchase Invoice so it's not left as Draft.

        Sets status to 'Submitted' (not ERPNext's default 'Unpaid') since the
        expense tracker only uses two statuses: Draft and Submitted.
        """
        try:
            if hasattr(self, 'doc') and self.doc:
                doc = self.doc
            else:
                doc = self
            if getattr(doc, 'docstatus', 0) == 0:
                doc.docstatus = 1
                doc.save(ignore_permissions=True)
                # Override ERPNext's computed status (e.g. "Unpaid") with simple "Submitted"
                inv_name = getattr(doc, 'name', None)
                if inv_name:
                    frappe.db.set_value("Purchase Invoice", inv_name, "status", "Submitted", update_modified=False)
                frappe.db.commit()
                logger.info("after_insert: auto-submitted Purchase Invoice %s", inv_name)
        except Exception as e:
            logger.error("after_insert: auto-submit failed: %s", e)
