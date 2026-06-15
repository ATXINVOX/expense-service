"""
Run inside central-site / dev-central-site container (bench Python):

  podman exec central-site /home/frappe/frappe-bench/env/bin/python /tmp/integration_bootstrap.py

Seeds Administrator tenant_id, default company, and test company for Cypress.
"""

from __future__ import annotations

import pathlib
import sys

import frappe

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cypress_bas_bootstrap import ensure_au_simpler_bas_report_setup, write_bas_fixture

BENCH_SITES = "/home/frappe/frappe-bench/sites"
TEST_COMPANY = "_Test Expense Integ Co"
TEST_ABBR = "TEIC"
TEST_TENANT_ID = "expense-integ-tenant-001"


def main() -> None:
    frappe.init(site="dev.localhost", sites_path=BENCH_SITES)
    frappe.connect()
    frappe.set_user("Administrator")

    if not frappe.db.exists("Company", TEST_COMPANY):
        company = frappe.get_doc(
            {
                "doctype": "Company",
                "company_name": TEST_COMPANY,
                "abbr": TEST_ABBR,
                "default_currency": "AUD",
                "country": "Australia",
                "create_chart_of_accounts_based_on": "Standard Template",
                "chart_of_accounts": "Standard",
            }
        )
        company.insert(ignore_permissions=True)
        frappe.db.commit()
        print(f"Created company: {TEST_COMPANY}")
    else:
        print(f"Company exists: {TEST_COMPANY}")

    default_currency = frappe.db.get_value("Company", TEST_COMPANY, "default_currency") or "AUD"
    frappe.db.sql(
        """
        UPDATE `tabAccount`
        SET account_currency = %s
        WHERE company = %s AND (account_currency IS NULL OR account_currency = '')
        """,
        (default_currency, TEST_COMPANY),
    )
    for field, atype in (
        ("default_payable_account", "Payable"),
        ("default_expense_account", "Expense Account"),
    ):
        acc = frappe.db.get_value(
            "Account", {"company": TEST_COMPANY, "account_type": atype, "is_group": 0}, "name"
        )
        if acc:
            frappe.db.set_value("Company", TEST_COMPANY, field, acc)
    cc = frappe.db.get_value("Cost Center", {"company": TEST_COMPANY, "is_group": 0}, "name")
    if cc:
        frappe.db.set_value("Company", TEST_COMPANY, "cost_center", cc)

    abbr = frappe.db.get_value("Company", TEST_COMPANY, "abbr") or TEST_ABBR
    expense_root = f"Expenses - {abbr}"
    if not frappe.db.exists("Account", expense_root):
        frappe.get_doc(
            {
                "doctype": "Account",
                "account_name": "Expenses",
                "company": TEST_COMPANY,
                "root_type": "Expense",
                "report_type": "Profit and Loss",
                "is_group": 1,
                "account_currency": default_currency,
            }
        ).insert(ignore_permissions=True, ignore_mandatory=True)
    general_expense = f"General Expenses - {abbr}"
    if not frappe.db.exists("Account", general_expense):
        parent = expense_root if frappe.db.exists("Account", expense_root) else None
        if parent:
            frappe.get_doc(
                {
                    "doctype": "Account",
                    "account_name": "General Expenses",
                    "company": TEST_COMPANY,
                    "root_type": "Expense",
                    "report_type": "Profit and Loss",
                    "parent_account": parent,
                    "account_type": "Expense Account",
                    "is_group": 0,
                    "account_currency": default_currency,
                }
            ).insert(ignore_permissions=True, ignore_mandatory=True)
    expense_leaf = frappe.db.get_value(
        "Account",
        {"company": TEST_COMPANY, "account_type": "Expense Account", "is_group": 0},
        "name",
    )
    if expense_leaf:
        frappe.db.set_value("Company", TEST_COMPANY, "default_expense_account", expense_leaf)

    frappe.db.commit()

    frappe.db.sql(
        "UPDATE `tabUser` SET tenant_id = %s WHERE name = 'Administrator'",
        (TEST_TENANT_ID,),
    )
    existing = frappe.db.get_value(
        "DefaultValue", {"parent": "Administrator", "defkey": "company"}, "name"
    )
    if existing:
        frappe.db.set_value("DefaultValue", existing, "defvalue", TEST_COMPANY)
    else:
        frappe.get_doc(
            {
                "doctype": "DefaultValue",
                "parent": "Administrator",
                "parenttype": "User",
                "parentfield": "defaults",
                "defkey": "company",
                "defvalue": TEST_COMPANY,
            }
        ).insert(ignore_permissions=True)

    frappe.db.sql(
        "UPDATE `tabCompany` SET tenant_id = %s WHERE name = %s",
        (TEST_TENANT_ID, TEST_COMPANY),
    )
    frappe.db.sql(
        "UPDATE `tabAccount` SET tenant_id = %s WHERE company = %s",
        (TEST_TENANT_ID, TEST_COMPANY),
    )
    frappe.db.sql(
        "UPDATE `tabCost Center` SET tenant_id = %s WHERE company = %s",
        (TEST_TENANT_ID, TEST_COMPANY),
    )

    bas = ensure_au_simpler_bas_report_setup(TEST_COMPANY, TEST_ABBR)
    fixture_path = pathlib.Path(__file__).resolve().parent.parent / "cypress/fixtures/resolved_pi_bas_accounts.json"
    container_fixture = pathlib.Path("/mnt/expense/cypress/fixtures/resolved_pi_bas_accounts.json")
    if container_fixture.parent.is_dir():
        fixture_path = container_fixture
    write_bas_fixture(fixture_path, company=TEST_COMPANY, bas=bas)

    frappe.db.commit()
    print(f"Administrator.tenant_id = {TEST_TENANT_ID}")
    print(f"Administrator default company = {TEST_COMPANY}")
    frappe.destroy()


if __name__ == "__main__":
    main()
