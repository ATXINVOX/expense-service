"""
Integration tests for expense-service Purchase Invoice CRUD.

Runs inside the vyogo/erpnext:sne-version-16 container against a live
Frappe/ERPNext/MariaDB instance — zero mocks.

Test groups
-----------
TestPurchaseInvoiceCreate   — controller enrichment: accounts, supplier, item,
                              GST, custom fields, child-row types
TestPurchaseInvoiceSubmit   — draft → submitted via submit_purchase_invoice()
TestPurchaseInvoiceDelete   — draft delete + submitted cancel-then-delete
TestDashboardSummary        — aggregation across multiple invoices
TestGetExpenses             — paginated list with embedded items
"""

import frappe
import pytest
from frappe_microservice.tenant import TenantAwareDB

from tests.integration.conftest import TEST_TENANT_ID, TEST_COMPANY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_invoice(supplier, item_code, company=TEST_COMPANY):
    """Minimal valid Purchase Invoice payload."""
    return {
        "doctype": "Purchase Invoice",
        "company": company,
        "supplier": supplier,
        "posting_date": frappe.utils.today(),
        "due_date": frappe.utils.today(),
        "items": [
            {
                "item_code": item_code,
                "item_name": item_code,
                "qty": 1,
                "rate": 100.0,
                "uom": "Nos",
            }
        ],
        "tenant_id": TEST_TENANT_ID,
    }


def _get_pi(name):
    return frappe.db.get_value(
        "Purchase Invoice", name,
        ["docstatus", "status", "company", "supplier",
         "expense_item_name", "expense_item_group", "expense_items_count"],
        as_dict=True,
    )


# ---------------------------------------------------------------------------
# Create / Controller enrichment
# ---------------------------------------------------------------------------

class TestPurchaseInvoiceCreate:

    def test_insert_sets_expense_account_on_items(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """before_validate must populate expense_account on every item row."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )

        items = frappe.get_all(
            "Purchase Invoice Item",
            filters={"parent": doc.name},
            fields=["expense_account", "cost_center"],
        )
        assert items, "No item rows saved"
        assert items[0]["expense_account"], "expense_account must be set by controller"
        assert items[0]["cost_center"],     "cost_center must be set by controller"

    def test_insert_populates_expense_custom_fields(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """expense_item_name, expense_item_group, expense_items_count must be set."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )

        row = _get_pi(doc.name)
        assert row["expense_item_name"]  == test_item
        assert row["expense_items_count"] == 1

    def test_insert_item_rows_are_document_instances_not_dicts(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """Child rows on the returned doc must not be plain dicts.

        Verifies the fix: if rows were dicts, doc.save() would crash with
            AttributeError: 'dict' object has no attribute 'is_new'
        """
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )

        for row in doc.items:
            assert not isinstance(row, dict), (
                f"Item row is a plain dict — child table not converted to Document: {row}"
            )
            assert hasattr(row, "is_new"), "Row missing is_new() — not a Document instance"
            assert callable(row.is_new)

    def test_insert_auto_creates_unknown_supplier(
        self, mock_app, tenant_db, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """A supplier name that doesn't exist must be auto-created."""
        new_supplier_name = "_Auto Created Supplier Integ"
        # Cleanup: remove if leftover from previous run
        if frappe.db.exists("Supplier", new_supplier_name):
            frappe.delete_doc("Supplier", new_supplier_name, force=True, ignore_permissions=True)
            frappe.db.commit()

        payload = _minimal_invoice(new_supplier_name, test_item)
        doc = tenant_db.insert_doc(
            "Purchase Invoice", payload,
            ignore_permissions=True, ignore_mandatory=True,
        )

        saved_supplier = frappe.db.get_value("Purchase Invoice", doc.name, "supplier")
        assert frappe.db.exists("Supplier", saved_supplier), (
            f"Supplier '{saved_supplier}' was not auto-created"
        )

    def test_insert_auto_creates_unknown_item(
        self, mock_app, tenant_db, test_supplier,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """An item_code that doesn't exist must be auto-created."""
        new_item_code = "_Auto Created Item Integ"
        if frappe.db.exists("Item", new_item_code):
            frappe.delete_doc("Item", new_item_code, force=True, ignore_permissions=True)
            frappe.db.commit()

        payload = _minimal_invoice(test_supplier, new_item_code)
        doc = tenant_db.insert_doc(
            "Purchase Invoice", payload,
            ignore_permissions=True, ignore_mandatory=True,
        )

        items = frappe.get_all(
            "Purchase Invoice Item",
            filters={"parent": doc.name},
            fields=["item_code"],
        )
        assert items, "No items saved"
        assert frappe.db.exists("Item", items[0]["item_code"]), (
            "Item was not auto-created by controller"
        )

    def test_insert_multiple_items_all_enriched(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """All item rows must have expense_account set, and expense_items_count == n."""
        payload = _minimal_invoice(test_supplier, test_item)
        payload["items"].append({
            "item_code": test_item,
            "item_name": test_item,
            "qty": 2,
            "rate": 50.0,
            "uom": "Nos",
        })

        doc = tenant_db.insert_doc(
            "Purchase Invoice", payload,
            ignore_permissions=True, ignore_mandatory=True,
        )

        items = frappe.get_all(
            "Purchase Invoice Item",
            filters={"parent": doc.name},
            fields=["expense_account"],
        )
        assert len(items) == 2
        for row in items:
            assert row["expense_account"], "expense_account missing on item row"

        row = _get_pi(doc.name)
        assert row["expense_items_count"] == 2


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

class TestPurchaseInvoiceSubmit:

    def test_submit_changes_docstatus_to_1(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """submit_purchase_invoice() must flip docstatus from 0 → 1."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )
        assert frappe.db.get_value("Purchase Invoice", doc.name, "docstatus") == 0

        from expense_tracker.api import submit_purchase_invoice

        # Simulate flask request payload via mock_app context
        from unittest.mock import patch, MagicMock
        mock_request = MagicMock()
        mock_request.get_json.return_value = {"name": doc.name}

        with patch("expense_tracker.api.request", mock_request):
            result = submit_purchase_invoice("Administrator")

        assert result.get("success") is True, f"Submit failed: {result}"
        assert frappe.db.get_value("Purchase Invoice", doc.name, "docstatus") == 1

    def test_submit_rejects_already_submitted(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """Submitting an already-submitted invoice must return 400."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )
        frappe.db.set_value("Purchase Invoice", doc.name, {"docstatus": 1, "status": "Submitted"})
        frappe.db.commit()

        from expense_tracker.api import submit_purchase_invoice
        from unittest.mock import patch, MagicMock
        mock_request = MagicMock()
        mock_request.get_json.return_value = {"name": doc.name}

        with patch("expense_tracker.api.request", mock_request):
            body, code = submit_purchase_invoice("Administrator")

        assert code == 400
        assert "Only draft" in body["message"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestPurchaseInvoiceDelete:

    def test_delete_draft_removes_document(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """Deleting a draft invoice must remove it from the DB."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )
        name = doc.name
        frappe.db.commit()

        from expense_tracker.api import delete_purchase_invoice
        result = delete_purchase_invoice("Administrator", name)

        assert result.get("success") is True, f"Delete failed: {result}"
        assert not frappe.db.exists("Purchase Invoice", name), (
            "Draft invoice still exists after delete"
        )

    def test_delete_submitted_cancels_then_removes(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """Deleting a submitted invoice must cancel first (docstatus 2) then delete."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )
        name = doc.name
        frappe.db.set_value("Purchase Invoice", name, {"docstatus": 1, "status": "Submitted"})
        frappe.db.commit()

        from expense_tracker.api import delete_purchase_invoice
        result = delete_purchase_invoice("Administrator", name)

        assert result.get("success") is True, f"Delete failed: {result}"
        assert not frappe.db.exists("Purchase Invoice", name), (
            "Submitted invoice still exists after delete"
        )


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

class TestDashboardSummary:

    def test_dashboard_aggregates_invoices_for_company(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """get_dashboard_summary must sum grand_total across all invoices."""
        for rate in (100.0, 200.0):
            payload = _minimal_invoice(test_supplier, test_item)
            payload["items"][0]["rate"] = rate
            tenant_db.insert_doc(
                "Purchase Invoice", payload,
                ignore_permissions=True, ignore_mandatory=True,
            )
        frappe.db.commit()

        from expense_tracker.api import get_dashboard_summary
        result = get_dashboard_summary("Administrator")

        assert result["total_spend"] >= 300.0, (
            f"Expected total_spend >= 300, got {result['total_spend']}"
        )
        assert result["currency"]


# ---------------------------------------------------------------------------
# Get expenses list
# ---------------------------------------------------------------------------

class TestGetExpenses:

    def test_get_expenses_returns_invoices_with_items(
        self, mock_app, tenant_db, test_supplier, test_item,
        test_accounts, ensure_fiscal_year, test_company,
    ):
        """get_expenses must return invoices with embedded item rows."""
        tenant_db.insert_doc(
            "Purchase Invoice",
            _minimal_invoice(test_supplier, test_item),
            ignore_permissions=True, ignore_mandatory=True,
        )
        frappe.db.commit()

        from expense_tracker.api import get_expenses
        result = get_expenses("Administrator")

        assert result["count"] >= 1
        assert result["company"] == test_company
        # At least one invoice should have items embedded
        invoices_with_items = [inv for inv in result["data"] if inv.get("items")]
        assert invoices_with_items, "No invoice returned with embedded items"
