"""
Shared test doubles for expense-service unit tests.

FakeChildDocument / FakeDocumentController model the real Frappe
BaseDocument behaviour that MagicMock silently hides:

* FakeDocumentController.append(key, value) converts child dicts to
  FakeChildDocument instances — exactly what BaseDocument._init_child does.
* FakeDocumentController.save() calls _set_defaults() which calls
  is_new() on every child row.  Plain dicts do NOT have is_new(), so any
  test that leaves items/taxes as dicts will fail with:
      AttributeError: 'dict' object has no attribute 'is_new'
  — the same crash that hit production.

These classes are imported by test_document_model.py.  The existing
test_purchase_invoice.py keeps its own MockDocumentController so existing
tests are untouched.
"""

# Child table field names for Purchase Invoice
PI_TABLE_FIELDS = ("items", "taxes")


class FakeChildDocument:
    """Mimics a Frappe child Document row.

    Supports both attribute access (row.item_code) and dict-like access
    (row["item_code"]) so it drops in wherever existing assertions use either
    style.
    """

    def __init__(self, data: dict):
        for k, v in data.items():
            object.__setattr__(self, k, v)
        object.__setattr__(self, "_is_new", True)

    def is_new(self) -> bool:
        return self._is_new

    # --- dict-like interface so existing assertions (row["field"]) still work ---
    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        object.__setattr__(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, key):
        return hasattr(self, key) and not key.startswith("_")


class FakeDocumentController:
    """Lightweight stand-in for DocumentController + PurchaseInvoice.

    Models the critical behaviour that MagicMock hides:

    - set(key, value)   — converts child-table lists to FakeChildDocument
    - append(key, value) — converts appended dicts to FakeChildDocument
    - save()            — calls _set_defaults() → is_new() on every child row
                          (crashes if any row is still a plain dict)
    """

    def __init__(self, data: dict, table_fieldnames: tuple = PI_TABLE_FIELDS):
        self.flags = type("Flags", (), {})()
        object.__setattr__(self, "_table_fieldnames", table_fieldnames)
        object.__setattr__(self, "_saved", False)

        for key, value in data.items():
            if key in table_fieldnames and isinstance(value, list):
                object.__setattr__(
                    self, key,
                    [FakeChildDocument(v) if isinstance(v, dict) else v for v in value]
                )
            else:
                object.__setattr__(self, key, value)

        # DocumentController pattern: self.doc = self
        self.doc = self

    def set(self, key, value):
        if key in self._table_fieldnames and isinstance(value, list):
            value = [
                FakeChildDocument(v) if isinstance(v, dict) else v for v in value
            ]
        object.__setattr__(self, key, value)

    def append(self, key, value):
        if not hasattr(self, key):
            object.__setattr__(self, key, [])
        if isinstance(value, dict) and key in self._table_fieldnames:
            value = FakeChildDocument(value)
        getattr(self, key).append(value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def save(self):
        """Simulate Frappe Document.save() → _set_defaults() → is_new() on children."""
        self._set_defaults()
        object.__setattr__(self, "_saved", True)

    def _set_defaults(self):
        for fieldname in self._table_fieldnames:
            children = getattr(self, fieldname, None)
            if children:
                for row in children:
                    row.is_new()  # AttributeError if row is a plain dict
