"""Tests for expense receipt URL resolution and persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mock_frappe = MagicMock()
sys.modules.setdefault("frappe", mock_frappe)

import expense_tracker.receipt_attachment as receipt_attachment  # noqa: E402
from expense_tracker.receipt_attachment import (  # noqa: E402
    delete_receipt_file,
    extract_storage_key,
    resolve_receipt_image_url,
    set_purchase_invoice_receipt_image,
)

PROXY_URL = "http://kong:8000/api/method/download_file?storage_key=tenants%2Ftnt1%2Freceipt.jpg"


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


def test_extract_storage_key_from_proxy_url():
    assert extract_storage_key(PROXY_URL) == "tenants/tnt1/receipt.jpg"


def test_extract_storage_key_missing_param():
    assert extract_storage_key("http://kong:8000/api/method/download_file") is None


def test_extract_storage_key_handles_empty():
    assert extract_storage_key(None) is None
    assert extract_storage_key("") is None


def test_delete_receipt_file_noop_without_storage_key(_reset_frappe):
    with patch("requests.delete") as req:
        delete_receipt_file("http://kong:8000/files/no-key.jpg")
    req.assert_not_called()


def test_delete_receipt_file_skips_when_referenced_elsewhere(_reset_frappe, monkeypatch):
    receipt_attachment.frappe.get_all = MagicMock(return_value=[{"name": "OTHER-PI"}])
    monkeypatch.setenv("FILE_METADATA_SERVICE_URL", "http://file-metadata:8000")
    with patch("requests.delete") as req:
        delete_receipt_file(PROXY_URL)
    req.assert_not_called()


def test_delete_receipt_file_calls_service_with_storage_key(_reset_frappe, monkeypatch):
    receipt_attachment.frappe.get_all = MagicMock(return_value=[])
    monkeypatch.setenv("FILE_METADATA_SERVICE_URL", "http://file-metadata:8000")
    response = MagicMock()
    response.status_code = 200
    with patch("requests.delete", return_value=response) as req:
        delete_receipt_file(PROXY_URL)
    req.assert_called_once()
    args, kwargs = req.call_args
    assert args[0] == "http://file-metadata:8000/api/method/delete_file"
    assert kwargs["json"]["storage_key"] == "tenants/tnt1/receipt.jpg"


def test_delete_receipt_file_native_fallback_when_no_service(_reset_frappe, monkeypatch):
    monkeypatch.delenv("FILE_METADATA_SERVICE_URL", raising=False)
    receipt_attachment.frappe.get_all = MagicMock(return_value=[])
    receipt_attachment.frappe.db.get_value = MagicMock(return_value="FILE-1")
    receipt_attachment.frappe.delete_doc = MagicMock()
    delete_receipt_file(PROXY_URL)
    receipt_attachment.frappe.delete_doc.assert_called_once_with(
        "File", "FILE-1", ignore_permissions=True, force=True
    )


def test_delete_receipt_file_swallows_errors(_reset_frappe, monkeypatch):
    receipt_attachment.frappe.get_all = MagicMock(return_value=[])
    monkeypatch.setenv("FILE_METADATA_SERVICE_URL", "http://file-metadata:8000")
    with patch("requests.delete", side_effect=Exception("boom")):
        delete_receipt_file(PROXY_URL)  # must not raise
