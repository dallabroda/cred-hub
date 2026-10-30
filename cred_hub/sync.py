"""Sincronização incremental da API do HubTech (credtech#288): leitura segura do log de deleções."""

import frappe
from frappe import _


@frappe.whitelist()
def get_deleted_records(doctype: str, since: str | None = None) -> list[dict]:
	"""Retorna os registros excluídos de um DocType autorizado para sincronização incremental.

	- Requer chave de API autenticada com o escopo `sync_deletes`.
	- A chave precisa ter escopo de leitura do doctype solicitado.
	- Para `CRM Deal`, filtra os registros excluídos para retornar apenas cards dos funis da chave.
	"""
	from cred_hub.api_keys import RESOURCES, _scopes

	key_info = getattr(frappe.local, "credhub_api_key", None)
	if not key_info:
		frappe.throw(_("This method is only accessible via API Key."), frappe.PermissionError)

	scopes = _scopes(key_info["name"])
	if "sync_deletes" not in scopes:
		frappe.throw(_("This API key does not have the 'sync_deletes' scope."), frappe.PermissionError)

	read_scope, _write = RESOURCES.get(doctype, (None, None))
	if not read_scope or read_scope not in scopes:
		frappe.throw(_("This API key is not allowed to read {0}.").format(doctype), frappe.PermissionError)

	pipelines = frappe.get_all("Cred Hub API Key Pipeline", {"parent": key_info["name"]}, pluck="pipeline")
	return deleted_since(doctype, since, pipelines)


# Exclusões que dependem de um card: o funil vem do snapshot gravado na exclusão (CRM Deal) ou do card
# a que a nota, tarefa ou arquivo pertencia. Sem snapshot, sem funil, ou com o card já excluído, o
# registro fica de fora: falhar fechado, nunca mostrar "por garantia" (credtech#288).
CARD_SCOPED = {
	"FCRM Note": ("reference_doctype", "reference_docname"),
	"CRM Task": ("reference_doctype", "reference_docname"),
	"File": ("attached_to_doctype", "attached_to_name"),
}


def deleted_since(doctype: str, since: str | None, pipelines: list[str], limit: int = 500) -> list[dict]:
	"""O filtro por funil é feito no banco, antes do limite: com o filtro depois, 500 exclusões de outros
	funis devolviam uma lista vazia e quem sincroniza nunca conseguia passar delas."""
	where = ["dd.deleted_doctype = %(doctype)s"]
	values = {"doctype": doctype, "since": since, "pipelines": tuple(pipelines) or ("",), "limit": limit}
	if since:
		where.append("dd.creation >= %(since)s")
	if doctype == "CRM Deal":
		where.append("JSON_VALUE(dd.data, '$.pipeline') in %(pipelines)s")
	elif doctype in CARD_SCOPED:
		kind, name = CARD_SCOPED[doctype]
		where.append(
			f"(ifnull(JSON_VALUE(dd.data, '$.{kind}'), '') != 'CRM Deal'"
			f" or JSON_VALUE(dd.data, '$.{name}') in"
			" (select deal.name from `tabCRM Deal` deal where deal.pipeline in %(pipelines)s))"
		)
	return frappe.db.sql(
		f"""select dd.name, dd.deleted_doctype, dd.deleted_name, dd.creation
		from `tabDeleted Document` dd
		where {" and ".join(where)}
		order by dd.creation asc, dd.name asc
		limit %(limit)s""",
		values,
		as_dict=True,
	)
