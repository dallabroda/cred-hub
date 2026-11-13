"""Operações em lote da API do HubTech (Batch API, credtech#288)."""

import frappe
from frappe import _

from cred_hub import api_guard, sandbox
from cred_hub.cnpj.mapping import format_cnpj
from cred_hub.party.document import format_cpf, only_digits


SUPPORTED_DOCTYPES = {"CRM Deal", "Contact", "CRM Organization"}
MAX_BATCH_SIZE = 50
ALLOWED_MATCH_BY = frozenset({"name", "external_id", "cnpj", "cpf", "tax_id", "email_id", "email"})


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


# Keys a record never sets through the batch API (identity, ownership, workflow and tree internals)
SYSTEM_KEYS = frozenset({
	"doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent", "parentfield",
	"parenttype", "lft", "rgt", "_user_tags", "_comments", "_assign", "_liked_by",
})


def _space_filters(doctype: str) -> dict:
	"""Garante isolamento estrito entre registros reais e registros do Sandbox nas buscas de identidade."""
	if doctype == "CRM Deal":
		sb = sandbox.sandbox_pipeline()
		if sandbox.is_test_user():
			return {"pipeline": sb or ""}
		return {"pipeline": ("!=", sb)} if sb else {}
	return sandbox.same_space(doctype)


def resolve_record_name(doctype: str, item: dict, match_by: str | None = None) -> str | None:
	"""Localiza o nome (ID interno) de um registro no MESMO espaço (produção vs sandbox) sem pré-consulta.

	Prioridades de correspondência (ou restrito a `match_by` quando informado):
	1. Match por name se fornecido, existente no banco e pertencente ao mesmo espaço.
	2. Match por external_id se fornecido (identificador universal de ERP).
	3. Para CRM Organization: match por cnpj ou tax_id (14 dígitos normalizados, formatado ou raw).
	4. Para Contact: match por cpf ou tax_id (11 dígitos normalizados, formatado ou raw), ou email_id/email.
	5. Para CRM Deal: match por external_id.
	"""
	if match_by is not None:
		match_by = str(match_by).strip()
		if match_by and match_by not in ALLOWED_MATCH_BY:
			frappe.throw(_("Unsupported match_by field: {0}.").format(match_by), frappe.ValidationError)
		if not match_by:
			match_by = None

	space = _space_filters(doctype)

	if match_by in (None, "name"):
		name = item.get("name")
		if name and frappe.db.exists(doctype, name):
			if sandbox.is_sandbox_record(doctype, name) == sandbox.is_test_user():
				return name

	if match_by in (None, "external_id"):
		external_id = item.get("external_id")
		if external_id and str(external_id).strip():
			found = frappe.db.get_value(
				doctype,
				{"external_id": str(external_id).strip(), **space},
				"name",
				order_by="creation asc",
			)
			if found:
				return found

	if doctype == "CRM Organization" and match_by in (None, "cnpj", "tax_id"):
		raw_cnpj = (
			item.get("cnpj")
			if match_by == "cnpj"
			else (item.get("tax_id") if match_by == "tax_id" else (item.get("cnpj") or item.get("tax_id")))
		)
		if raw_cnpj:
			digits = only_digits(raw_cnpj)
			if len(digits) == 14:
				formatted = format_cnpj(digits)
				found = frappe.db.get_value(
					"CRM Organization",
					{"cnpj": ("in", [digits, formatted]), **space},
					"name",
					order_by="creation asc",
				)
				if found:
					return found

	elif doctype == "Contact":
		if match_by in (None, "cpf", "tax_id"):
			raw_cpf = (
				item.get("cpf")
				if match_by == "cpf"
				else (item.get("tax_id") if match_by == "tax_id" else (item.get("cpf") or item.get("tax_id")))
			)
			if raw_cpf:
				digits = only_digits(raw_cpf)
				if len(digits) == 11:
					formatted = format_cpf(digits)
					found = frappe.db.get_value(
						"Contact",
						{"cpf": ("in", [digits, formatted]), **space},
						"name",
						order_by="creation asc",
					)
					if found:
						return found
		if match_by in (None, "email_id", "email"):
			email = (
				item.get("email_id")
				if match_by == "email_id"
				else (item.get("email") if match_by == "email" else (item.get("email_id") or item.get("email")))
			)
			if email and str(email).strip():
				clean_email = str(email).strip().lower()
				found = frappe.db.get_value(
					"Contact",
					{"email_id": clean_email, **space},
					"name",
					order_by="creation asc",
				)
				if found:
					return found

	return None


def prepare_upsert_payload(doctype: str, item: dict) -> dict:
	"""Remove chaves internas de sistema e normaliza aliases de integração (tax_id, email) para os campos reais do Frappe."""
	data = {k: v for k, v in item.items() if k not in SYSTEM_KEYS}
	if doctype == "CRM Organization" and "tax_id" in data:
		raw_tax = data.pop("tax_id")
		if not data.get("cnpj") and raw_tax:
			digits = only_digits(raw_tax)
			if len(digits) == 14:
				data["cnpj"] = digits
	elif doctype == "Contact":
		if "tax_id" in data:
			raw_tax = data.pop("tax_id")
			if not data.get("cpf") and raw_tax:
				digits = only_digits(raw_tax)
				if len(digits) == 11:
					data["cpf"] = digits
		if "email" in data:
			raw_email = data.pop("email")
			if not data.get("email_id") and raw_email:
				data["email_id"] = str(raw_email).strip().lower()
	else:
		data.pop("tax_id", None)
	return data


def sync_contact_channels(doc, item_data: dict):
	"""No Frappe, Contact.validate recalcula email_id, phone e mobile_no a partir de email_ids e phone_nos.
	Em atualizações planas via API, sincroniza as tabelas filhas primárias para não perder o novo valor."""
	if doc.doctype != "Contact":
		return
	if "email_id" in item_data and item_data["email_id"]:
		new_email = str(item_data["email_id"]).strip().lower()
		matched_row = None
		for row in doc.get("email_ids") or []:
			row.is_primary = 0
			if (row.get("email_id") or "").strip().lower() == new_email:
				matched_row = row
		if matched_row:
			matched_row.is_primary = 1
		else:
			doc.append("email_ids", {"email_id": new_email, "is_primary": 1})

	if "mobile_no" in item_data and item_data["mobile_no"]:
		new_mobile = str(item_data["mobile_no"]).strip()
		matched_row = None
		for row in doc.get("phone_nos") or []:
			row.is_primary_mobile_no = 0
			if (row.get("phone") or "").strip() == new_mobile:
				matched_row = row
		if matched_row:
			matched_row.is_primary_mobile_no = 1
		else:
			doc.append("phone_nos", {"phone": new_mobile, "is_primary_mobile_no": 1})

	if "phone" in item_data and item_data["phone"]:
		new_phone = str(item_data["phone"]).strip()
		matched_row = None
		for row in doc.get("phone_nos") or []:
			row.is_primary_phone = 0
			if (row.get("phone") or "").strip() == new_phone:
				matched_row = row
		if matched_row:
			matched_row.is_primary_phone = 1
		else:
			doc.append("phone_nos", {"phone": new_phone, "is_primary_phone": 1})


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

		if item.get("doctype") not in (None, "", doctype):
			# A record cannot switch to another doctype than the one the key's scope was checked for (audit H2)
			results.append({"index": index, "status": "error", "error": _("Record doctype must be {0}.").format(doctype)})
			failed_count += 1
			continue
		savepoint = f"bulk_item_{index}"
		try:
			frappe.db.savepoint(savepoint)
			matched_name = resolve_record_name(doctype, item)
			exists = bool(matched_name)
			if exists and getattr(frappe.local, "credhub_api_is_test", False):
				# a test key never learns that a real record exists: what it cannot read is created anew
				if not frappe.has_permission(doctype, "read", doc=matched_name):
					exists = False
					matched_name = None
			# system-filled fields and child rows of another record are refused, as in the REST routes (audit M6)
			item_data = prepare_upsert_payload(doctype, item)
			api_guard.check_write(doctype, item_data, matched_name if exists else None)

			if exists:
				doc = frappe.get_doc(doctype, matched_name)
				doc.check_permission("write")
				doc.update(item_data)
				sync_contact_channels(doc, item_data)
				doc.save()
				action = "updated"
			else:
				doc_data = {k: v for k, v in item_data.items() if k != "name"}
				doc = frappe.get_doc({"doctype": doctype, **doc_data})
				sync_contact_channels(doc, doc_data)
				doc.check_permission("create")
				doc.insert()
				action = "created"

			results.append({
				"index": index,
				"status": "success",
				"action": action,
				"name": doc.name,
				"external_id": doc.get("external_id") or None,
			})
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

