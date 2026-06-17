from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List

import frappe
from frappe_microservice import get_app
from frappe_microservice.controller import DocumentController

logger = logging.getLogger(__name__)


def _erpnext_accounts_utils():
    """Return ERPNext accounts utils when the app is installed (runtime image), else None.

    ERPNext is not vendored in this repo; the microservice bench still loads it via pip.
    Prefer these helpers over duplicating fiscal-year and company-scoped accounting logic.
    """
    try:
        import erpnext.accounts.utils as utils  # type: ignore[import-untyped]

        return utils
    except Exception:
        return None


def _app_db():
    return get_app().tenant_db


def _set_company_field(company: str, fieldname: str, value) -> None:
    """Update Company defaults without tenant-scoped get_doc (setup during PI enrich)."""
    if not company:
        return
    frappe.db.set_value("Company", company, fieldname, value, update_modified=False)


DEFAULT_GST_TEMPLATE = "AU GST 10%"
GST_MARKER = "gst"
# Australian expense product: all fallbacks and back-filled master data use AUD only.
DEFAULT_FALLBACK_CURRENCY = "AUD"

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


def _ensure_cost_center_by_name(expected_name: str, values: Dict[str, Any], **insert_kwargs) -> str:
    """Insert Cost Center if missing. Uses global name check: tenant-scoped get_value can
    miss rows with NULL/mismatched tenant_id while INSERT still hits PRIMARY duplicate."""
    if frappe.db.exists("Cost Center", expected_name):
        return expected_name
    try:
        return _create_system_doc("Cost Center", values, **insert_kwargs).name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return expected_name


def _link_fiscal_year_to_company(fy_name: str, company: str) -> None:
    """Ensure ``company`` is linked to ``fy_name`` for ERPNext company-scoped FY lookup."""
    if not fy_name or not company:
        return
    link_name = f"{fy_name}-{company}"
    if frappe.db.exists("Fiscal Year Company", link_name):
        return
    frappe.db.sql(
        """
        INSERT IGNORE INTO `tabFiscal Year Company`
        (name, parent, parenttype, parentfield, company, creation, modified, modified_by, owner, docstatus, idx)
        VALUES (%s, %s, 'Fiscal Year', 'companies', %s, NOW(), NOW(), 'Administrator', 'Administrator', 0, 1)
        """,
        (link_name, fy_name, company),
    )


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

    utils = _erpnext_accounts_utils()
    if utils:
        try:
            match = utils.get_fiscal_years(
                transaction_date=str(dt),
                company=company,
                raise_on_missing=False,
            )
            if match:
                first = match[0]
                if isinstance(first, tuple) and first:
                    return str(first[0])
        except Exception as exc:
            logger.debug("_ensure_fiscal_year: erpnext.get_fiscal_years skipped (%s)", exc)

    existing = frappe.db.get_value("Fiscal Year", fy_name, "name")
    if existing:
        _link_fiscal_year_to_company(existing, company)
        return existing

    # Any existing FY that covers the posting date is acceptable (e.g. a
    # calendar-year FY created by ERPNext bootstrap instead of a July-based one).
    existing_by_date = frappe.db.get_value(
        "Fiscal Year",
        [
            ["year_start_date", "<=", str(dt)],
            ["year_end_date", ">=", str(dt)],
        ],
        "name",
    )
    if existing_by_date:
        _link_fiscal_year_to_company(existing_by_date, company)
        return existing_by_date

    try:
        fy_doc = _create_system_doc("Fiscal Year", {
            "name": fy_name,
            "year": fy_name,
            "year_start_date": str(fy_start),
            "year_end_date": str(fy_end),
        }, ignore_mandatory=True)
        _link_fiscal_year_to_company(fy_doc.name, company)
        frappe.db.commit()
        return fy_doc.name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        _link_fiscal_year_to_company(fy_name, company)
        return fy_name


def _company_abbr(company: str) -> str:
    """Company abbreviation for account/CC naming (``Expenses - {abbr}``).

    Uses global ``frappe.db`` first — tenant-scoped lookups can miss Company rows
    and previously fell back to the hardcoded ``COMP`` suffix (breaking CoA paths).
    """
    if not company:
        return ""
    abbr = frappe.db.get_value("Company", company, "abbr")
    if abbr:
        return str(abbr).strip()
    try:
        abbr = _app_db().get_value("Company", company, "abbr")
        if abbr:
            return str(abbr).strip()
    except Exception:
        pass
    payable = frappe.db.get_value(
        "Account",
        {"company": company, "account_type": "Payable", "is_group": 0},
        "name",
    )
    if payable and " - " in str(payable):
        return str(payable).rsplit(" - ", 1)[-1].strip()
    # Last resort: initials from company name (never a fixed fake abbr).
    parts = [p for p in str(company).replace("_", " ").split() if p]
    if parts:
        return "".join(p[0] for p in parts[:6]).upper()[:10]
    return "CO"


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


def _find_expense_group_parent(company: str, abbr: str) -> str | None:
    """Existing expense group account for ``company`` (standard CoA or prior bootstrap)."""
    for name in (f"Expenses - {abbr}", "Expenses"):
        if not frappe.db.exists("Account", name):
            continue
        acct_company = frappe.db.get_value("Account", name, "company")
        is_group = frappe.db.get_value("Account", name, "is_group")
        if is_group and (not acct_company or acct_company == company):
            return name
    return frappe.db.get_value(
        "Account",
        {"company": company, "root_type": "Expense", "is_group": 1},
        "name",
    )


def _ensure_account_row(
    name: str,
    *,
    account_name: str,
    company: str,
    root_type: str,
    report_type: str,
    is_group: int = 0,
    account_type: str | None = None,
    parent_account: str | None = None,
) -> str:
    """Create Account if missing; global exists check avoids tenant-scoped duplicate INSERT."""
    if frappe.db.exists("Account", name):
        return name
    cc = _company_default_currency(company)
    payload: Dict[str, Any] = {
        "account_name": account_name,
        "company": company,
        "root_type": root_type,
        "report_type": report_type,
        "is_group": is_group,
        "account_currency": cc,
    }
    if account_type:
        payload["account_type"] = account_type
    if parent_account:
        payload["parent_account"] = parent_account
    try:
        _create_system_doc("Account", payload, ignore_mandatory=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
    return name


def _get_default_expense_account(item_code: str, company: str):
    try:
        account = frappe.db.get_value(
            "Item Default",
            {"parent": item_code, "company": company},
            "default_expense_account",
        )
        if account:
            return account
    except Exception:
        pass

    try:
        account = frappe.db.get_value("Company", company, "default_expense_account")
        if account:
            return account
    except Exception:
        pass

    account = frappe.db.get_value(
        "Account",
        {"company": company, "account_type": "Expense Account", "is_group": 0},
        "name",
    )
    if account:
        return account

    leaf_rows = frappe.get_all(
        "Account",
        filters={"company": company, "root_type": "Expense", "is_group": 0},
        fields=["name"],
        limit=1,
        order_by="name asc",
    )
    if leaf_rows:
        return leaf_rows[0].name

    abbr = _company_abbr(company)
    root_name = f"Expenses - {abbr}"
    parent_group = _find_expense_group_parent(company, abbr)
    if not parent_group:
        _ensure_account_row(
            root_name,
            account_name="Expenses",
            company=company,
            root_type="Expense",
            report_type="Profit and Loss",
            is_group=1,
        )
        parent_group = root_name if frappe.db.exists("Account", root_name) else None

    account_name = f"General Expenses - {abbr}"
    if not frappe.db.exists("Account", account_name) and parent_group:
        _ensure_account_row(
            account_name,
            account_name="General Expenses",
            company=company,
            root_type="Expense",
            report_type="Profit and Loss",
            is_group=0,
            account_type="Expense Account",
            parent_account=parent_group,
        )

    if frappe.db.exists("Account", account_name):
        return account_name

    fallback_rows = frappe.get_all(
        "Account",
        filters={"company": company, "root_type": "Expense", "is_group": 0},
        fields=["name"],
        limit=1,
    )
    if fallback_rows:
        return fallback_rows[0].name

    raise frappe.ValidationError(
        f"Could not find Parent Account: Expenses - {abbr}"
        if not parent_group
        else f"No expense account available for company {company!r}"
    )


def _ensure_company_round_off_cost_center(company: str, cost_center_name: str | None) -> None:
    """ERPNext requires Company.round_off_cost_center when submitting Purchase Invoice (rounding GL)."""
    if not cost_center_name:
        return
    try:
        current = _app_db().get_value("Company", company, "round_off_cost_center")
    except Exception:
        current = None
    if current:
        return
    try:
        _set_company_field(company, "round_off_cost_center", cost_center_name)
    except Exception as exc:
        logger.warning(
            "ensure round_off_cost_center: could not set Company %r → %r (%s)",
            company,
            cost_center_name,
            exc,
        )


def _get_default_cost_center(company: str):
    for field in ("default_cost_center", "cost_center", "default_cost_center_name"):
        try:
            value = _app_db().get_value("Company", company, field)
        except Exception:
            value = None
        if value:
            _ensure_company_round_off_cost_center(company, value)
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
        _set_company_field(company, "cost_center", cc_name)
        _ensure_company_round_off_cost_center(company, cc_name)
        return cc_name

    abbr = _company_abbr(company)
    root_name = f"{company} - {abbr}"
    root_name = _ensure_cost_center_by_name(
        root_name,
        {
            "cost_center_name": company,
            "company": company,
            "is_group": 1,
        },
        ignore_mandatory=True,
    )

    cost_center_name = f"Main - {abbr}"
    cost_center_name = _ensure_cost_center_by_name(
        cost_center_name,
        {
            "cost_center_name": "Main",
            "company": company,
            "parent_cost_center": root_name,
            "is_group": 0,
        },
    )

    # Set as default for the company
    _set_company_field(company, "cost_center", cost_center_name)
    _ensure_company_round_off_cost_center(company, cost_center_name)
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
        _set_company_field(company, "default_payable_account", existing[0].get("name"))
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


def _get_au_simpler_bas_accounts(company: str) -> Dict[str, Any]:
    """Read G1 income accounts and 1A/1B GST accounts from AU Simpler BAS Report Setup."""
    empty: Dict[str, Any] = {"income_accounts": [], "account_1a": "", "account_1b": ""}
    if not company:
        return empty
    try:
        if not frappe.db.table_exists("AU Simpler BAS Report Setup"):
            return empty
    except Exception:
        return empty
    try:
        row = frappe.db.get_value(
            "AU Simpler BAS Report Setup",
            company,
            ["account_1a", "account_1b"],
            as_dict=True,
        )
    except Exception as exc:
        logger.debug("_get_au_simpler_bas_accounts setup: %s", exc)
        row = None
    account_1a = _as_plain_str((row or {}).get("account_1a"))
    account_1b = _as_plain_str((row or {}).get("account_1b"))
    income_accounts: List[str] = []
    try:
        if frappe.db.table_exists("Income Account for Simpler BAS"):
            raw = frappe.get_all(
                "Income Account for Simpler BAS",
                filters={
                    "parent": company,
                    "parenttype": "AU Simpler BAS Report Setup",
                    "parentfield": "accounts_g1",
                },
                pluck="account",
            )
            income_accounts = [_as_plain_str(a) for a in (raw or []) if _as_plain_str(a)]
    except Exception as exc:
        logger.debug("_get_au_simpler_bas_accounts g1: %s", exc)
    return {
        "income_accounts": income_accounts,
        "account_1a": account_1a,
        "account_1b": account_1b,
    }


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


def _client_requests_purchase_gst(doc) -> bool:
    """True when mobile/client opted into GST on a purchase invoice."""
    tc = (_value(doc, "taxes_and_charges", None) or "").strip()
    if tc:
        tc_l = tc.lower()
        if tc_l == "gst" or "gst" in tc_l:
            return True
    existing_taxes = [_serialise(t) for t in _value(doc, "taxes", []) or []]
    return any(_is_gst_row(t) for t in existing_taxes)


def _ensure_purchase_gst_template(company: str) -> str | None:
    """Create the standard AU purchase GST template when missing (lazy provisioning)."""
    if not company:
        return None

    existing = _find_gst_template(company)
    if existing:
        return existing

    abbr = _company_abbr(company)
    template_name = f"Non Capital (GST 10%) - {company}"
    gst_account = f"GST Paid - {abbr}"
    bas = _get_au_simpler_bas_accounts(company)
    account_1b = _as_plain_str(bas.get("account_1b"))
    if account_1b:
        gst_account = account_1b

    if not frappe.db.exists("Account", gst_account):
        parent = f"Current Assets - {abbr}"
        if not frappe.db.exists("Account", parent):
            parent = f"Assets - {abbr}"
        _ensure_account_row(
            gst_account,
            account_name="GST Paid",
            company=company,
            root_type="Asset",
            report_type="Balance Sheet",
            is_group=0,
            account_type="Tax",
            parent_account=parent if frappe.db.exists("Account", parent) else None,
        )

    tenant_id = ""
    try:
        tenant_id = _as_plain_str(_app_db().get_value("Company", company, "tenant_id"))
    except Exception:
        tenant_id = _as_plain_str(frappe.db.get_value("Company", company, "tenant_id"))

    try:
        frappe.db.sql(
            """
            INSERT IGNORE INTO `tabPurchase Taxes and Charges Template`
            (name, title, company, tenant_id, is_default,
             creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%(name)s, %(title)s, %(company)s, %(tenant_id)s, 1,
                    NOW(), NOW(), 'Administrator', 'Administrator', 0, 0)
            """,
            {
                "name": template_name,
                "title": template_name,
                "company": company,
                "tenant_id": tenant_id,
            },
        )
        child_name = f"{template_name}-row-1"
        frappe.db.sql(
            """
            INSERT IGNORE INTO `tabPurchase Taxes and Charges`
            (name, parent, parenttype, parentfield,
             charge_type, account_head, description, rate,
             add_deduct_tax, included_in_print_rate, category,
             tenant_id, creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%(name)s, %(parent)s, 'Purchase Taxes and Charges Template', 'taxes',
                    'On Net Total', %(account_head)s, 'GST 10%%', 10,
                    'Add', 0, 'Total',
                    %(tenant_id)s, NOW(), NOW(), 'Administrator', 'Administrator', 0, 1)
            """,
            {
                "name": child_name,
                "parent": template_name,
                "account_head": gst_account,
                "tenant_id": tenant_id,
            },
        )
        frappe.db.commit()
    except Exception as exc:
        logger.warning("ensure_purchase_gst_template: could not create template for %r (%s)", company, exc)
        frappe.db.rollback()

    return _find_gst_template(company)


def _resolve_purchase_gst_template(company: str, taxes_and_charges: str | None) -> str | None:
    """Map mobile ``taxes_and_charges: GST`` to the company purchase tax template."""
    tc = (taxes_and_charges or "").strip()
    if not tc:
        return None
    if tc.lower() == "gst" or "gst" in tc.lower():
        found = _find_gst_template(company)
        return found or _ensure_purchase_gst_template(company)
    if frappe.db.exists("Purchase Taxes and Charges Template", tc):
        return tc
    return _find_gst_template(company)


def _company_accounts_nested_set_broken(company: str) -> bool:
    """True when SQL-provisioned accounts still have lft=rgt=0 (breaks P&L rollups)."""
    if not company:
        return False
    try:
        result = frappe.db.sql(
            """
            SELECT COUNT(*)
            FROM `tabAccount`
            WHERE company = %s
              AND COALESCE(lft, 0) = 0
              AND COALESCE(rgt, 0) = 0
            """,
            (company,),
        )
        if not result or not isinstance(result, (list, tuple)):
            return False
        first = result[0]
        count = first[0] if isinstance(first, (list, tuple)) else first
        if not isinstance(count, (int, float)):
            return False
        return int(count) > 0
    except Exception:
        return False


def _ensure_account_nested_set(company: str) -> None:
    """Rebuild Account tree once per request when provisioning left lft/rgt unset."""
    if not company:
        return
    flag_key = f"account_nested_set_rebuilt_{company}"
    if getattr(frappe.flags, flag_key, False):
        return
    setattr(frappe.flags, flag_key, True)
    if not _company_accounts_nested_set_broken(company):
        return
    try:
        from frappe.utils.nestedset import rebuild_tree

        rebuild_tree("Account")
        frappe.db.commit()
        logger.info("ensure_account_nested_set: rebuilt Account tree for company=%s", company)
    except Exception as exc:
        logger.warning("ensure_account_nested_set: rebuild_tree failed for %r (%s)", company, exc)
        frappe.db.rollback()


def _gst_template_rows(
    company: str,
    template_name: str = DEFAULT_GST_TEMPLATE,
    *,
    gst_inclusive: bool = True,
) -> List[Dict[str, Any]]:
    """Build purchase GST tax rows for mobile expenses.

    When ``gst_inclusive`` is True (default), line ``rate`` is treated as a GST-inclusive
    bill total (e.g. user enters 100 → ~90.91 net + ~9.09 GST → grand total 100).
    When False, ERPNext adds GST on top of the net rate (100 → 110).
    """
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
        try:
            if frappe.db.table_exists("Purchase Taxes and Charges Template Detail"):
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
        except Exception:
            template_rows = []
    if not template_rows:
        return []

    abbr = _company_abbr(company)
    cost_center = _get_default_cost_center(company)
    bas_gst_purchase = _as_plain_str(_get_au_simpler_bas_accounts(company).get("account_1b"))
    rows = []
    for row in template_rows or []:
        account_head = _value(row, "account_head", "")
        if bas_gst_purchase:
            account_head = bas_gst_purchase
        elif account_head and " - " in account_head:
            account_base = account_head.rsplit(" - ", 1)[0]
            account_head = f"{account_base} - {abbr}"

        included = int(bool(_value(row, "included_in_print_rate", 0)))
        if gst_inclusive:
            included = 1
        rows.append(
            {
                "charge_type": _value(row, "charge_type", "On Net Total"),
                "account_head": account_head,
                "description": _value(row, "description", DEFAULT_GST_TEMPLATE),
                "rate": _value(row, "rate"),
                "cost_center": _value(row, "cost_center", cost_center),
                "included_in_print_rate": included,
                "add_deduct_tax": _value(row, "add_deduct_tax", "Add"),
            }
        )
    return rows


def _as_plain_str(val: Any) -> str:
    """Coerce get_value results for currency fields (tests may return MagicMock/dict)."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return ""


def _company_default_currency(company: str) -> str:
    try:
        cur = _app_db().get_value("Company", company, "default_currency")
    except Exception:
        cur = None
    return _as_plain_str(cur) or DEFAULT_FALLBACK_CURRENCY


def _ensure_company_default_currency(company: str) -> None:
    """Persist Company.default_currency and reporting_currency when missing.

    default_currency: ERPNext get_company_currency() returns None without it.
    reporting_currency: GL entry set_amount_in_reporting_currency() fails with
    'Unable to find exchange rate for AUD to None' when NULL.
    """
    if not company:
        return
    cur = _as_plain_str(_app_db().get_value("Company", company, "default_currency"))
    rep = _as_plain_str(_app_db().get_value("Company", company, "reporting_currency"))
    updates = {}
    if not cur:
        updates["default_currency"] = DEFAULT_FALLBACK_CURRENCY
    if not rep:
        updates["reporting_currency"] = cur or DEFAULT_FALLBACK_CURRENCY
    if not updates:
        return
    try:
        for field, value in updates.items():
            _set_company_field(company, field, value)
        cached = getattr(frappe.flags, "company_currency", None)
        if isinstance(cached, dict):
            cached.pop(company, None)
        frappe.clear_document_cache("Company", company)
    except Exception as exc:
        logger.warning(
            "ensure_company_default_currency: could not set Company %r fields %s (%s)",
            company,
            list(updates.keys()),
            exc,
        )


def _ensure_supplier_party_currency(supplier: str, company: str) -> None:
    """ERPNext submit resolves exchange rates using Supplier.default_currency; NULL → 'AUD to None'."""
    if not supplier or not company:
        return
    try:
        cur = _app_db().get_value("Supplier", supplier, "default_currency")
    except Exception:
        cur = None
    if _as_plain_str(cur):
        return
    cc = _company_default_currency(company)
    try:
        _app_db().set_value("Supplier", supplier, "default_currency", cc)
    except Exception as exc:
        logger.warning(
            "ensure_supplier_party_currency: could not set Supplier %r → %r (%s)",
            supplier,
            cc,
            exc,
        )


def _ensure_default_payable_account_currency(company: str) -> None:
    """Payable Account.account_currency must match company currency or submit looks up AUD→None."""
    if not company:
        return
    try:
        payable = _app_db().get_value("Company", company, "default_payable_account")
    except Exception:
        payable = None
    if not payable:
        return
    try:
        ac = _app_db().get_value("Account", payable, "account_currency")
    except Exception:
        ac = None
    if _as_plain_str(ac):
        return
    cc = _company_default_currency(company)
    try:
        _app_db().set_value("Account", payable, "account_currency", cc)
    except Exception as exc:
        logger.warning(
            "ensure_default_payable_account_currency: could not set Account %r (%s)",
            payable,
            exc,
        )


def clear_company_currency_cache(company: str) -> None:
    """Clear all ERPNext company-currency caches so get_company_currency() re-reads from DB.

    Must run unconditionally before doc.submit() — even when Company.default_currency
    is already 'AUD' in the DB — because erpnext.get_company_currency() caches via
    frappe.flags.company_currency (request-level) and frappe.db.get_value(cache=True)
    (Redis document cache).  If any earlier code path loaded the Company doc (e.g.
    set_value for cost_center, round_off_cost_center) and the doc-cache snapshot
    happened to have default_currency = NULL (transient state or SQL not yet visible),
    the cached None persists for the rest of the request → 'AUD to None'.
    """
    if not company:
        return
    # 1. Request-level dict used by erpnext.get_company_currency()
    cached = getattr(frappe.flags, "company_currency", None)
    if isinstance(cached, dict):
        cached.pop(company, None)
    # 2. Redis document cache for Company and Supplier
    frappe.clear_document_cache("Company", company)


def clear_account_cache_for_company(company: str) -> None:
    """Invalidate document cache for Account after raw SQL currency patches."""
    if not company:
        return
    try:
        for acc in frappe.get_all("Account", filters={"company": company}, pluck="name"):
            frappe.clear_document_cache("Account", acc)
    except Exception as exc:
        logger.debug("clear_account_cache_for_company: %s", exc)


def _ensure_all_company_accounts_currency(company: str) -> None:
    """Set account_currency on every company Account row that is still blank.

    Partial provisioning (signup SQL, templates) often leaves Payable / Tax / Expense
    accounts without account_currency. ERPNext then sets party_account_currency = None
    and submit fails with 'exchange rate for AUD to None'.

    Uses a two-pronged approach:
    1. Raw SQL UPDATE to catch ALL rows regardless of tenant_id (fast, covers edge cases).
    2. Tenant-scoped get_all + set_value to clear individual document caches.
    """
    if not company:
        return
    cc = _company_default_currency(company)

    # Prong 1: raw SQL — catches accounts with NULL/mismatched tenant_id that the
    # tenant-scoped query below would miss.
    try:
        frappe.db.sql(
            "UPDATE `tabAccount` SET `account_currency` = %s "
            "WHERE `company` = %s AND (`account_currency` IS NULL OR `account_currency` = '')",
            (cc, company),
        )
    except Exception as exc:
        logger.warning(
            "ensure_all_company_accounts_currency: raw SQL update company=%r (%s)",
            company,
            exc,
        )

    # Prong 2: tenant-scoped loop only to clear the document cache for each account.
    try:
        rows = _app_db().get_all(
            "Account",
            filters={"company": company},
            fields=["name", "account_currency"],
        )
    except Exception as exc:
        logger.warning(
            "ensure_all_company_accounts_currency: list accounts company=%r (%s)",
            company,
            exc,
        )
        return
    for row in rows or []:
        acc_name = _value(row, "name")
        if not acc_name:
            continue
        if not _as_plain_str(_value(row, "account_currency")):
            try:
                _app_db().set_value("Account", acc_name, "account_currency", cc)
            except Exception as exc:
                logger.warning(
                    "ensure_all_company_accounts_currency: Account %r (%s)",
                    acc_name,
                    exc,
                )


def ensure_purchase_invoice_item_defaults(doc) -> None:
    """Set child-row defaults ERPNext 16 expects when site DocType sync lags app code."""
    for item in _value(doc, "items", []) or []:
        if _value(item, "delivered_by_supplier", None) is None:
            _set_value(item, "delivered_by_supplier", 0)


def normalize_purchase_invoice_payment_dates(doc) -> None:
    """Align bill_date and due_date with posting_date for mobile expense invoices.

    ERPNext validates ``due_date >= bill_date`` (Supplier Invoice Date). Drafts saved
    on an earlier day can fail on submit when ``set_missing_values`` sets ``bill_date``
    to today while ``due_date`` remains on the original posting date.
    """
    from frappe.utils import getdate

    posting = _value(doc, "posting_date", None)
    if not isinstance(posting, (str, date, datetime)):
        return

    doc.bill_date = posting
    due = _value(doc, "due_date", None) or posting
    if not isinstance(due, (str, date, datetime)):
        due = posting
    if getdate(due) < getdate(posting):
        doc.due_date = posting
    else:
        doc.due_date = due


def ensure_purchase_invoice_submit_prereqs(
    company: str, supplier: str | None, posting_date=None,
) -> None:
    """Patch master data before doc.submit() so currency / exchange-rate validation passes."""
    _ensure_account_nested_set(company)
    _ensure_company_default_currency(company)
    _get_default_cost_center(company)
    _ensure_default_payable_account(company)
    _ensure_all_company_accounts_currency(company)
    _ensure_default_payable_account_currency(company)
    if company and posting_date is not None:
        _ensure_fiscal_year(posting_date, company)
    elif company:
        _ensure_fiscal_year(None, company)
    if supplier:
        _ensure_supplier_party_currency(supplier, company)
        frappe.clear_document_cache("Supplier", supplier)

    # CRITICAL: unconditionally clear company-currency caches so ERPNext's
    # get_company_currency() re-reads from DB during doc.submit().
    # Even though Company.default_currency IS 'AUD' in the DB, earlier
    # code in the same request may have cached stale values.
    clear_company_currency_cache(company)


def _ensure_supplier_group(group_name: str) -> str:
    """Ensure a Supplier Group row exists; create if missing.

    The signup-service skips the ERPNext setup wizard, so standard fixtures
    like 'All Supplier Groups' may not exist yet.
    """
    if frappe.db.exists("Supplier Group", group_name):
        return group_name
    try:
        frappe.db.sql("""
            INSERT IGNORE INTO `tabSupplier Group`
            (name, supplier_group_name, is_group, lft, rgt,
             creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%s, %s, 1, 1, 2, NOW(), NOW(),
                    'Administrator', 'Administrator', 0, 0)
        """, (group_name, group_name))
        frappe.db.commit()
    except Exception as exc:
        logger.warning("_ensure_supplier_group(%r): %s", group_name, exc)
    return group_name


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


def _resolve_supplier_name(supplier_value, company: str | None = None):
    supplier_value = str(supplier_value).strip() if supplier_value else None
    if not supplier_value:
        return None

    party_cc = _company_default_currency(company) if company else None

    existing = _app_db().get_value("Supplier", supplier_value, "name")
    if existing:
        if company:
            _ensure_supplier_party_currency(existing, company)
        return existing

    named = _app_db().get_value("Supplier", {"supplier_name": supplier_value}, "name")
    if named:
        if company:
            _ensure_supplier_party_currency(named, company)
        return named

    supplier_group = _default_supplier_group()
    return _create_supplier(supplier_value, supplier_group, default_currency=party_cc)


def _normalize_name(value: str, fallback: str = "Supplier", max_length: int = 100) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        normalized = fallback
    return normalized[:max_length]


def _create_supplier(
    supplier_name: str,
    supplier_group: str | None,
    default_currency: str | None = None,
):
    if not supplier_group:
        supplier_group = "All Supplier Groups"

    _ensure_supplier_group(supplier_group)

    base_name = _normalize_name(supplier_name, fallback="Supplier", max_length=95)
    if not base_name:
        base_name = "Supplier"

    name = base_name

    for i in range(1, 20):
        existing = _app_db().get_value("Supplier", name, "name")
        if not existing:
            break
        name = _normalize_name(f"{base_name}-{i}", fallback="Supplier", max_length=95)

    supplier_payload: Dict[str, Any] = {
        "name": name,
        "supplier_name": supplier_name,
        "supplier_group": supplier_group,
    }
    if default_currency:
        supplier_payload["default_currency"] = default_currency

    try:
        doc = _app_db().insert_doc("Supplier", supplier_payload, ignore_permissions=True)
        frappe.db.commit()
        return doc.name
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return name


def _ensure_root_item_group():
    """Ensure the 'All Item Groups' root exists (setup wizard normally creates it)."""
    if frappe.db.exists("Item Group", "All Item Groups"):
        return
    try:
        frappe.db.sql("""
            INSERT IGNORE INTO `tabItem Group`
            (name, item_group_name, is_group, lft, rgt,
             creation, modified, modified_by, owner, docstatus, idx)
            VALUES ('All Item Groups', 'All Item Groups', 1, 1, 2,
                    NOW(), NOW(), 'Administrator', 'Administrator', 0, 0)
        """)
        frappe.db.commit()
    except Exception as exc:
        logger.warning("_ensure_root_item_group: %s", exc)


def _ensure_uom(uom_name: str) -> str:
    """Ensure a UOM row exists (setup wizard normally creates these)."""
    if frappe.db.exists("UOM", uom_name):
        return uom_name
    try:
        frappe.db.sql("""
            INSERT IGNORE INTO `tabUOM`
            (name, uom_name, enabled, must_be_whole_number,
             creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%s, %s, 1, 1, NOW(), NOW(),
                    'Administrator', 'Administrator', 0, 0)
        """, (uom_name, uom_name))
        frappe.db.commit()
    except Exception as exc:
        logger.warning("_ensure_uom(%r): %s", uom_name, exc)
    return uom_name


def _ensure_item_group(group_name: str) -> str:
    """Ensure the Item Group exists; auto-create under 'All Item Groups' if missing."""
    group_name = str(group_name).strip() if group_name else "All Item Groups"

    _ensure_root_item_group()

    if not group_name or group_name == "All Item Groups":
        return "All Item Groups"

    if frappe.db.exists("Item Group", group_name):
        return group_name

    # SQL insert avoids loading saas_platform.overrides.item_group.TenantItemGroup when
    # the bench app is missing overrides/__init__.py (common in older central-site images).
    try:
        frappe.db.sql(
            """
            INSERT IGNORE INTO `tabItem Group`
            (name, item_group_name, parent_item_group, is_group, lft, rgt,
             creation, modified, modified_by, owner, docstatus, idx)
            VALUES (%s, %s, 'All Item Groups', 0, 1, 2,
                    NOW(), NOW(), 'Administrator', 'Administrator', 0, 0)
            """,
            (group_name, group_name),
        )
        tenant_id = None
        try:
            tenant_id = _app_db().get_tenant_id()
        except Exception:
            pass
        if tenant_id and frappe.db.has_column("Item Group", "tenant_id"):
            frappe.db.sql(
                "UPDATE `tabItem Group` SET tenant_id = %s WHERE name = %s",
                (tenant_id, group_name),
            )
        frappe.db.commit()
    except Exception as exc:
        logger.warning("_ensure_item_group(%r): %s", group_name, exc)
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
    _ensure_uom("Nos")

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


def _expense_title(primary_item: str | None, items_count: int, remarks: str | None) -> str:
    """List / document title: what was purchased, not the supplier."""
    primary = (primary_item or "").strip()
    if primary:
        n = max(int(items_count or 0), 0)
        if n > 1:
            return f"{primary} (+{n - 1} more)"[:140]
        return primary[:140]
    rem = (remarks or "").strip()
    return rem[:140] if rem else ""


class PurchaseInvoice(DocumentController):
    """
    Auto-populate accounting values for Purchase Invoice records created by the mobile app.

    Create flow: POST keeps the document as Draft (docstatus 0). After the user confirms,
    the app calls POST /api/method/frappe.client.submit (body ``doc`` or ``name``) so Frappe
    runs the real Purchase Invoice submit path (doc.submit()).

    Custom fields populated on every save:
      - expense_item_name  : primary item's display name
      - expense_item_group : primary item's group (Link → Item Group)
      - expense_items_count: total number of line items
      - title              : human title — primary line item(s), not supplier
    """

    def before_validate(self):
        doc = self.doc
        # insert_doc() runs DocumentHooks.before_validate, then Frappe doc.insert()
        # runs before_validate again via run_before_save_methods — without this guard,
        # items/taxes are rebuilt twice and child rows duplicate → DuplicateEntryError.
        if getattr(doc.flags, "expense_pi_enriched", False):
            return

        company = _value(self, "company", None) or _resolve_company_from_user()
        if not company:
            logger.warning("before_validate: no company resolved, skipping enrichment")
            return
        _set_value(self, "company", company)
        logger.info("before_validate: company=%s", company)

        _ensure_account_nested_set(company)
        _ensure_company_default_currency(company)
        # Default currency and conversion_rate from company when not supplied,
        # preventing Frappe's party-account currency mismatch validation error.
        if not _value(self, "currency", None):
            _set_value(self, "currency", _company_default_currency(company))
        if not _value(self, "conversion_rate", None):
            _set_value(self, "conversion_rate", 1.0)

        _get_default_cost_center(company)
        _ensure_default_payable_account(company)
        _ensure_default_payable_account_currency(company)
        _ensure_fiscal_year(_value(self, "posting_date", None), company)

        supplier_value = _value(self, "supplier", None)
        if not supplier_value or not str(supplier_value).strip():
            supplier_value = "General Supplier"
        supplier = _resolve_supplier_name(supplier_value, company)
        if supplier:
            _set_value(self, "supplier", supplier)
            logger.debug("before_validate: supplier=%s", supplier)

        cost_center = _get_default_cost_center(company)
        raw_items = list(_value(self, "items", []) or [])
        self.set("items", [])

        primary_item_name = None
        primary_item_group = None

        for idx, item_data in enumerate(raw_items):
            # ERPNext 16 AccountsController.is_drop_ship reads this on child rows;
            # microservice sites may lag ERPNext DocType sync so set explicitly.
            if _value(item_data, "delivered_by_supplier", None) is None:
                _set_value(item_data, "delivered_by_supplier", 0)

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

        expense_title = _expense_title(
            primary_item_name, len(raw_items), _value(self, "remarks", None),
        )
        if expense_title:
            _set_value(self, "title", expense_title)

        wants_gst = _client_requests_purchase_gst(self)
        existing_taxes = [
            _serialise(tax) for tax in _value(self, "taxes", []) or []
        ]
        manual_taxes = [tax for tax in existing_taxes if not _is_gst_row(tax)]

        if wants_gst:
            gst_template = _resolve_purchase_gst_template(
                company,
                _value(self, "taxes_and_charges", None),
            )
            if gst_template:
                _set_value(self, "taxes_and_charges", gst_template)
                self.set("taxes", [])
                # Mobile UX: user-entered line rate is GST-inclusive when GST is on.
                for row in manual_taxes + _gst_template_rows(
                    company, gst_template, gst_inclusive=True
                ):
                    self.append("taxes", row)
                logger.debug("before_validate: GST template=%s applied", gst_template)
            else:
                _set_value(self, "taxes_and_charges", "")
                self.set("taxes", [])
                for row in manual_taxes:
                    self.append("taxes", row)
                logger.warning(
                    "before_validate: GST requested but no purchase template for company=%s",
                    company,
                )
        else:
            _set_value(self, "taxes_and_charges", "")
            self.set("taxes", [])
            for row in manual_taxes:
                self.append("taxes", row)

        normalize_purchase_invoice_payment_dates(doc)

        doc.flags.expense_pi_enriched = True
