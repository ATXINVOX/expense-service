"""Ensure AU Simpler BAS Report Setup (G1 / 1A / 1B) for Cypress integration companies."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Optional

import frappe


def _company_currency(company: str) -> str:
    return frappe.db.get_value("Company", company, "default_currency") or "AUD"


def _ensure_account(
    company: str,
    full_name: str,
    account_name: str,
    *,
    root_type: str,
    report_type: str,
    account_type: str,
    parent_account: str,
) -> str:
    if frappe.db.exists("Account", full_name):
        return full_name
    if not parent_account or not frappe.db.exists("Account", parent_account):
        return ""
    currency = _company_currency(company)
    doc = frappe.get_doc(
        {
            "doctype": "Account",
            "account_name": account_name,
            "company": company,
            "parent_account": parent_account,
            "root_type": root_type,
            "report_type": report_type,
            "account_type": account_type,
            "is_group": 0,
            "account_currency": currency,
        }
    )
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    return doc.name


def _income_parent(company: str, abbr: str) -> str:
    for candidate in (f"Income - {abbr}", f"Direct Income - {abbr}"):
        if frappe.db.exists("Account", candidate):
            return candidate
    return (
        frappe.db.get_value(
            "Account",
            {"company": company, "root_type": "Income", "is_group": 1},
            "name",
        )
        or ""
    )


def _tax_parent(company: str, abbr: str) -> str:
    for candidate in (f"Duties and Taxes - {abbr}", f"Tax Assets - {abbr}"):
        if frappe.db.exists("Account", candidate):
            return candidate
    return (
        frappe.db.get_value(
            "Account",
            {"company": company, "account_type": "Tax", "is_group": 1},
            "name",
        )
        or frappe.db.get_value(
            "Account",
            {"company": company, "root_type": "Liability", "is_group": 1},
            "name",
        )
        or ""
    )


def ensure_invox_bas_accounts(company: str, abbr: str) -> Dict[str, str]:
    """Create Invox-style G1/1A/1B accounts when missing; return resolved names."""
    abbr = (abbr or "").strip()
    sales_g1 = f"Sales - {abbr}"
    gst_1a = f"GST Collected - {abbr}"
    gst_1b = f"GST Paid - {abbr}"

    income_parent = _income_parent(company, abbr)
    if income_parent:
        sales_g1 = _ensure_account(
            company,
            sales_g1,
            "Sales",
            root_type="Income",
            report_type="Profit and Loss",
            account_type="Income Account",
            parent_account=income_parent,
        ) or sales_g1

    tax_parent = _tax_parent(company, abbr)
    if tax_parent:
        gst_1a = _ensure_account(
            company,
            gst_1a,
            "GST Collected",
            root_type="Liability",
            report_type="Balance Sheet",
            account_type="Tax",
            parent_account=tax_parent,
        ) or gst_1a
        gst_1b = _ensure_account(
            company,
            gst_1b,
            "GST Paid",
            root_type="Liability",
            report_type="Balance Sheet",
            account_type="Tax",
            parent_account=tax_parent,
        ) or gst_1b

    if not frappe.db.exists("Account", sales_g1):
        fallback = frappe.db.get_value(
            "Account",
            {"company": company, "account_type": "Income Account", "is_group": 0},
            "name",
        )
        if fallback:
            sales_g1 = str(fallback)
    if not frappe.db.exists("Account", gst_1a):
        gst_1a = (
            frappe.db.get_value(
                "Account",
                {
                    "company": company,
                    "account_type": "Tax",
                    "is_group": 0,
                    "account_name": ("like", "%GST Collected%"),
                },
                "name",
            )
            or gst_1a
        )
    if not frappe.db.exists("Account", gst_1b):
        gst_1b = (
            frappe.db.get_value(
                "Account",
                {
                    "company": company,
                    "account_type": "Tax",
                    "is_group": 0,
                    "account_name": ("like", "%GST Paid%"),
                },
                "name",
            )
            or gst_1b
        )

    return {
        "sales_g1": sales_g1 if frappe.db.exists("Account", sales_g1) else "",
        "account_1a": gst_1a if frappe.db.exists("Account", gst_1a) else "",
        "account_1b": gst_1b if frappe.db.exists("Account", gst_1b) else "",
    }


def _g1_accounts_from_setup(company: str) -> list[str]:
    if not frappe.db.table_exists("Income Account for Simpler BAS"):
        return []
    return frappe.get_all(
        "Income Account for Simpler BAS",
        filters={
            "parent": company,
            "parenttype": "AU Simpler BAS Report Setup",
            "parentfield": "accounts_g1",
        },
        pluck="account",
    ) or []


def ensure_au_simpler_bas_report_setup(company: str, abbr: str) -> Dict[str, str]:
    """Create or patch AU Simpler BAS Report Setup for integration/Cypress."""
    accounts = ensure_invox_bas_accounts(company, abbr)
    if not accounts.get("sales_g1") or not accounts.get("account_1a") or not accounts.get("account_1b"):
        print(f"BAS bootstrap: incomplete accounts for {company}: {accounts}")
        return accounts

    if not frappe.db.table_exists("AU Simpler BAS Report Setup"):
        print("BAS bootstrap: AU Simpler BAS Report Setup table missing")
        return accounts

    setup_name = company
    if frappe.db.exists("AU Simpler BAS Report Setup", setup_name):
        doc = frappe.get_doc("AU Simpler BAS Report Setup", setup_name)
        changed = False
        if not (doc.get("account_1a") or "").strip():
            doc.account_1a = accounts["account_1a"]
            changed = True
        if not (doc.get("account_1b") or "").strip():
            doc.account_1b = accounts["account_1b"]
            changed = True
        existing_g1 = {
            row.account
            for row in (doc.get("accounts_g1") or [])
            if getattr(row, "account", None)
        }
        if accounts["sales_g1"] not in existing_g1:
            doc.append("accounts_g1", {"account": accounts["sales_g1"]})
            changed = True
        if changed:
            doc.flags.ignore_mandatory = True
            doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("AU Simpler BAS Report Setup")
        doc.company = company
        doc.account_1a = accounts["account_1a"]
        doc.account_1b = accounts["account_1b"]
        doc.append("accounts_g1", {"account": accounts["sales_g1"]})
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)

    frappe.db.commit()
    print(f"BAS bootstrap: setup ready for {company} -> {accounts}")
    return accounts


def write_bas_fixture(
    fixture_path: pathlib.Path,
    *,
    company: str,
    bas: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Write Cypress fixture with BAS account mapping."""
    bas_payload = dict(bas or {})
    g1_accounts = _g1_accounts_from_setup(company) if hasattr(frappe, "db") else []
    if g1_accounts:
        bas_payload["g1_accounts"] = g1_accounts
        if not bas_payload.get("sales_g1"):
            bas_payload["sales_g1"] = g1_accounts[0]
    purchase_template = ""
    if frappe.db.table_exists("Purchase Taxes and Charges Template"):
        for pattern in ("%Non Capital%GST%", "%Capital%GST%", "%GST%"):
            rows = frappe.get_all(
                "Purchase Taxes and Charges Template",
                filters={"company": company, "name": ("like", pattern)},
                fields=["name"],
                limit=1,
            )
            if rows:
                purchase_template = str(rows[0].get("name") or "")
                break
    if purchase_template:
        bas_payload["purchase_gst_template"] = purchase_template
    payload: Dict[str, Any] = {
        "company": company,
        "bas": bas_payload,
    }
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(payload))
    print(f"BAS fixture written: {payload} -> {fixture_path}")
    return payload
