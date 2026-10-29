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

	filters = {"deleted_doctype": doctype}
	if since:
		filters["creation"] = (">=", since)

	records = frappe.get_all(
		"Deleted Document",
		filters=filters,
		fields=["name", "deleted_doctype", "deleted_name", "creation", "owner"],
		order_by="creation asc",
		limit=500,
	)

	# Se for CRM Deal, isola por funis autorizados da chave
	if doctype == "CRM Deal":
		allowed_pipelines = set(
			frappe.get_all("Cred Hub API Key Pipeline", {"parent": key_info["name"]}, pluck="pipeline")
		)
		filtered = []
		for r in records:
			raw_data = frappe.db.get_value("Deleted Document", r["name"], "data")
			if raw_data:
				try:
					data_dict = frappe.parse_json(raw_data) if isinstance(raw_data, str) else raw_data
					if data_dict.get("pipeline") in allowed_pipelines:
						filtered.append(r)
				except Exception:
					filtered.append(r)
			else:
				# Se não houver snapshot salvo dos campos, preserva o registro
				filtered.append(r)
		return filtered

	return records
