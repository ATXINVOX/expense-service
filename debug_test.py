"""Temporary debug script — delete after use."""
import frappe
import frappe.utils as fu
from unittest.mock import MagicMock, patch
from frappe_microservice.tenant import TenantAwareDB

frappe.init(site="dev.localhost", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")

# Ensure the DefaultValue exists
if not frappe.db.get_value("DefaultValue", {"parent": "Administrator", "defkey": "company"}, "name"):
    frappe.get_doc({
        "doctype": "DefaultValue",
        "parent": "Administrator",
        "parenttype": "User",
        "parentfield": "defaults",
        "defkey": "company",
        "defvalue": "_Test Expense Integ Co",
    }).insert(ignore_permissions=True)
    frappe.db.commit()

tenant_db = TenantAwareDB(lambda: "expense-integ-tenant-001")
app_mock = MagicMock()
app_mock.tenant_db = tenant_db

with patch("frappe_microservice.get_app", return_value=app_mock), \
     patch("expense_tracker.api.get_app", return_value=app_mock), \
     patch("controllers.purchase_invoice.get_app", return_value=app_mock):
    try:
        doc = tenant_db.insert_doc("Purchase Invoice", {
            "doctype": "Purchase Invoice",
            "company": "_Test Expense Integ Co",
            "supplier": "_Test Expense Supplier",
            "currency": "AUD",
            "conversion_rate": 1.0,
            "posting_date": fu.today(),
            "due_date": fu.today(),
            "items": [{"item_code": "_Test Expense Item", "item_name": "_Test Expense Item", "qty": 1, "rate": 100.0, "uom": "Nos"}],
            "tenant_id": "expense-integ-tenant-001",
        }, ignore_permissions=True, ignore_mandatory=True)
        print("name:", doc.name)
        print("type(doc):", type(doc).__name__)
        inner_doc = getattr(doc, "doc", None)
        if inner_doc:
            print("expense_item_name on .doc:", getattr(inner_doc, "expense_item_name", "NOT_FOUND"))
        else:
            print("expense_item_name on doc:", getattr(doc, "expense_item_name", "NOT_FOUND"))
        db_val = frappe.db.get_value("Purchase Invoice", doc.name, "expense_item_name")
        print("expense_item_name in DB:", repr(db_val))
        frappe.db.rollback()
    except Exception as e:
        import traceback; traceback.print_exc()

frappe.destroy()
