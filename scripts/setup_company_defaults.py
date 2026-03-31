"""
Setup default accounts on Company 'thiru varasu' for Purchase Invoice support.
Run inside the central-site container via bench:
  bench --site dev.localhost execute expense_tracker_setup.setup_company_defaults
Or pipe directly:
  bench --site dev.localhost execute frappe.utils.execute_in_shell ...
"""
import frappe

COMPANY = "thiru varasu"
ABBR = None  # auto-detect from existing accounts


def get_abbr():
    """Get the company abbreviation from an existing account."""
    existing = frappe.db.get_value("Account", {"company": COMPANY}, "name")
    if existing and " - " in existing:
        return existing.rsplit(" - ", 1)[1]
    return frappe.db.get_value("Company", COMPANY, "abbr") or "TV"


def ensure_account(name_without_abbr, account_type, root_type, parent_name_without_abbr, is_group=0):
    abbr = get_abbr()
    full_name = f"{name_without_abbr} - {abbr}"
    parent_full = f"{parent_name_without_abbr} - {abbr}" if parent_name_without_abbr else None

    if frappe.db.exists("Account", full_name):
        print(f"  Account already exists: {full_name}")
        return full_name

    doc = frappe.get_doc({
        "doctype": "Account",
        "account_name": name_without_abbr,
        "company": COMPANY,
        "parent_account": parent_full,
        "root_type": root_type,
        "account_type": account_type,
        "is_group": is_group,
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    print(f"  Created account: {full_name}")
    return full_name


def run():
    abbr = get_abbr()
    print(f"Company: {COMPANY}, Abbreviation: {abbr}")

    # Ensure parent groups exist
    ensure_account("Current Liabilities", "", "Liability", "Liabilities", is_group=1)
    ensure_account("Expenses", "", "Expense", None, is_group=1)
    ensure_account("Direct Expenses", "", "Expense", "Expenses", is_group=1)
    ensure_account("Equity", "", "Equity", None, is_group=1)

    # Required accounts
    stock_received = ensure_account("Stock Received But Not Billed", "Stock Received But Not Billed", "Liability", "Current Liabilities")
    stock_adjustment = ensure_account("Stock Adjustment", "Stock Adjustment", "Expense", "Direct Expenses")
    default_expense = ensure_account("Cost of Goods Sold", "Cost of Goods Sold", "Expense", "Direct Expenses")
    expense_account = ensure_account("Miscellaneous Expenses", "Expense Account", "Expense", "Direct Expenses")
    round_off = ensure_account("Rounded Off", "Round Off", "Expense", "Direct Expenses")
    write_off = ensure_account("Write Off", "", "Expense", "Direct Expenses")
    exchange_gain_loss = ensure_account("Exchange Gain/Loss", "", "Expense", "Direct Expenses")
    default_payable = f"Accounts Payable - {abbr}"
    default_receivable = f"Accounts Receivable - {abbr}"
    retained_earnings = ensure_account("Retained Earnings", "", "Equity", "Equity")

    # Set defaults on the Company
    company_doc = frappe.get_doc("Company", COMPANY)
    company_doc.default_receivable_account = default_receivable
    company_doc.default_payable_account = default_payable
    company_doc.stock_received_but_not_billed = stock_received
    company_doc.stock_adjustment_account = stock_adjustment
    company_doc.default_expense_account = expense_account
    company_doc.cost_center = ""  # will set below if needed
    company_doc.round_off_account = round_off
    company_doc.write_off_account = write_off
    company_doc.exchange_gain_loss_account = exchange_gain_loss
    company_doc.accumulated_depreciation_account = ""
    company_doc.depreciation_expense_account = ""
    company_doc.default_income_account = ""

    company_doc.flags.ignore_permissions = True
    company_doc.flags.ignore_mandatory = True
    company_doc.save()

    frappe.db.commit()
    print(f"\nCompany '{COMPANY}' default accounts updated successfully.")


if __name__ == "__main__":
    run()
