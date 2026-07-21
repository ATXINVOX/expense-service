"""Purchase Invoice receipt URL — field on PI + linked File attachments."""

from __future__ import annotations

import logging
import os
import urllib.parse

import frappe

logger = logging.getLogger(__name__)

RECEIPT_IMAGE_FIELD = "receipt_image"
PURCHASE_INVOICE_DOCTYPE = "Purchase Invoice"
STORAGE_KEY_PARAM = "storage_key"


def resolve_receipt_image_url(invoice_name: str, doc_dict: dict | None = None) -> str | None:
	"""Return receipt proxy URL from PI field, else latest linked File row."""
	if not invoice_name:
		return None

	doc_dict = doc_dict or {}
	field_value = (doc_dict.get(RECEIPT_IMAGE_FIELD) or "").strip()
	if field_value:
		return field_value

	if RECEIPT_IMAGE_FIELD not in doc_dict:
		try:
			db_value = frappe.db.get_value(
				PURCHASE_INVOICE_DOCTYPE,
				invoice_name,
				RECEIPT_IMAGE_FIELD,
			)
			if db_value and str(db_value).strip():
				return str(db_value).strip()
		except Exception:
			pass

	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": PURCHASE_INVOICE_DOCTYPE,
			"attached_to_name": invoice_name,
		},
		fields=["file_url"],
		order_by="creation desc",
		limit=1,
	)
	if files:
		url = (files[0].get("file_url") or "").strip()
		return url or None
	return None


def set_purchase_invoice_receipt_image(invoice_name: str, file_url: str) -> bool:
	"""Persist receipt URL on Purchase Invoice (idempotent)."""
	url = (file_url or "").strip()
	if not invoice_name or not url:
		return False
	if not frappe.db.exists(PURCHASE_INVOICE_DOCTYPE, invoice_name):
		return False

	current = frappe.db.get_value(
		PURCHASE_INVOICE_DOCTYPE,
		invoice_name,
		RECEIPT_IMAGE_FIELD,
	)
	if (current or "").strip() == url:
		return False

	frappe.db.set_value(
		PURCHASE_INVOICE_DOCTYPE,
		invoice_name,
		RECEIPT_IMAGE_FIELD,
		url,
		update_modified=True,
	)
	return True


def extract_storage_key(file_url: str | None) -> str | None:
	"""Pull the storage_key from a proxy download_file URL (``?storage_key=...``)."""
	if not file_url:
		return None
	try:
		query = urllib.parse.urlparse(file_url).query
		values = urllib.parse.parse_qs(query).get(STORAGE_KEY_PARAM)
		if values:
			key = urllib.parse.unquote(values[0]).strip()
			return key or None
	except Exception:
		return None
	return None


def _receipt_url_referenced_elsewhere(file_url: str) -> bool:
	"""True if any Purchase Invoice still points ``receipt_image`` at this URL.

	Called after the owning invoice is deleted or repointed, so a match here means the
	file is shared and must be kept.
	"""
	try:
		rows = frappe.get_all(
			PURCHASE_INVOICE_DOCTYPE,
			filters={RECEIPT_IMAGE_FIELD: file_url},
			fields=["name"],
			limit=1,
		)
		return bool(rows)
	except Exception:
		# If the check cannot run, keep the file (safer than deleting a shared object).
		return True


def _current_session_auth() -> tuple[dict, dict]:
	"""Best-effort forward of the caller's session to the internal delete endpoint."""
	headers: dict = {}
	cookies: dict = {}
	try:
		from flask import has_request_context, request

		if has_request_context():
			auth = request.headers.get("Authorization")
			if auth:
				headers["Authorization"] = auth
			sid = request.cookies.get("sid")
			if sid:
				cookies["sid"] = sid
	except Exception:
		pass
	return headers, cookies


def _delete_via_file_metadata_service(service_url: str, storage_key: str, file_url: str) -> None:
	import requests

	headers, cookies = _current_session_auth()
	response = requests.delete(
		f"{service_url.rstrip('/')}/api/method/delete_file",
		json={"storage_key": storage_key, "file_url": file_url},
		headers=headers,
		cookies=cookies,
		timeout=10,
	)
	# 200/204 deleted, 404 already gone — all fine (deletion is idempotent).
	if response.status_code not in (200, 204, 404):
		logger.warning(
			"delete_file service returned %s for key=%s", response.status_code, storage_key
		)


def _delete_frappe_file(file_url: str) -> None:
	"""Native fallback (no storage service configured): delete the File doc for this URL."""
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if name:
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)


def delete_receipt_file(file_url: str | None) -> None:
	"""Best-effort delete of a receipt's stored file once no invoice references it.

	Never raises: a failed cleanup must not fail the expense delete/update.
	"""
	try:
		url = (file_url or "").strip()
		if not url:
			return
		storage_key = extract_storage_key(url)
		if not storage_key:
			return
		if _receipt_url_referenced_elsewhere(url):
			return
		service_url = (os.environ.get("FILE_METADATA_SERVICE_URL") or "").strip()
		if service_url:
			_delete_via_file_metadata_service(service_url, storage_key, url)
		else:
			_delete_frappe_file(url)
	except Exception as exc:
		logger.warning("delete_receipt_file failed for %s: %s", file_url, exc)
