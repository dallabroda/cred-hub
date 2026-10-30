"""Operações em lote da API do HubTech (Batch API, credtech#288)."""

import frappe
from frappe import _


SUPPORTED_DOCTYPES = {"CRM Deal", "Contact", "CRM Organization"}
MAX_BATCH_SIZE = 50


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
			frappe.db.rollback(save_point=savepoint)
			results.append({"index": index, "status": "error", "error": str(exc).strip()})
			failed_count += 1

	return {
		"total": len(records),
		"success": success_count,
		"failed": failed_count,
		"results": results,
	}
