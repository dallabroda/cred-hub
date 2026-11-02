"""APIs REST (Frappe Whitelist) para telas de Configurações e Cards do CRM."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.clients.directd import DirectDAuthError, DirectDError, fetch_fgts_regularidade
from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import InfosimplesError, query_pgfn
from cred_hub.credit_check.clients.serasa import (
	SerasaError,
	SerasaNotConfigured,
	trigger_serasa_relato,
)
from cred_hub.credit_check.enrich import get_credit_settings, run_background_check
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS
from cred_hub.credit_check.rules import pre_filter_needs_acknowledgement

# Preço de tabela aproximado de cada chamada de teste de conexão — é uma consulta real, cobrada pelo
# provedor na conta do cliente (credtech#316: todo provedor usa a chave do próprio cliente). Cada
# teste usa a consulta mais barata do provedor: Escavador (processos), Infosimples (só PGFN, R$ 0,20)
# e DirectD (FGTS, R$ 0,36).
TEST_CONNECTION_COST = {
	"escavador": 0.50,
	"infosimples": 0.20,
	"directd": 0.36,
}

# Roles que podem disparar consulta paga a bureau final (as demais só veem as opções).
FINAL_BUREAU_ROLES = ("System Manager", "Sales Manager")


def _check_deal_and_org_permission(deal_name: str | None, organization_name: str | None) -> None:
	"""Garante que o usuário pode ver o card e/ou a organização antes de disparar ou ler a esteira."""
	if deal_name and not frappe.has_permission("CRM Deal", "read", doc=deal_name):
		frappe.throw(_("You do not have permission to access this deal."), frappe.PermissionError)
	if organization_name and not frappe.has_permission("CRM Organization", "read", doc=organization_name):
		frappe.throw(_("You do not have permission to access this organization."), frappe.PermissionError)


@frappe.whitelist()
def trigger_check(deal_name: str | None = None, organization_name: str | None = None, force: bool = False) -> dict[str, Any]:
	"""Dispara a esteira manualmente a partir da interface do Card ou Organização."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(deal_name, organization_name)

	job = frappe.enqueue(
		"cred_hub.credit_check.enrich.run_background_check",
		queue="long",
		job_id=f"manual_credit_check::{deal_name or organization_name}",
		deduplicate=True,
		deal_name=deal_name,
		organization_name=organization_name,
		force=bool(force),
		user=frappe.session.user,
	)
	return {"message": _("Credit and compliance analysis queued in the background."), "job_id": str(job)}


@frappe.whitelist()
def get_report_details(report_name: str) -> dict[str, Any]:
	"""Retorna os dados detalhados do relatório para o modal do Card."""
	if not frappe.db.exists("Cred Hub Credit Report", report_name):
		frappe.throw(_("Report not found."))

	report = frappe.get_doc("Cred Hub Credit Report", report_name)
	_check_deal_and_org_permission(report.get("deal"), report.get("organization"))

	raw_details = {}
	if report.get("raw_details_json"):
		try:
			raw_details = json.loads(report.raw_details_json)
		except Exception:
			pass

	return {
		"name": report.name,
		"organization": report.organization,
		"cnpj": report.cnpj,
		"checked_on": report.checked_on,
		"valid_until": report.valid_until,
		"verdict": report.verdict,
		"verdict_summary": report.verdict_summary,
		"ceis_cnep_sanctioned": bool(report.get("ceis_cnep_sanctioned")),
		"ceis_cnep_verified": bool(report.get("ceis_cnep_verified")),
		"lawsuits_total": report.lawsuits_total,
		"lawsuits_defendant": report.lawsuits_defendant,
		"lawsuits_highest_value": report.lawsuits_highest_value,
		"has_bankruptcy_recovery": bool(report.has_bankruptcy_recovery),
		"pgfn_status": report.pgfn_status,
		"pgfn_total_debt": report.pgfn_total_debt,
		"cnd_federal_status": report.cnd_federal_status,
		"cnd_federal_pdf_url": report.cnd_federal_pdf_url,
		"cndt_status": report.cndt_status,
		"cndt_pdf_url": report.cndt_pdf_url,
		"cenprot_has_protests": bool(report.cenprot_has_protests),
		"cenprot_protests_count": report.cenprot_protests_count,
		"fgts_status": report.get("fgts_status") or "",
		"sintegra_situacao_cadastral": report.get("sintegra_situacao_cadastral") or "",
		"total_cost": report.get("total_cost") or 0.0,
		"client_total_cost": report.get("client_total_cost") or 0.0,
		"dossie_pdf_url": report.get("dossie_pdf_url") or "",
		"directd_score": ((raw_details.get("directd_score") or {}).get("score")),
		"directd_score_faixa": ((raw_details.get("directd_score") or {}).get("faixa_score") or ""),
		"raw_details": raw_details,
	}


DOSSIE_PRINT_FORMAT = "Cred Hub - Dossiê de Crédito"


@frappe.whitelist()
def generate_dossie_pdf(organization_name: str, report_name: str | None = None) -> dict[str, Any]:
	"""Gera o dossiê de crédito em PDF (dados estruturados por seção) a partir do relatório mais
	recente da Organização (ou de um relatório específico) e anexa nos Anexos da Organização."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)

	if not report_name:
		report_name = frappe.db.get_value("CRM Organization", organization_name, "credit_report")
	if not report_name or not frappe.db.exists("Cred Hub Credit Report", report_name):
		frappe.throw(_("Run the credit analysis before generating the dossier."))

	report = frappe.get_doc("Cred Hub Credit Report", report_name)
	if report.organization != organization_name:
		frappe.throw(_("This report does not belong to this organization."))

	pdf_bytes = frappe.get_print("Cred Hub Credit Report", report_name, DOSSIE_PRINT_FORMAT, as_pdf=True)

	from frappe.utils.file_manager import save_file

	cnpj_slug = (report.cnpj or "").replace(".", "").replace("/", "").replace("-", "")
	filename = f"Dossie-Credito-{cnpj_slug}-{report.checked_on}.pdf"
	file_doc = save_file(filename, pdf_bytes, "CRM Organization", organization_name, is_private=1)

	report.db_set("dossie_pdf_url", file_doc.file_url, update_modified=False)
	return {"file_url": file_doc.file_url, "file_name": file_doc.file_name}


@frappe.whitelist()
def get_price_references() -> dict[str, Any]:
	"""Preço de tabela de cada consulta, para a tela de configurações mostrar antes de o cliente
	ligar um provedor (credtech#316). É só referência: quem cobra é o provedor, na conta do
	cliente, e o contrato dele pode ter outro preço."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	from cred_hub.credit_check import enrich

	return {
		"escavador": enrich.ESCAVADOR_ESTIMATED_COST,
		"infosimples": enrich.INFOSIMPLES_ESTIMATED_COST,
		"directd_score": enrich.DIRECTD_SCORE_ESTIMATED_COST,
		"directd_protestos": enrich.DIRECTD_PROTESTS_ESTIMATED_COST,
		"directd_processos": enrich.DIRECTD_PROCESSOS_ESTIMATED_COST,
		"directd_fgts": enrich.DIRECTD_FGTS_ESTIMATED_COST,
		"directd_sintegra": enrich.DIRECTD_SINTEGRA_ESTIMATED_COST,
		**{key: registry.price_reference for key, registry in PREMIUM_BUREAUS.items()},
		"test_connection": TEST_CONNECTION_COST,
	}


def _truthy(value: Any) -> bool:
	"""Flags vindas do frontend chegam como bool, int ou string ("1", "true")."""
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def _is_masked(value: str) -> bool:
	"""Campo Password já salvo volta para a tela como asteriscos."""
	return bool(value) and set(value) == {"*"}


@frappe.whitelist()
def test_provider_connection(provider: str, token: str, cnpj: str, confirm_cost: bool = False) -> dict[str, Any]:
	"""Valida se o token informado conecta com sucesso ao provedor.

	Custo real e explícito: exige um CNPJ escolhido por quem está testando (nunca um CNPJ de
	terceiro fixo no código) e a confirmação de que esta chamada tem custo — cobrado pelo provedor
	na conta do cliente —, para o custo nunca ficar escondido num botão "testar". Sem token digitado
	(ou com o token salvo, que volta mascarado), testa o token já salvo nas configurações.
	"""
	frappe.only_for("System Manager")
	token = (token or "").strip()
	cnpj = (cnpj or "").strip()
	if provider not in TEST_CONNECTION_COST:
		frappe.throw(_("Unknown provider: {0}").format(provider))
	if not token or _is_masked(token):
		settings_doc = frappe.get_single("Cred Hub Credit Settings")
		token = (settings_doc.get_password(f"{provider}_token", raise_exception=False) or "").strip()
	if not token:
		frappe.throw(_("Enter the API token to test."))
	if not cnpj:
		frappe.throw(_("Enter a CNPJ to run the test query against."))
	digits = only_digits(cnpj)
	if len(digits) != 14:
		frappe.throw(_("Enter a valid CNPJ to run the test query against."))
	if not _truthy(confirm_cost):
		frappe.throw(
			_("This test runs a real paid query, billed by the provider on your account (list price ~R$ {0}). Confirm the cost to continue.").format(
				f"{TEST_CONNECTION_COST[provider]:.2f}"
			)
		)

	if provider == "escavador":
		try:
			fetch_lawsuits(digits, token)
			return {"ok": True, "message": _("Connected to the Escavador API (v2) successfully!")}
		except Exception as e:
			frappe.throw(_("Escavador connection test failed: {0}").format(str(e)))
	if provider == "infosimples":
		try:
			query_pgfn(digits, token)
			return {"ok": True, "message": _("Connected to the Infosimples API successfully!")}
		except (InfosimplesError, ValueError) as e:
			frappe.throw(_("Infosimples connection test failed: {0}").format(str(e)))
	if provider == "directd":
		try:
			fetch_fgts_regularidade(digits, token)
			return {"ok": True, "message": _("Connected to the DirectD API successfully!")}
		except (DirectDError, ValueError) as e:
			frappe.throw(_("DirectD connection test failed: {0}").format(str(e)))


def _log_premium_bureau_query(
	bureau: str,
	organization_name: str | None,
	deal_name: str | None,
	cnpj: str,
	price_reference: float,
	status: str,
	raw: dict[str, Any],
	pre_filter_verdict: str,
	warning_acknowledged: bool,
) -> None:
	"""Grava a auditoria da consulta ao bureau final: quem pediu, o pré-filtro mostrado, se o
	operador confirmou um alerta e o preço de tabela do provedor (cobrado na conta do cliente; a
	CredTech não cobra nada, por isso `client_price` fica 0)."""
	frappe.get_doc({
		"doctype": "Cred Hub Premium Bureau Query",
		"bureau": bureau,
		"organization": organization_name,
		"deal": deal_name,
		"cnpj": cnpj,
		"requested_by": frappe.session.user,
		"requested_on": now_datetime(),
		"pre_filter_verdict": pre_filter_verdict,
		"warning_acknowledged": 1 if warning_acknowledged else 0,
		"cost": price_reference or 0.0,
		"client_price": 0.0,
		"status": status,
		"raw_response_json": json.dumps(raw, ensure_ascii=False),
	}).insert(ignore_permissions=True)


def _final_bureau_context(deal_name: str | None, organization_name: str | None) -> dict[str, Any]:
	"""Resolve o card, a Organização, o CNPJ e o pré-filtro atual para uma consulta a bureau final.
	Pode vir de um Card OU direto da Organização (a análise de crédito é da empresa) — passe pelo
	menos um dos dois."""
	if not deal_name and not organization_name:
		frappe.throw(_("Inform a deal or an organization."))
	_check_deal_and_org_permission(deal_name, organization_name)

	deal = frappe.get_doc("CRM Deal", deal_name) if deal_name else None
	if deal and not organization_name:
		organization_name = deal.organization
	if not organization_name:
		frappe.throw(_("The deal must be linked to an organization with a valid CNPJ."))

	org = frappe.db.get_value(
		"CRM Organization", organization_name, ["cnpj", "credit_verdict", "credit_report"], as_dict=True
	) or {}
	if not org.get("cnpj"):
		frappe.throw(_("The organization needs a valid CNPJ."))

	# O veredito do card (se veio de um) tem prioridade; sem card, usa o da própria Organização.
	verdict = (deal.get("credit_verdict") if deal else None) or org.get("credit_verdict") or "Not Checked"
	report_name = (deal.get("credit_report") if deal else None) or org.get("credit_report")
	summary = frappe.db.get_value("Cred Hub Credit Report", report_name, "verdict_summary") if report_name else ""
	return {
		"deal": deal,
		"organization": organization_name,
		"cnpj": org["cnpj"],
		"verdict": verdict,
		"summary": summary or "",
	}


def _final_bureaus(settings_doc) -> list[dict[str, Any]]:
	"""Bureaus finais que a empresa ligou, com o preço de tabela (só referência: quem cobra é o
	provedor, na conta do cliente) e se a credencial já está cadastrada."""
	bureaus: list[dict[str, Any]] = []
	if settings_doc.get("serasa_enabled"):
		bureaus.append({
			"key": "serasa",
			"label": "Serasa Experian (Relato PJ)",
			"provider": "Serasa Experian",
			"price_reference": None,  # depende do contrato do cliente com a Serasa
			"configured": bool(settings_doc.get("serasa_endpoint_url") and settings_doc.get("serasa_token")),
		})
	has_directd_token = bool(settings_doc.get("directd_token"))
	for registry in PREMIUM_BUREAUS.values():
		if settings_doc.get(registry.enabled_field):
			bureaus.append({
				"key": registry.key,
				"label": registry.label,
				"provider": "DirectD",
				"price_reference": registry.price_reference,
				"configured": has_directd_token,
			})
	return bureaus


@frappe.whitelist()
def get_final_bureau_options(deal_name: str | None = None, organization_name: str | None = None) -> dict[str, Any]:
	"""O que a tela mostra antes de uma consulta a bureau final (credtech#316, transparência): os
	bureaus ligados, o preço de tabela de cada um (cobrado pelo provedor na conta do cliente) e o
	resultado do pré-filtro com o motivo, para o operador decidir."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _final_bureau_context(deal_name, organization_name)
	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	return {
		"verdict": ctx["verdict"],
		"verdict_summary": ctx["summary"],
		"needs_acknowledgement": pre_filter_needs_acknowledgement(ctx["verdict"]),
		"can_request": bool(set(frappe.get_roles()) & set(FINAL_BUREAU_ROLES)),
		"bureaus": _final_bureaus(settings_doc),
	}


@frappe.whitelist()
def request_bureau_final(
	deal_name: str | None = None,
	organization_name: str | None = None,
	bureau: str = "serasa",
	confirm_cost: bool = False,
	acknowledged_warning: bool = False,
) -> dict[str, Any]:
	"""Consulta um bureau final (Serasa ou Boa Vista / Dossiê QUOD / SCR Bacen pela DirectD) com a
	chave do próprio cliente — a CredTech não cobra nada (credtech#316).

	Transparência: nada é consultado sem `confirm_cost` (a tela mostrou o preço de tabela e que
	quem cobra é o provedor, na conta do cliente). Pré-filtro diferente de 'Pre-Approved' não
	bloqueia mais: exige `acknowledged_warning` (a tela mostrou o resultado e o motivo), e a
	confirmação fica na auditoria e no comentário da linha do tempo."""
	frappe.only_for(list(FINAL_BUREAU_ROLES))
	ctx = _final_bureau_context(deal_name, organization_name)
	settings_doc = frappe.get_single("Cred Hub Credit Settings")

	options = {item["key"]: item for item in _final_bureaus(settings_doc)}
	option = options.get(bureau)
	if not option:
		if bureau == "serasa" or bureau in PREMIUM_BUREAUS:
			frappe.throw(_("This bureau is not enabled in the Credit settings."))
		frappe.throw(_("Unknown bureau: {0}").format(bureau))
	if not option["configured"]:
		frappe.throw(_("{0}: no API credential configured in the Credit settings.").format(option["label"]))
	if not _truthy(confirm_cost):
		frappe.throw(
			_("This query is paid and billed by {0} on your account. Confirm the cost to continue.").format(option["provider"])
		)

	verdict = ctx["verdict"]
	needs_ack = pre_filter_needs_acknowledgement(verdict)
	if needs_ack and not _truthy(acknowledged_warning):
		frappe.throw(
			_("The pre-filter result for this organization is {0}. Review the alert and confirm to query anyway.").format(_(verdict))
		)

	deal = ctx["deal"]
	organization_name = ctx["organization"]
	org_cnpj = ctx["cnpj"]

	if bureau == "serasa":
		endpoint_url = settings_doc.get("serasa_endpoint_url") or ""
		serasa_token = settings_doc.get_password("serasa_token", raise_exception=False) or ""
		try:
			result = trigger_serasa_relato(org_cnpj, frappe.session.user, endpoint_url, serasa_token)
		except SerasaNotConfigured as e:
			frappe.throw(str(e))
		except SerasaError as e:
			frappe.throw(_("Serasa query failed: {0}").format(str(e)))
		status = result.get("status") or ""
		raw = result
	else:
		registry = PREMIUM_BUREAUS[bureau]
		token = settings_doc.get_password("directd_token", raise_exception=False) or ""
		try:
			result = registry.fetch(org_cnpj, token)
		except (DirectDAuthError, DirectDError, ValueError) as e:
			frappe.throw(_("{0} query failed: {1}").format(registry.label, str(e)))
		status = "Concluído"
		raw = result.get("raw") or result

	price_reference = option["price_reference"]
	_log_premium_bureau_query(
		bureau, organization_name, deal.name if deal else None, org_cnpj, price_reference or 0.0, status, raw, verdict, needs_ack
	)

	price_text = f"R$ {price_reference:.2f}" if price_reference else _("as per your contract")
	message = _("Final bureau query ({0}) requested by {1}. Billed by {2} on the client's account (list price: {3}).").format(
		option["label"], frappe.session.user, option["provider"], price_text
	)
	if needs_ack:
		message += " " + _("Queried even with the pre-filter alert: {0}.").format(_(verdict))
	comment_target = deal or frappe.get_doc("CRM Organization", organization_name)
	comment_target.add_comment("Comment", message)

	return {
		"status": status,
		"bureau": option["label"],
		"price_reference": price_reference,
		"warning_acknowledged": needs_ack,
		"data": result,
	}
