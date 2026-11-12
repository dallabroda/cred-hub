"""Linha do tempo e comentários da API do HubTech (credtech#288)."""

import frappe
from frappe import _


@frappe.whitelist()
def get_comments(reference_doctype: str, reference_name: str, limit: int = 50) -> list[dict]:
	"""Retorna os comentários da timeline de um registro após verificar a permissão de leitura no registro pai."""
	# Confere permissão de leitura no documento referenciado
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if key_info:
		from cred_hub.api_keys import RESOURCES, _scopes

		scopes = _scopes(key_info["name"])
		read_scope, _write = RESOURCES.get(reference_doctype, (None, None))
		# only records the API exposes, and only with their read scope (audit L5)
		if not read_scope or read_scope not in scopes:
			frappe.throw(_("This API key is not allowed to read {0}.").format(reference_doctype), frappe.PermissionError)

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
		limit=max(1, min(frappe.utils.cint(limit) or 50, 100)),
	)
