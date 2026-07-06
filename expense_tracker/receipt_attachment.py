"""Purchase Invoice receipt URL — field on PI + linked File attachments."""

from __future__ import annotations

import frappe

RECEIPT_IMAGE_FIELD = "receipt_image"
PURCHASE_INVOICE_DOCTYPE = "Purchase Invoice"


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
