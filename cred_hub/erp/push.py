"""Push endpoints da Porta B de ERPs (ADR 0007 §1, §4, §5, credtech#375).

Permite que QUALQUER ERP externo (Omie, Conta Azul, Bling, Tiny, Protheus, SAP, Asaas, Qprof ou sistemas proprietários)
envie títulos, baixas e confirmações de cadastro de forma stateless via REST API do HubTech CRM.
"""

from datetime import date

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, today

from cred_hub import sandbox
from cred_hub.billing import CREDIT_AND_COLLECTION, plan_allows
from cred_hub.erp import sync
from cred_hub.erp.base import ErpError
from cred_hub.erp.formats import to_date


def _require_api_key() -> dict:
	key_info = getattr(frappe.local, "credhub_api_key", None)
	if not key_info:
		frappe.throw(_("This method is only accessible via API Key."), frappe.PermissionError)
	return key_info


def _allowed_pipelines(key_info: dict) -> set[str]:
	return set(
		frappe.get_all(
			"Cred Hub API Key Pipeline",
			{"parent": key_info["name"], "parenttype": "Cred Hub API Key"},
			pluck="pipeline",
		)
	)


def _require_collection_pipeline(key_info: dict) -> tuple[str, set[str]]:
	if getattr(frappe.local, "credhub_api_is_test", False) or sandbox.is_test_user():
		raise sandbox.SandboxViolation(
			_("Test API keys (Sandbox) cannot push or settle titles in the real Collection pipeline.")
		)
	try:
		pipeline = sync._collection_pipeline()
	except ErpError as exc:
		frappe.throw(str(exc), frappe.ValidationError)

	allowed = _allowed_pipelines(key_info)
	if pipeline not in allowed:
		frappe.throw(
			_("This API key does not have permission for the Collection pipeline."),
			frappe.PermissionError,
		)
	return pipeline, allowed


def _parse_push_date(raw) -> date | None:
	if raw in (None, ""):
		return None
	if isinstance(raw, date):
		return raw
	parsed = to_date(str(raw).strip())
	if parsed is not None:
		return parsed
	try:
		return getdate(str(raw).strip())
	except Exception:
		return None


@frappe.whitelist(methods=["POST"])
def upsert_titles(titles: list[dict] | str, erp_source: str = "External ERP", run_id: str | None = None) -> dict:
	"""Recebe lote de títulos em aberto/vencidos de qualquer ERP externo e processa via motor de cobrança.

	- Requer chave de API real (não-Sandbox) com escopo cards_write e acesso ao funil de Cobrança.
	- Faz upsert idempotente por erp_source + erp_title_id.
	- Se o título já existir no CRM e estiver aberto, atualiza situação/valor.
	- Se for novo, agrupa no funil de cobrança conforme regra da empresa (devedor + cedente ou devedor).
	"""
	if not plan_allows(CREDIT_AND_COLLECTION):
		frappe.throw(_("The Credit and Collection module is not active for this plan."), frappe.PermissionError)

	key_info = _require_api_key()
	pipeline, _allowed = _require_collection_pipeline(key_info)

	if isinstance(titles, str):
		try:
			titles = frappe.parse_json(titles)
		except Exception:
			frappe.throw(_("Invalid JSON in titles parameter."), frappe.ValidationError)

	if not isinstance(titles, list) or not titles:
		frappe.throw(_("The 'titles' parameter must be a non-empty list of objects."), frappe.ValidationError)

	if len(titles) > 500:
		frappe.throw(_("Maximum batch size is 500 titles per call."), frappe.ValidationError)

	dt_now = now_datetime()
	ts_str = (
		dt_now.strftime("%Y%m%d-%H%M%S")
		if hasattr(dt_now, "strftime")
		else str(dt_now).replace("-", "").replace(":", "").replace(" ", "-")[:15]
	)
	current_run_id = run_id or f"push-{ts_str}"
	frappe.flags.cred_hub_erp_run_id = current_run_id

	stats = sync._new_stats()
	stats["run_id"] = current_run_id
	stats["total_received"] = len(titles)

	normalized_titles = []
	for item in titles:
		if not isinstance(item, dict):
			stats["skipped"] += 1
			continue

		erp_id = item.get("erp_id") or item.get("id") or item.get("titulo_id")
		sacado_doc = (
			item.get("sacado_document")
			or item.get("debtor_document")
			or item.get("cnpj_sacado")
			or item.get("cpf_sacado")
		)
		amount = flt(
			item.get("amount") or item.get("original_amount") or item.get("valor") or item.get("valor_original"),
			2,
		)
		raw_due = item.get("due_date") or item.get("vencimento") or item.get("data_vencimento")
		due_date = _parse_push_date(raw_due)
		raw_issue = item.get("issue_date") or item.get("emissao")
		issue_date = _parse_push_date(raw_issue) if raw_issue else None

		if not erp_id or not sacado_doc or amount <= 0 or not due_date:
			stats["skipped"] += 1
			continue

		norm = {
			"erp": erp_source,
			"erp_id": str(erp_id).strip(),
			"sacado_document": str(sacado_doc).strip(),
			"sacado_name": str(item.get("sacado_name") or item.get("debtor_name") or item.get("nome_sacado") or "").strip(),
			"cedente_document": str(item.get("cedente_document") or item.get("cnpj_cedente") or "").strip(),
			"cedente_name": str(item.get("cedente_name") or item.get("nome_cedente") or "").strip(),
			"number": str(item.get("number") or item.get("title_number") or item.get("numero") or erp_id).strip(),
			"our_number": str(item.get("our_number") or item.get("nosso_numero") or "").strip(),
			"due_date": due_date,
			"issue_date": issue_date,
			"amount": amount,
			"digitable_line": str(item.get("digitable_line") or item.get("linha_digitavel") or "").strip(),
			"situation": str(item.get("situation") or item.get("situacao") or "Em Aberto").strip(),
			"operation": str(item.get("operation") or item.get("operacao") or "").strip(),
			"raw": item,
		}
		normalized_titles.append(norm)

	try:
		sync.upsert_overdue(pipeline, normalized_titles, stats)
	finally:
		frappe.flags.cred_hub_erp_run_id = None

	return {
		"ok": True,
		"run_id": current_run_id,
		"total": len(titles),
		"processed": len(normalized_titles),
		"titles_new": stats["titles_new"],
		"titles_update": stats["titles_update"],
		"cards_new": stats["cards_new"],
		"failed": stats["failed"],
		"skipped": stats["skipped"],
	}


@frappe.whitelist(methods=["POST"])
def settle_titles(settlements: list[dict] | str, erp_source: str = "External ERP") -> dict:
	"""Recebe lote de baixas, liquidações ou recompras do ERP para encerrar títulos no HubTech CRM.

	- Requer chave de API real (não-Sandbox) com escopo cards_write e acesso ao funil de Cobrança.
	- outcome aceitos: 'paid' (quitado) ou 'repurchased' (recomprado/repassado).
	- Quando todos os títulos de um card são liquidados, o card é marcado automaticamente como Ganho (Won).
	"""
	if not plan_allows(CREDIT_AND_COLLECTION):
		frappe.throw(_("The Credit and Collection module is not active for this plan."), frappe.PermissionError)

	key_info = _require_api_key()
	_pipeline, allowed_pipelines = _require_collection_pipeline(key_info)

	if isinstance(settlements, str):
		try:
			settlements = frappe.parse_json(settlements)
		except Exception:
			frappe.throw(_("Invalid JSON in settlements parameter."), frappe.ValidationError)

	if not isinstance(settlements, list) or not settlements:
		frappe.throw(_("The 'settlements' parameter must be a non-empty list of objects."), frappe.ValidationError)

	if len(settlements) > 500:
		frappe.throw(_("Maximum batch size is 500 settlements per call."), frappe.ValidationError)

	stats = sync._new_stats()
	stats["total_received"] = len(settlements)

	normalized_settlements = []
	for item in settlements:
		if not isinstance(item, dict):
			stats["skipped"] += 1
			continue

		erp_id = item.get("erp_id") or item.get("id") or item.get("titulo_id")
		if not erp_id:
			stats["skipped"] += 1
			continue

		erp_id_str = str(erp_id).strip()
		found = sync.find_row(erp_source, erp_id_str)
		if found:
			card_pipeline = frappe.db.get_value("CRM Deal", found[0], "pipeline")
			if card_pipeline not in allowed_pipelines:
				stats["skipped"] += 1
				continue

		raw_outcome = str(item.get("outcome") or item.get("motivo") or item.get("status") or "").lower()
		outcome = "repurchased" if ("recompr" in raw_outcome or "repass" in raw_outcome) else "paid"
		raw_paid = item.get("paid_date") or item.get("paid_on") or item.get("data_pagamento")
		paid_date = _parse_push_date(raw_paid) or getdate(today())
		situation = str(
			item.get("situation")
			or item.get("situacao")
			or item.get("note")
			or item.get("observacao")
			or f"Baixa via API ({erp_source})"
		).strip()

		normalized_settlements.append(
			{
				"erp": erp_source,
				"erp_id": erp_id_str,
				"outcome": outcome,
				"paid_date": paid_date,
				"paid_amount": flt(item.get("paid_amount") or item.get("valor_pago"), 2) or None,
				"situation": situation,
			}
		)

	sync.close_settled(normalized_settlements, stats)

	return {
		"ok": True,
		"total": len(settlements),
		"processed": len(normalized_settlements),
		"titles_closed": stats["titles_closed"],
		"cards_won": stats["cards_won"],
		"cards_to_review": stats["cards_to_review"],
		"failed": stats["failed"],
		"skipped": stats["skipped"],
	}


@frappe.whitelist(methods=["POST"])
def register_result(
	deal: str,
	status: str,
	erp_source: str,
	erp_customer_id: str | None = None,
	message: str | None = None,
) -> dict:
	"""Registra a resposta do ERP externo após o evento de webhook 'cedente.ready_to_register'.

	- Requer chave de API com escopo cards_write (e organizations_write ao vincular erp_customer_id).
	- Localiza o card por name ou external_id respeitando isolamento de funil e Sandbox.
	- Registra comentário auditável na timeline do card.
	- Se status == 'success' e erp_customer_id informado, vincula external_id na Organização do card.
	"""
	from cred_hub.api_keys import _scopes
	from cred_hub.bulk import resolve_record_name

	if not plan_allows(CREDIT_AND_COLLECTION):
		frappe.throw(_("The Credit and Collection module is not active for this plan."), frappe.PermissionError)

	key_info = _require_api_key()

	deal_ref = str(deal or "").strip()
	if not deal_ref:
		frappe.throw(_("Provide the card name or external_id."), frappe.ValidationError)

	deal_name = resolve_record_name("CRM Deal", {"name": deal_ref}, match_by="name")
	if not deal_name:
		deal_name = resolve_record_name("CRM Deal", {"external_id": deal_ref}, match_by="external_id")
	if not deal_name or not frappe.has_permission("CRM Deal", "write", doc=deal_name):
		frappe.throw(_("Card {0} not found.").format(deal_ref), frappe.DoesNotExistError)

	doc = frappe.get_doc("CRM Deal", deal_name)
	doc.check_permission("write")

	is_success = str(status).lower() in ("success", "ok", "approved", "created")
	clean_customer_id = str(erp_customer_id).strip() if erp_customer_id else ""
	linked_id = None

	if is_success and clean_customer_id and doc.organization:
		if "organizations_write" not in _scopes(key_info["name"]):
			frappe.throw(
				_("Linking erp_customer_id to the Organization requires the organizations_write scope."),
				frappe.PermissionError,
			)
		org = frappe.get_doc("CRM Organization", doc.organization)
		org.check_permission("write")
		if not org.get("external_id"):
			org.external_id = clean_customer_id
			org.save()
		linked_id = org.external_id

	safe_source = frappe.utils.escape_html(str(erp_source or "External ERP").strip())
	comment_text = (
		f"<b>Retorno de Cadastro no ERP ({safe_source})</b><br>"
		f"Status: <b>{'Sucesso' if is_success else 'Erro'}</b><br>"
	)
	if clean_customer_id:
		comment_text += f"ID no ERP: <code>{frappe.utils.escape_html(clean_customer_id)}</code><br>"
	if message:
		comment_text += f"Detalhes: {frappe.utils.escape_html(str(message))}"

	doc.add_comment("Comment", text=comment_text)

	return {
		"ok": True,
		"deal": doc.name,
		"organization": doc.organization,
		"status": "success" if is_success else "error",
		"external_id_linked": linked_id,
	}
