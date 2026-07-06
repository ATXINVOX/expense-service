"""Tests for expense receipt URL resolution and persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mock_frappe = MagicMock()
sys.modules.setdefault("frappe", mock_frappe)

import expense_tracker.receipt_attachment as receipt_attachment  # noqa: E402
from expense_tracker.receipt_attachment import (  # noqa: E402
    resolve_receipt_image_url,
    set_purchase_invoice_receipt_image,
)


@pytest.fixture(autouse=True)
def _reset_frappe():
    db = MagicMock()
    db.get_value.return_value = None
    db.exists.return_value = True
    db.set_value = MagicMock()
    receipt_attachment.frappe.db = db
    receipt_attachment.frappe.get_all = MagicMock(return_value=[])
    yield db


def test_resolve_prefers_doc_dict_field():
    url = resolve_receipt_image_url(
        "ACC-PINV-1",
        {"receipt_image": "http://kong/api/method/download_file?storage_key=abc"},
    )
    assert url == "http://kong/api/method/download_file?storage_key=abc"
    receipt_attachment.frappe.get_all.assert_not_called()


def test_resolve_falls_back_to_db_field(_reset_frappe):
    _reset_frappe.get_value.return_value = "http://example/receipt.webp"
    url = resolve_receipt_image_url("ACC-PINV-1", {})
    assert url == "http://example/receipt.webp"


def test_resolve_falls_back_to_latest_file(_reset_frappe):
    receipt_attachment.frappe.get_all.return_value = [
        {"file_url": "http://kong/api/method/download_file?storage_key=file-1"},
    ]
    url = resolve_receipt_image_url("ACC-PINV-1", {})
    assert url == "http://kong/api/method/download_file?storage_key=file-1"
    receipt_attachment.frappe.get_all.assert_called_once()


def test_set_receipt_image_updates_when_changed(_reset_frappe):
    _reset_frappe.get_value.return_value = None
    changed = set_purchase_invoice_receipt_image(
        "ACC-PINV-1",
        "http://kong/api/method/download_file?storage_key=new",
    )
    assert changed is True
    _reset_frappe.set_value.assert_called_once_with(
        "Purchase Invoice",
        "ACC-PINV-1",
        "receipt_image",
        "http://kong/api/method/download_file?storage_key=new",
        update_modified=True,
    )


def test_set_receipt_image_skips_when_unchanged(_reset_frappe):
    _reset_frappe.get_value.return_value = "http://same/url"
    changed = set_purchase_invoice_receipt_image("ACC-PINV-1", "http://same/url")
    assert changed is False
    _reset_frappe.set_value.assert_not_called()
