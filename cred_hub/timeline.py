"""Linha do tempo e comentários da API do HubTech (credtech#288, soberania v1)."""

import frappe
from frappe import _

COMMENTABLE_DOCTYPES = frozenset({"CRM Deal", "CRM Organization", "Contact", "CRM Task", "FCRM Note"})
MAX_COMMENT_LENGTH = 50_000


def _check_parent_read_scope(reference_doctype: str):
	if reference_doctype not in COMMENTABLE_DOCTYPES:
		frappe.throw(_("Comments are not supported for {0}.").format(reference_doctype), frappe.PermissionError)
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if key_info:
		from cred_hub.api_keys import RESOURCES, _scopes

		scopes = _scopes(key_info["name"])
		read_scope, _write = RESOURCES.get(reference_doctype, (None, None))
		# only records the API exposes, and only with their read scope (audit L5)
		if not read_scope or read_scope not in scopes:
			frappe.throw(_("This API key is not allowed to read {0}.").format(reference_doctype), frappe.PermissionError)


@frappe.whitelist()
def get_comments(reference_doctype: str, reference_name: str, limit: int = 50) -> list[dict]:
	"""Retorna os comentários da timeline de um registro após verificar a permissão de leitura no registro pai."""
	_check_parent_read_scope(reference_doctype)
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


@frappe.whitelist(methods=["POST"])
def add_comment(reference_doctype: str, reference_name: str, content: str) -> dict:
	"""Adiciona um comentário auditável à timeline de um Card, Organização, Contato, Tarefa ou Nota."""
	if reference_doctype not in COMMENTABLE_DOCTYPES:
		frappe.throw(_("Comments are not supported for {0}.").format(reference_doctype), frappe.PermissionError)
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if key_info:
		from cred_hub.api_keys import RESOURCES, _scopes

		scopes = _scopes(key_info["name"])
		if "comments_write" not in scopes:
			frappe.throw(_("This API key is not allowed to add comments."), frappe.PermissionError)
		read_scope, write_scope = RESOURCES.get(reference_doctype, (None, None))
		if not ((read_scope and read_scope in scopes) or (write_scope and write_scope in scopes)):
			frappe.throw(_("This API key is not allowed to access {0}.").format(reference_doctype), frappe.PermissionError)

	text = (content or "").strip()
	if not text:
		frappe.throw(_("Comment content cannot be empty."), frappe.ValidationError)
	if len(text) > MAX_COMMENT_LENGTH:
		frappe.throw(_("Comment content exceeds the maximum length of {0} characters.").format(MAX_COMMENT_LENGTH), frappe.ValidationError)

	doc = frappe.get_doc(reference_doctype, reference_name)
	doc.check_permission("read")
	comment = doc.add_comment("Comment", text=text)
	return {
		"name": comment.name,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"content": comment.content,
		"comment_by": comment.comment_by,
		"creation": str(comment.creation),
	}
