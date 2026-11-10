"""Operações em lote da API do HubTech (Batch API, credtech#288)."""

import frappe
from frappe import _


SUPPORTED_DOCTYPES = {"CRM Deal", "Contact", "CRM Organization"}
MAX_BATCH_SIZE = 50


class BatchRetryable(frappe.ValidationError):
	"""The database undid the whole batch (deadlock): nothing was saved and the same call can be sent again."""

	http_status_code = 409


def _is_deadlock(exc: Exception) -> bool:
	"""Only a real deadlock: Frappe's QueryDeadlockError, or the driver's error with code 1213 as its first argument.
	Never the message text: "CRM-ORG-2026-01213" or "Value 1213 is not a valid CPF" are record errors, and treating
	them as a deadlock would make the client resend the same bad batch forever."""
	if isinstance(exc, getattr(frappe, "QueryDeadlockError", ())):
		return True
	args = getattr(exc, "args", ())
	return bool(args) and args[0] == 1213


def _abort_batch(index: int):
	"""A deadlock makes MariaDB undo the WHOLE transaction, not just the statement: the records already reported as
	saved in this call are gone and the item savepoints too. Retrying inside the same transaction would answer
	"saved" for records that are not. Undo everything and tell the client to send the same call again."""
	frappe.db.rollback()
	frappe.throw(
		_(
			"The database was busy and undid this batch at record {0}. Nothing was saved: send the same call again."
		).format(index),
		BatchRetryable,
	)


@frappe.whitelist(methods=["POST"])
def bulk_upsert(doctype: str, records: list[dict] | str) -> dict:
	"""Cria ou atualiza até 50 registros em lote sob uma única chamada de API.

	- Requer chave de API com escopo de escrita no doctype (cards_write, contacts_write ou organizations_write).
	- Respeita todas as validações, regras de etapa e isolamento de funil.
	- Retorna relatório estruturado de sucessos e falhas por registro sem abortar o lote inteiro.
	- Deadlock no banco desfaz o lote inteiro: responde 409 (BatchRetryable) e nada é gravado; repita a chamada.
	"""
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if not key_info:
		frappe.throw(_("This method is only accessible via API Key."), frappe.PermissionError)

	if doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("Batch operations are not supported for {0}.").format(doctype), frappe.ValidationError)

	if isinstance(records, str):
		try:
			records = frappe.parse_json(records)
		except Exception:
			frappe.throw(_("Invalid JSON in records parameter."), frappe.ValidationError)

	if not isinstance(records, list) or not records:
		frappe.throw(_("The 'records' parameter must be a non-empty list."), frappe.ValidationError)

	if len(records) > MAX_BATCH_SIZE:
		frappe.throw(
			_("Maximum of {0} records per batch (received {1}).").format(MAX_BATCH_SIZE, len(records)),
			frappe.ValidationError,
		)

	success_count = 0
	failed_count = 0
	results = []

	for index, item in enumerate(records):
		if not isinstance(item, dict):
			results.append({"index": index, "status": "error", "error": _("Record must be a JSON object.")})
			failed_count += 1
			continue

		name = item.get("name")
		savepoint = f"bulk_item_{index}"
		try:
			frappe.db.savepoint(savepoint)

			if name and frappe.db.exists(doctype, name):
				doc = frappe.get_doc(doctype, name)
				doc.check_permission("write")
				doc.update(item)
				doc.save()
				action = "updated"
			else:
				doc_data = {k: v for k, v in item.items() if k != "name"} if name else dict(item)
				doc = frappe.get_doc({"doctype": doctype, **doc_data})
				doc.check_permission("create")
				doc.insert()
				action = "created"

			results.append({"index": index, "status": "success", "action": action, "name": doc.name})
			success_count += 1
		except Exception as exc:
			if _is_deadlock(exc):
				_abort_batch(index)
			frappe.db.rollback(save_point=savepoint)
			results.append({"index": index, "status": "error", "error": str(exc).strip()})
			failed_count += 1

	return {
		"total": len(records),
		"success": success_count,
		"failed": failed_count,
		"results": results,
	}


@frappe.whitelist(methods=["POST"])
def bulk_delete(doctype: str, names: list[str] | str) -> dict:
	"""Exclui até 50 registros em lote sob uma única chamada de API.

	- Requer chave de API com escopo de escrita no doctype e escopo allow_delete.
	- Respeita isolamento de funil e bloqueio de doctypes protegidos.
	- Retorna relatório estruturado de sucessos e falhas por registro sem abortar o lote inteiro.
	- Deadlock no banco desfaz o lote inteiro: responde 409 (BatchRetryable) e nada é gravado; repita a chamada.
	"""
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if not key_info:
		frappe.throw(_("This method is only accessible via API Key."), frappe.PermissionError)

	if doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("Batch operations are not supported for {0}.").format(doctype), frappe.ValidationError)

	from cred_hub.api_keys import PROTECTED_AGAINST_DELETE, _scopes

	scopes = _scopes(key_info["name"])
	if "allow_delete" not in scopes:
		frappe.throw(_("This API key does not have permission to delete records."), frappe.PermissionError)

	if doctype in PROTECTED_AGAINST_DELETE:
		frappe.throw(_("Records of type {0} cannot be deleted via API.").format(doctype), frappe.PermissionError)

	if isinstance(names, str):
		try:
			names = frappe.parse_json(names)
		except Exception:
			frappe.throw(_("Invalid JSON in names parameter."), frappe.ValidationError)

	if not isinstance(names, list) or not names:
		frappe.throw(_("The 'names' parameter must be a non-empty list."), frappe.ValidationError)

	if len(names) > MAX_BATCH_SIZE:
		frappe.throw(
			_("Maximum of {0} records per batch (received {1}).").format(MAX_BATCH_SIZE, len(names)),
			frappe.ValidationError,
		)

	success_count = 0
	failed_count = 0
	results = []

	for index, name in enumerate(names):
		if not isinstance(name, str) or not name.strip():
			results.append({"index": index, "name": name, "status": "error", "error": _("Record name must be a non-empty string.")})
			failed_count += 1
			continue

		name = name.strip()
		savepoint = f"bulk_del_{index}"
		try:
			frappe.db.savepoint(savepoint)

			if frappe.db.exists(doctype, name):
				doc = frappe.get_doc(doctype, name)
				doc.check_permission("delete")
				doc.delete()
				action = "deleted"
				results.append({"index": index, "name": name, "status": "success", "action": action})
				success_count += 1
			else:
				results.append({"index": index, "name": name, "status": "not_found", "action": "ignored"})
		except Exception as exc:
			if _is_deadlock(exc):
				_abort_batch(index)
			frappe.db.rollback(save_point=savepoint)
			results.append({"index": index, "name": name, "status": "error", "error": str(exc).strip()})
			failed_count += 1

	return {
		"total": len(names),
		"success": success_count,
		"failed": failed_count,
		"results": results,
	}

