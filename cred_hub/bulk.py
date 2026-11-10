"""Operações em lote da API do HubTech (Batch API, credtech#288)."""

import random
import time

import frappe
from frappe import _


SUPPORTED_DOCTYPES = {"CRM Deal", "Contact", "CRM Organization"}
MAX_BATCH_SIZE = 50


def _is_deadlock(exc: Exception) -> bool:
	msg = str(exc).lower()
	return "deadlock" in msg or "1213" in msg or "querydeadlockerror" in msg


def _run_with_retry(fn, max_retries=3, base_delay=0.05):
	"""Executa uma ação no banco com retry automático e jitter exponencial em caso de deadlock."""
	for attempt in range(max_retries):
		try:
			return fn()
		except Exception as exc:
			if _is_deadlock(exc) and attempt < max_retries - 1:
				time.sleep(base_delay * (2 ** attempt) + random.uniform(0.01, 0.05))
				continue
			raise


@frappe.whitelist(methods=["POST"])
def bulk_upsert(doctype: str, records: list[dict] | str) -> dict:
	"""Cria ou atualiza até 50 registros em lote sob uma única chamada de API.

	- Requer chave de API com escopo de escrita no doctype (cards_write, contacts_write ou organizations_write).
	- Respeita todas as validações, regras de etapa e isolamento de funil.
	- Retorna relatório estruturado de sucessos e falhas por registro sem abortar o lote inteiro.
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
				_run_with_retry(doc.save)
				action = "updated"
			else:
				doc_data = {k: v for k, v in item.items() if k != "name"} if name else dict(item)
				doc = frappe.get_doc({"doctype": doctype, **doc_data})
				doc.check_permission("create")
				_run_with_retry(doc.insert)
				action = "created"

			results.append({"index": index, "status": "success", "action": action, "name": doc.name})
			success_count += 1
		except Exception as exc:
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
				_run_with_retry(doc.delete)
				action = "deleted"
				results.append({"index": index, "name": name, "status": "success", "action": action})
				success_count += 1
			else:
				results.append({"index": index, "name": name, "status": "not_found", "action": "ignored"})
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			results.append({"index": index, "name": name, "status": "error", "error": str(exc).strip()})
			failed_count += 1

	return {
		"total": len(names),
		"success": success_count,
		"failed": failed_count,
		"results": results,
	}

