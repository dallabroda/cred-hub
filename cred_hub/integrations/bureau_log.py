"""Histórico das consultas pagas e das operações no cartório (Cred Hub Bureau Query).

Serve de auditoria LGPD (quem consultou, quando, com qual finalidade) e de cache: a mesma consulta dentro do prazo
das configurações devolve o resultado salvo, sem nova cobrança. Segredos nunca entram aqui.
"""

import json

import frappe
from frappe.utils import add_days, now_datetime

QUERY = "Cred Hub Bureau Query"
SUCCESS, NOT_FOUND, ERROR = "Success", "Not Found", "Error"


def record(
	provider: str,
	operation: str,
	status: str,
	*,
	document: str = "",
	reference=None,
	http_status: int | None = None,
	result_code: str = "",
	protocol: str = "",
	purpose: str = "",
	response=None,
	error: str = "",
) -> str:
	if response is not None and not isinstance(response, str):
		response = json.dumps(response, ensure_ascii=False)
	doc = frappe.get_doc(
		{
			"doctype": QUERY,
			"provider": provider,
			"operation": operation,
			"status": status,
			"document": document,
			"reference_doctype": reference.doctype if reference else None,
			"reference_name": reference.name if reference else None,
			"user": frappe.session.user,
			"http_status": http_status,
			"result_code": (result_code or "")[:40],
			"protocol": (protocol or "")[:80],
			"purpose": purpose,
			"response": response,
			"error": (error or "")[:1000],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def recent(provider: str, operation: str, document: str, days: int):
	"""A última consulta respondida (achou ou não achou) dentro de `days` dias, ou None."""
	if not document or days <= 0:
		return None
	rows = frappe.get_all(
		QUERY,
		filters={
			"provider": provider,
			"operation": operation,
			"document": document,
			"status": ("in", (SUCCESS, NOT_FOUND)),
			"creation": (">=", add_days(now_datetime(), -days)),
		},
		fields=["name", "creation", "status", "protocol", "response"],
		order_by="creation desc",
		limit=1,
	)
	if not rows:
		return None
	row = rows[0]
	row.data = json.loads(row.response) if row.response else None
	return row
