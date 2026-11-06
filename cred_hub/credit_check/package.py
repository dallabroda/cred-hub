"""Pacote mínimo do dossiê de crédito (credtech#316): as consultas que todo dossiê precisa ter.

A empresa escolhe o pacote nas Configurações de Crédito. O padrão é o Dossiê QUOD e o SCR Bacen da empresa (decisão
do dono em 02/11/2026); dá para trocar ou somar Serasa, Boa Vista e as consultas dos sócios com CPF. O botão
"Gerar dossiê" confere o que já foi consultado dentro do prazo de reaproveitamento, mostra o que falta e quanto custa
na conta do cliente no provedor e, com a confirmação, faz as consultas que faltam com a chave do cliente e gera o
PDF 1 (retornos) e o PDF 2 (parecer da IA). Consulta que falha fica registrada com o motivo, e o parecer recebe a
situação de cada item do pacote: só item do pacote que faltou rebaixa a recomendação.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, format_datetime, get_datetime, now_datetime

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check import partners as partners_lib
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS

SETTINGS = "Cred Hub Credit Settings"
FIELD = "dossier_package_json"
COMPANY_BUREAUS = ("dossie_quod", "scr_bacen", "boavista_risco", "serasa")
PARTNER_BUREAUS = partners_lib.PARTNER_BUREAUS
DEFAULT_ITEMS = ("company:dossie_quod", "company:scr_bacen")
DEFAULT_REUSE_DAYS = 30
MAX_REUSE_DAYS = 365
DONE = "Concluído"  # status gravado na consulta que deu certo (o mesmo das consultas avulsas)
FAILED = "Erro"
JOB_PREFIX = "credit_package::"


def valid_items() -> list[str]:
	return [f"company:{bureau}" for bureau in COMPANY_BUREAUS] + [f"partners:{bureau}" for bureau in PARTNER_BUREAUS]


def bureau_label(bureau: str) -> str:
	if bureau == "serasa":
		return "Serasa Experian (Relatório Avançado PJ)"
	return PREMIUM_BUREAUS[bureau].label


def _list_price(bureau: str) -> float | None:
	registry = PREMIUM_BUREAUS.get(bureau)
	return registry.price_reference if registry else None  # Serasa: depende do contrato do cliente


def config(settings_doc=None) -> dict[str, Any]:
	"""O pacote em vigor: {"items": [...], "reuse_days": n, "is_default": bool}. Sem nada salvo, vale o padrão."""
	raw = (settings_doc.get(FIELD) if settings_doc is not None else frappe.db.get_single_value(SETTINGS, FIELD)) or ""
	try:
		data = json.loads(raw) if raw else {}
	except ValueError:
		data = {}
	if not isinstance(data, dict):
		data = {}
	allowed = valid_items()
	items = [item for item in data.get("items") or [] if item in allowed] if "items" in data else list(DEFAULT_ITEMS)
	reuse = data.get("reuse_days")
	reuse_days = DEFAULT_REUSE_DAYS if reuse is None else max(0, min(MAX_REUSE_DAYS, cint(reuse)))
	return {"items": items, "reuse_days": reuse_days, "is_default": not raw}


def save(items: Any, reuse_days: Any) -> dict[str, Any]:
	"""Grava o pacote escolhido pelo administrador. Precisa de pelo menos uma consulta da empresa."""
	if isinstance(items, str):
		items = frappe.parse_json(items)
	items = [str(item) for item in items or []]
	allowed = valid_items()
	unknown = [item for item in items if item not in allowed]
	if unknown:
		frappe.throw(_("Unknown item in the minimum package: {0}").format(", ".join(unknown)))
	if not any(item.startswith("company:") for item in items):
		frappe.throw(_("The minimum package needs at least one query of the company."))
	try:
		days = int(reuse_days)
	except (TypeError, ValueError):
		frappe.throw(_("Invalid value for {0}.").format("reuse_days"))
	if days < 0 or days > MAX_REUSE_DAYS:
		frappe.throw(_("Queries can be reused for at most {0} days.").format(MAX_REUSE_DAYS))
	ordered = [item for item in allowed if item in items]
	frappe.db.set_single_value(
		SETTINGS,
		FIELD,
		json.dumps({"items": ordered, "reuse_days": days}),
		update_modified=False,
	)
	return config()


def options(settings_doc=None) -> list[dict[str, Any]]:
	"""Todos os itens que podem entrar no pacote, com o preço de tabela e se o bureau está ligado e configurado."""
	from cred_hub.credit_check import api  # o api importa este módulo

	settings_doc = settings_doc or frappe.get_single(SETTINGS)
	enabled = {item["key"]: item for item in api._final_bureaus(settings_doc)}
	result = []
	for item in valid_items():
		scope, bureau = item.split(":", 1)
		option = enabled.get(bureau)
		result.append(
			{
				"item": item,
				"scope": scope,
				"bureau": bureau,
				"label": bureau_label(bureau),
				"price_reference": _list_price(bureau),
				"provider": "Serasa Experian" if bureau == "serasa" else "DirectD",
				"enabled": bool(option),
				"configured": bool(option and option["configured"]),
			}
		)
	return result


def _attempts(organization_name: str, bureau: str, document: str, since) -> list[Any]:
	return frappe.get_all(
		"Cred Hub Premium Bureau Query",
		filters={
			"organization": organization_name,
			"bureau": bureau,
			"document": document,
			"requested_on": (">=", since),
		},
		fields=["name", "status", "requested_on", "raw_response_json"],
		order_by="requested_on desc",
		limit_page_length=20,
	)


def _error_of(row) -> str:
	try:
		return str((json.loads(row.raw_response_json or "{}") or {}).get("error") or "")
	except (ValueError, AttributeError):
		return ""


def status(organization_name: str, settings_doc=None, as_of=None) -> dict[str, Any]:
	"""A situação do pacote para uma Organização: cada item por empresa ou sócio, como "reused" (consulta que deu
	certo dentro do prazo), "missing" (falta consultar) ou "unavailable" (bureau desligado ou sem credencial).

	`as_of` é o momento de referência do prazo (o pedido do parecer), para o parecer enxergar as consultas feitas
	logo depois do pedido mesmo com prazo 0 ("sempre consultar de novo")."""
	from cred_hub.credit_check import api  # o api importa este módulo

	settings_doc = settings_doc or frappe.get_single(SETTINGS)
	conf = config(settings_doc)
	enabled = {item["key"]: item for item in api._final_bureaus(settings_doc)}
	org = frappe.db.get_value("CRM Organization", organization_name, ["organization_name", "cnpj"], as_dict=True) or {}
	reference = get_datetime(as_of) if as_of else now_datetime()
	since = add_days(reference, -conf["reuse_days"])
	wants_partners = any(item.startswith("partners:") for item in conf["items"])
	partners = partners_lib.partners_of(organization_name) if wants_partners else []

	entries: list[dict[str, Any]] = []
	for item in conf["items"]:
		scope, bureau = item.split(":", 1)
		option = enabled.get(bureau)
		if scope == "company":
			subjects = [{"contact": None, "name": org.get("organization_name") or organization_name, "document": org.get("cnpj") or ""}]
		else:
			subjects = [
				{"contact": p["contact"], "name": p["name"], "document": only_digits(p["cpf"])} for p in partners if p["has_cpf"]
			]
		for subject in subjects:
			entry = {
				"item": item,
				"bureau": bureau,
				"label": bureau_label(bureau),
				"provider": "Serasa Experian" if bureau == "serasa" else "DirectD",
				"subject_type": "Partner" if subject["contact"] else "Organization",
				"contact": subject["contact"],
				"subject_name": subject["name"],
				"document": subject["document"],
				"price_reference": _list_price(bureau),
				"state": "missing",
				"last_on": None,
				"last_error": "",
			}
			if not option:
				entry["state"] = "unavailable"
				entry["reason"] = _("not enabled in the Credit settings")
			elif not option["configured"]:
				entry["state"] = "unavailable"
				entry["reason"] = _("no API credential in the Credit settings")
			else:
				attempts = _attempts(organization_name, bureau, subject["document"], since)
				done = next((row for row in attempts if row.status == DONE), None)
				if done:
					entry["state"] = "reused"
					entry["last_on"] = format_datetime(done.requested_on)
				elif attempts and attempts[0].status == FAILED:
					entry["last_on"] = format_datetime(attempts[0].requested_on)
					entry["last_error"] = _error_of(attempts[0])
			entries.append(entry)

	missing = [entry for entry in entries if entry["state"] == "missing"]
	return {
		"items": entries,
		"reuse_days": conf["reuse_days"],
		"missing": missing,
		"unavailable": [entry for entry in entries if entry["state"] == "unavailable"],
		"missing_cost": round(sum(entry["price_reference"] or 0 for entry in missing), 2),
		"missing_has_contract_price": any(entry["price_reference"] is None for entry in missing),
		"partners_without_cpf": [p["name"] for p in partners if not p["has_cpf"]],
		"ready": not missing and not any(entry["state"] == "unavailable" for entry in entries),
	}


def for_ai(organization_name: str, as_of=None) -> list[dict[str, Any]]:
	"""A situação de cada item do pacote para o parecer: "done", "failed" (com o motivo) ou "not done"."""
	result = []
	for entry in status(organization_name, as_of=as_of)["items"]:
		if entry["state"] == "reused":
			state = "done"
		elif entry["last_error"]:
			state = "failed"
		else:
			state = "not done"
		item = {"query": entry["label"], "queried": entry["subject_name"], "state": state}
		if entry["last_on"]:
			item["when"] = entry["last_on"]
		if entry["last_error"]:
			item["error"] = entry["last_error"]
		result.append(item)
	return result


def job_id(organization_name: str) -> str:
	return f"{JOB_PREFIX}{organization_name}"


def run_job(
	organization_name: str,
	deal_name: str | None = None,
	verdict: str = "Not Checked",
	warning_acknowledged: bool = False,
	opinion_name: str | None = None,
) -> None:
	"""Em segundo plano: faz as consultas que faltam no pacote, gera o PDF 1 e, se pedido, o parecer (PDF 2)."""
	from cred_hub.credit_check import api, dossier
	from cred_hub.credit_check import opinion as credit_opinion

	try:
		settings_doc = frappe.get_single(SETTINGS)
		current = status(organization_name, settings_doc)
		enabled = {item["key"]: item for item in api._final_bureaus(settings_doc)}
		cnpj = frappe.db.get_value("CRM Organization", organization_name, "cnpj") or ""
		done, failed, spent = [], [], 0.0
		for entry in current["missing"]:
			option = enabled[entry["bureau"]]
			try:
				raw, _data = api._execute_final_query(settings_doc, entry["bureau"], entry["document"])
				query_status, cost = DONE, option["price_reference"] or 0.0
			except api.FinalQueryError as e:
				raw, query_status, cost = {"error": str(e)}, FAILED, 0.0
			except Exception as e:  # noqa: BLE001 - falha inesperada de um provedor não pode travar o resto do pacote
				frappe.log_error(title="Credit package query failed", message=frappe.get_traceback())
				raw, query_status, cost = {"error": str(e)}, FAILED, 0.0
			api._log_premium_bureau_query(
				entry["bureau"],
				organization_name,
				deal_name,
				cnpj,
				cost,
				query_status,
				raw,
				verdict,
				warning_acknowledged,
				document=entry["document"],
				contact_name=entry["contact"],
			)
			# consulta paga fica registrada mesmo se um passo seguinte falhar
			frappe.db.commit()  # nosemgrep
			label = f"{entry['label']} ({entry['subject_name']})"
			if query_status == DONE:
				done.append(label)
				spent += cost
			else:
				failed.append(label)

		if current["missing"]:
			message = _("Minimum package of the credit dossier: {0} queries made, {1} failed. List price billed by the provider on the client's account: R$ {2}.").format(
				len(done), len(failed), f"{spent:.2f}"
			)
			if failed:
				message += " " + _("Failed: {0}.").format(", ".join(failed))
			target = frappe.get_doc("CRM Deal", deal_name) if deal_name else frappe.get_doc("CRM Organization", organization_name)
			target.add_comment("Comment", message)

		dossier.generate(organization_name, deal_name)
		frappe.db.commit()  # nosemgrep
	except Exception as e:
		if opinion_name:
			doc = frappe.get_doc(credit_opinion.DOCTYPE, opinion_name)
			credit_opinion._fail(doc, str(e), {"prompt": 0, "output": 0, "thinking": 0, "cost": 0.0, "seconds": 0.0})
			frappe.db.commit()  # nosemgrep
		raise

	if opinion_name:
		credit_opinion.run(opinion_name)
