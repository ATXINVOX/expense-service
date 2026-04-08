"""
Document-model unit tests for FakeChildDocument / FakeDocumentController.

These tests do NOT import any production controller code — they verify that
the test doubles in tests/conftest.py faithfully reproduce the Frappe
BaseDocument behaviour that MagicMock silently hides.

Why this matters
----------------
The existing test_purchase_invoice.py uses MockDocumentController, whose
append() stores raw dicts.  Those dicts pass every MagicMock assertion, but
in production Frappe's _set_defaults() calls is_new() on every child row and
crashes with:
    AttributeError: 'dict' object has no attribute 'is_new'

The FakeDocumentController in conftest.py converts child dicts to
FakeChildDocument instances (as Frappe's BaseDocument._init_child does), and
its save() reproduces the crash when plain dicts are left behind.

These tests give pytest something to catch that crash BEFORE the container.
"""

import pytest
from tests.conftest import FakeChildDocument, FakeDocumentController


class TestFakeChildDocument:

    def test_is_new_returns_true(self):
        row = FakeChildDocument({"item_code": "Fuel", "qty": 1})
        assert row.is_new() is True

    def test_attribute_access(self):
        row = FakeChildDocument({"item_code": "Fuel", "rate": 100.0})
        assert row.item_code == "Fuel"
        assert row.rate == 100.0

    def test_dict_style_access(self):
        row = FakeChildDocument({"item_code": "Fuel", "expense_account": "10000 - F"})
        assert row["item_code"] == "Fuel"
        assert row["expense_account"] == "10000 - F"

    def test_dict_style_set(self):
        row = FakeChildDocument({"item_code": "X"})
        row["cost_center"] = "Main - CC"
        assert row.cost_center == "Main - CC"

    def test_get_with_default(self):
        row = FakeChildDocument({"item_code": "X"})
        assert row.get("missing_field", "default") == "default"

    def test_contains(self):
        row = FakeChildDocument({"item_code": "X"})
        assert "item_code" in row
        assert "nonexistent" not in row


class TestFakeDocumentControllerChildConversion:
    """set() and append() must convert dicts to FakeChildDocument."""

    def test_init_converts_child_list(self):
        doc = FakeDocumentController({
            "doctype": "Purchase Invoice",
            "items": [{"item_code": "Fuel", "qty": 1}],
            "taxes": [],
        })
        assert isinstance(doc.items[0], FakeChildDocument)

    def test_set_converts_child_list(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        doc.set("items", [{"item_code": "Paper", "qty": 2}])
        assert isinstance(doc.items[0], FakeChildDocument)
        assert doc.items[0]["item_code"] == "Paper"

    def test_append_converts_dict_to_child_document(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        doc.append("items", {"item_code": "Pen", "rate": 5.0})
        assert isinstance(doc.items[0], FakeChildDocument)
        assert doc.items[0].item_code == "Pen"

    def test_append_non_table_field_stores_raw(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        doc.append("remarks_list", {"text": "note"})  # not a table field
        assert isinstance(doc.remarks_list[0], dict)

    def test_set_scalar_field_unchanged(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        doc.set("company", "Acme Pty Ltd")
        assert doc.company == "Acme Pty Ltd"

    def test_multiple_appends_all_converted(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        for item in [{"item_code": "A"}, {"item_code": "B"}, {"item_code": "C"}]:
            doc.append("items", item)
        assert len(doc.items) == 3
        for row in doc.items:
            assert isinstance(row, FakeChildDocument)


class TestFakeDocumentControllerSave:
    """save() must succeed with FakeChildDocument rows and crash on plain dicts."""

    def test_save_succeeds_when_all_rows_are_child_documents(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        doc.append("items", {"item_code": "Fuel", "rate": 100.0})
        doc.append("taxes", {"charge_type": "On Net Total", "rate": 10})

        doc.save()  # must not raise

        assert doc._saved is True

    def test_save_crashes_when_raw_dict_bypasses_append(self):
        """Regression proof: plain dicts in child tables crash save() just like production."""
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})

        # Simulate broken code that bypasses set()/append() — stores raw dict directly
        doc.items.append({"item_code": "X", "qty": 1})

        assert isinstance(doc.items[0], dict)

        with pytest.raises(AttributeError, match="is_new"):
            doc.save()

    def test_save_crashes_on_direct_setattr_of_raw_list(self):
        """setattr with a list of dicts (old setattr-based update_doc) also crashes."""
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})

        # Simulate old broken update_doc: setattr instead of doc.update()/set()
        object.__setattr__(doc, "items", [{"item_code": "Y", "rate": 50.0}])

        with pytest.raises(AttributeError, match="is_new"):
            doc.save()

    def test_save_with_empty_child_tables_succeeds(self):
        doc = FakeDocumentController({
            "doctype": "Purchase Invoice",
            "items": [],
            "taxes": [],
        })
        doc.save()
        assert doc._saved is True

    def test_save_marks_document_as_saved(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        assert doc._saved is False
        doc.save()
        assert doc._saved is True


class TestFakeDocumentControllerDocCompatibility:
    """self.doc = self pattern used by DocumentController."""

    def test_doc_is_self(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        assert doc.doc is doc

    def test_get_method(self):
        doc = FakeDocumentController({
            "doctype": "Purchase Invoice",
            "company": "Acme Pty Ltd",
            "items": [],
        })
        assert doc.get("company") == "Acme Pty Ltd"
        assert doc.get("nonexistent", "fallback") == "fallback"

    def test_flags_object_exists(self):
        doc = FakeDocumentController({"doctype": "Purchase Invoice", "items": []})
        assert hasattr(doc, "flags")
        doc.flags.expense_pi_enriched = True
        assert doc.flags.expense_pi_enriched is True
