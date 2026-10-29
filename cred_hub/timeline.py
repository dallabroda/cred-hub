"""Linha do tempo e comentários da API do HubTech (credtech#288)."""

import frappe
from frappe import _


@frappe.whitelist()
def get_comments(reference_doctype: str, reference_name: str, limit: int = 50) -> list[dict]:
	"""Retorna os comentários da timeline de um registro após verificar a permissão de leitura no registro pai."""
	# Confere permissão de leitura no documento referenciado
	frappe.get_lazy_doc(reference_doctype, reference_name, check_permission="read")

	return frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"comment_type": "Comment",
		},
		fields=["name", "content", "comment_by", "comment_email", "creation", "modified"],
		order_by="creation desc",
		limit=min(int(limit or 50), 100),
	)
