"""Worker e orquestrador de segundo plano da esteira de crédito e compliance.

Executa consultas assíncronas nos provedores habilitados (Escavador, Infosimples e outros
que venham a ser plugados — ver `_build_providers`), aplica regras de corte, grava o DocType
'Cred Hub Credit Report', atualiza a Organização e o Card, e posta o resumo na linha do tempo.

Regra de segurança (fail-closed): se um provedor habilitado estiver sem credencial ou a
chamada falhar, o parecer final é sempre 'Error' — nunca 'Pre-Approved' por omissão de dado.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import frappe
from frappe import _
from frappe.utils import add_days, today

from cred_hub.cnpj.mapping import format_cnpj, is_valid_cnpj, only_digits
from cred_hub.credit_check.clients.cgu import CGUError, fetch_ceis_cnep
from cred_hub.credit_check.clients.directd import (
	fetch_fgts_regularidade,
	fetch_lawsuits_directd,
	fetch_protests_nacional,
	fetch_score,
	fetch_sintegra_ccc,
	merge_lawsuit_sources,
	merge_protest_sources,
)
from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import fetch_fiscal_and_protests
from cred_hub.credit_check.rules import (
	VERDICT_ERROR,
	evaluate_credit_risk,
	format_timeline_summary,
)

CACHE_DAYS = 30
ESCAVADOR_ESTIMATED_COST = 0.50
# Infosimples cobra R$0,20 por sub-consulta (confirmado, 30/10/2026). fetch_fiscal_and_protests()
# sempre chama as 4 (PGFN, CND Federal, CNDT, CENPROT-SP), então o custo real por checagem é 4x isso.
INFOSIMPLES_ESTIMATED_COST = 0.80
DIRECTD_SCORE_ESTIMATED_COST = 1.98
DIRECTD_PROTESTS_ESTIMATED_COST = 2.95
DIRECTD_PROCESSOS_ESTIMATED_COST = 1.50
DIRECTD_FGTS_ESTIMATED_COST = 0.36
DIRECTD_SINTEGRA_ESTIMATED_COST = 1.80
# Markup fixo sobre o custo da esteira automática (decisão do dono, 30/10/2026). Os bureaus
# Premium (Boa Vista, Dossiê QUOD, SCR Bacen) têm preço fixo próprio — ver premium_bureaus.py.
CLIENT_PRICE_MULTIPLIER = 1.20

EMPTY_ESCAVADOR_DATA = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
EMPTY_INFOSIMPLES_DATA = {
	"pgfn": {"total_debt": 0.0, "status": "Regular", "inscriptions_count": 0},
	"cnd_federal": {"is_regular": True, "status": "Regular", "pdf_url": ""},
	"cndt": {"is_regular": True, "status": "Regular", "pdf_url": ""},
	"cenprot": {"has_protests": False, "protests_count": 0},
}
EMPTY_DIRECTD_SCORE_DATA = {"score": None, "faixa_score": "", "motivos": [], "indicadores_negocio": []}
EMPTY_DIRECTD_PROTESTS_DATA = {"has_protests": False, "protests_count": 0, "total_value": 0.0, "cartorios_distintos": 0, "cidades_distintas": 0}
EMPTY_FGTS_DATA = {"is_regular": True, "status": "Regular", "certificado_numero": "", "raw": {}}
EMPTY_SINTEGRA_DATA = {"situacao_cadastral": "", "situacao_cnpj": "", "inscricoes_estaduais": [], "raw": {}}
EMPTY_CGU_DATA = {"sanctioned": False, "ceis_count": 0, "cnep_count": 0, "sanctions": [], "raw": {}}


@dataclass
class ProviderCheck:
	"""Um provedor plugável da esteira (Escavador, Infosimples e futuros — Assertiva/CENPROT/Serasa
	real quando entrarem no épico #290). Cada provedor é independente: habilitar um não exige os
	outros, e a falha de um nunca é mascarada pelos demais."""

	key: str
	label: str
	enabled: bool
	token: str
	fetch: Callable[[str, str], dict[str, Any]]
	empty_result: dict[str, Any]
	estimated_cost: float
	data_slot: str  # onde o resultado entra no dicionário consolidado


def get_credit_settings() -> dict[str, Any]:
	"""Lê as configurações de bureau e análise de risco do tenant."""
	if not frappe.db.exists("DocType", "Cred Hub Credit Settings"):
		return {"enabled": False}

	doc = frappe.get_single("Cred Hub Credit Settings")
	return {
		"enabled": bool(doc.get("enabled")),
		"cgu_enabled": bool(doc.get("cgu_enabled")),
		"cgu_api_key": doc.get_password("cgu_api_key") if doc.get("cgu_api_key") else "",
		"block_on_ceis_cnep": bool(doc.get("block_on_ceis_cnep", 1)),
		"escavador_enabled": bool(doc.get("escavador_enabled")),
		"escavador_token": doc.get_password("escavador_token") if doc.get("escavador_token") else "",
		"infosimples_enabled": bool(doc.get("infosimples_enabled")),
		"infosimples_token": doc.get_password("infosimples_token") if doc.get("infosimples_token") else "",
		"directd_enabled": bool(doc.get("directd_enabled")),
		"directd_token": doc.get_password("directd_token") if doc.get("directd_token") else "",
		"min_directd_score": int(doc.get("min_directd_score") or 0),
		"directd_fgts_enabled": bool(doc.get("directd_fgts_enabled")),
		"directd_sintegra_enabled": bool(doc.get("directd_sintegra_enabled")),
		"auto_trigger_stage": doc.get("auto_trigger_stage") or "",
		"check_second_organization": bool(doc.get("check_second_organization")),
		"max_lawsuits_defendant": int(doc.get("max_lawsuits_defendant") or 0),
		"max_lawsuit_value": float(doc.get("max_lawsuit_value") or 0.0),
		"block_on_bankruptcy": bool(doc.get("block_on_bankruptcy", 1)),
		"max_pgfn_debt": float(doc.get("max_pgfn_debt") or 0.0),
		"block_on_cndt": bool(doc.get("block_on_cndt", 1)),
		"block_on_protest": bool(doc.get("block_on_protest", 1)),
		"block_on_fgts_irregular": bool(doc.get("block_on_fgts_irregular", 1)),
	}


def _build_providers(settings: dict[str, Any]) -> list[ProviderCheck]:
	"""Monta a lista de provedores habilitados nesta empresa. Adicionar um novo provedor
	(ex.: Serasa automático, CENPROT direto) é só acrescentar um `ProviderCheck` aqui — o
	orquestrador abaixo trata todos de forma genérica e sem fail-open."""
	return [
		ProviderCheck(
			key="escavador",
			label="Escavador",
			enabled=bool(settings.get("escavador_enabled")),
			token=settings.get("escavador_token") or "",
			fetch=fetch_lawsuits,
			empty_result=EMPTY_ESCAVADOR_DATA,
			estimated_cost=ESCAVADOR_ESTIMATED_COST,
			data_slot="escavador",
		),
		ProviderCheck(
			key="infosimples",
			label="Infosimples",
			enabled=bool(settings.get("infosimples_enabled")),
			token=settings.get("infosimples_token") or "",
			fetch=fetch_fiscal_and_protests,
			empty_result=EMPTY_INFOSIMPLES_DATA,
			estimated_cost=INFOSIMPLES_ESTIMATED_COST,
			data_slot="infosimples",
		),
		# DirectD — só os 3 produtos de Crédito/Processos pedidos pelo dono (29/10/2026), não o
		# catálogo inteiro. Um único toggle/token habilita os três.
		ProviderCheck(
			key="directd_score",
			label="DirectD Score (QUOD)",
			enabled=bool(settings.get("directd_enabled")),
			token=settings.get("directd_token") or "",
			fetch=fetch_score,
			empty_result=EMPTY_DIRECTD_SCORE_DATA,
			estimated_cost=DIRECTD_SCORE_ESTIMATED_COST,
			data_slot="directd_score",
		),
		ProviderCheck(
			key="directd_protestos",
			label="DirectD Protestos Nacional",
			enabled=bool(settings.get("directd_enabled")),
			token=settings.get("directd_token") or "",
			fetch=fetch_protests_nacional,
			empty_result=EMPTY_DIRECTD_PROTESTS_DATA,
			estimated_cost=DIRECTD_PROTESTS_ESTIMATED_COST,
			data_slot="directd_protestos",
		),
		ProviderCheck(
			key="directd_processos",
			label="DirectD Processos Judiciais",
			enabled=bool(settings.get("directd_enabled")),
			token=settings.get("directd_token") or "",
			fetch=fetch_lawsuits_directd,
			empty_result=EMPTY_ESCAVADOR_DATA,
			estimated_cost=DIRECTD_PROCESSOS_ESTIMATED_COST,
			data_slot="directd_processos",
		),
		ProviderCheck(
			key="directd_fgts",
			label="DirectD FGTS",
			enabled=bool(settings.get("directd_enabled")) and bool(settings.get("directd_fgts_enabled")),
			token=settings.get("directd_token") or "",
			fetch=fetch_fgts_regularidade,
			empty_result=EMPTY_FGTS_DATA,
			estimated_cost=DIRECTD_FGTS_ESTIMATED_COST,
			data_slot="directd_fgts",
		),
		ProviderCheck(
			key="directd_sintegra",
			label="DirectD Sintegra/CCC",
			enabled=bool(settings.get("directd_enabled")) and bool(settings.get("directd_sintegra_enabled")),
			token=settings.get("directd_token") or "",
			fetch=fetch_sintegra_ccc,
			empty_result=EMPTY_SINTEGRA_DATA,
			estimated_cost=DIRECTD_SINTEGRA_ESTIMATED_COST,
			data_slot="directd_sintegra",
		),
	]


def find_valid_cached_report(org_name: str) -> str | None:
	"""Localiza um relatório de crédito válido (não expirado) para a Organização."""
	today_date = today()
	reports = frappe.get_all(
		"Cred Hub Credit Report",
		filters={"organization": org_name, "valid_until": (">=", today_date)},
		fields=["name", "verdict"],
		order_by="checked_on DESC",
		limit=1,
	)
	if reports and reports[0].get("verdict") != "Error":
		return reports[0].name
	return None


def run_background_check(
	deal_name: str | None = None,
	organization_name: str | None = None,
	force: bool = False,
	user: str | None = None,
) -> str | None:
	"""Executa a triagem jurídica e fiscal da organização ligada."""
	settings = get_credit_settings()
	if not settings.get("enabled"):
		return None

	org_doc = None
	deal_doc = None

	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		deal_doc = frappe.get_doc("CRM Deal", deal_name)
		if not organization_name and deal_doc.organization:
			organization_name = deal_doc.organization

	if organization_name and frappe.db.exists("CRM Organization", organization_name):
		org_doc = frappe.get_doc("CRM Organization", organization_name)

	if not org_doc or not org_doc.get("cnpj"):
		return None

	cnpj = org_doc.cnpj
	digits = only_digits(cnpj)
	if not is_valid_cnpj(cnpj):
		return None

	# Conferência de cache (30 dias) — nunca reaproveita um relatório 'Error'
	if not force:
		cached_report_name = find_valid_cached_report(org_doc.name)
		if cached_report_name:
			_apply_report_to_deal_and_org(cached_report_name, org_doc, deal_doc, is_cached=True)
			return cached_report_name

	# Etapa 0 — Sanções CEIS/CNEP (CGU, grátis). Roda antes e separada do loop genérico: se achar
	# sanção e o cliente configurou pra reprovar, nem chama os provedores pagos (economiza o custo
	# inteiro). Exceção deliberada ao fail-closed: falha do governo nunca trava o resto da esteira
	# nem vira 'Error' geral — só fica "não verificado" nesse campo específico.
	cgu_data: dict[str, Any] | None = None
	skip_paid_providers = False
	if settings.get("cgu_enabled"):
		cgu_api_key = settings.get("cgu_api_key")
		if cgu_api_key:
			try:
				cgu_data = fetch_ceis_cnep(digits, cgu_api_key)
				if cgu_data.get("sanctioned") and settings.get("block_on_ceis_cnep", True):
					skip_paid_providers = True
			except CGUError as e:
				frappe.log_error(title=f"Cred Hub: CGU CEIS/CNEP falhou para CNPJ {cnpj}", message=str(e))
		else:
			frappe.log_error(title=f"Cred Hub: CGU habilitado sem chave de API para CNPJ {cnpj}", message="cgu_enabled=1 mas cgu_api_key vazio")

	# Chamada aos provedores externos habilitados — cada um isolado, sem fail-open
	providers = _build_providers(settings)
	enabled_providers = [] if skip_paid_providers else [p for p in providers if p.enabled]

	results: dict[str, Any] = {p.data_slot: p.empty_result for p in providers}
	failures: list[str] = []
	total_cost = 0.0

	if not enabled_providers and not skip_paid_providers:
		failures.append(_("no provider is enabled in Credit & Legal Bureaus settings"))
	else:
		for provider in enabled_providers:
			if not provider.token.strip():
				failures.append(_("{0}: no API token configured").format(provider.label))
				continue
			try:
				results[provider.data_slot] = provider.fetch(digits, provider.token)
				total_cost += provider.estimated_cost
			except Exception as e:
				frappe.log_error(
					title=f"Cred Hub: {provider.label} API falhou para CNPJ {cnpj}",
					message=str(e),
				)
				failures.append(_("{0}: query failed ({1})").format(provider.label, str(e)[:150]))

	# Combina as fontes de processos e de protestos (Escavador+DirectD, Infosimples+DirectD) da
	# forma mais conservadora, antes de avaliar o risco — nunca perde um sinal que só uma das
	# fontes tenha encontrado.
	merged_lawsuits = merge_lawsuit_sources(results["escavador"], results["directd_processos"])
	merged_infosimples = dict(results["infosimples"])
	merged_infosimples["cenprot"] = merge_protest_sources(
		results["infosimples"].get("cenprot") or {}, results["directd_protestos"]
	)
	merged_infosimples["fgts"] = results["directd_fgts"]

	# Avaliação de risco — qualquer falha num provedor habilitado força o parecer 'Error'
	evaluation = evaluate_credit_risk(
		merged_lawsuits,
		merged_infosimples,
		settings,
		provider_failures=failures,
		directd_score_data=results["directd_score"],
		cgu_data=cgu_data,
	)
	verdict = evaluation.get("verdict") or VERDICT_ERROR
	summary = evaluation.get("summary") or ""
	metrics = evaluation.get("metrics") or {}

	# Gravação do DocType 'Cred Hub Credit Report'
	today_date = today()
	# Um parecer 'Error' nunca fica em cache: a próxima tentativa deve consultar de novo
	valid_until_date = today_date if verdict == VERDICT_ERROR else add_days(today_date, CACHE_DAYS)

	if verdict == VERDICT_ERROR:
		pgfn_status = "Não verificado"
	elif (metrics.get("pgfn_debt") or 0.0) > 0:
		pgfn_status = "Inscrito na Dívida Ativa"
	else:
		pgfn_status = "Regular"

	report_doc = frappe.get_doc({
		"doctype": "Cred Hub Credit Report",
		"organization": org_doc.name,
		"deal": deal_doc.name if deal_doc else None,
		"cnpj": format_cnpj(digits),
		"checked_on": today_date,
		"valid_until": valid_until_date,
		"verdict": verdict,
		"verdict_summary": summary,
		"ceis_cnep_sanctioned": 1 if metrics.get("ceis_cnep_sanctioned") else 0,
		"ceis_cnep_verified": 1 if metrics.get("ceis_cnep_verified") else 0,
		"lawsuits_total": metrics.get("lawsuits_total") or 0,
		"lawsuits_defendant": metrics.get("lawsuits_defendant") or 0,
		"lawsuits_highest_value": metrics.get("lawsuits_highest_value") or 0.0,
		"has_bankruptcy_recovery": 1 if metrics.get("has_bankruptcy") else 0,
		"pgfn_status": pgfn_status,
		"pgfn_total_debt": metrics.get("pgfn_debt") or 0.0,
		"cnd_federal_status": metrics.get("cnd_federal_status", "Regular"),
		"cnd_federal_pdf_url": metrics.get("cnd_federal_pdf", ""),
		"cndt_status": metrics.get("cndt_status", "Regular"),
		"cndt_pdf_url": metrics.get("cndt_pdf", ""),
		"cenprot_has_protests": 1 if metrics.get("cenprot_has_protest") else 0,
		"cenprot_protests_count": metrics.get("cenprot_protests_count") or 0,
		"fgts_status": metrics.get("fgts_status") or "Regular",
		"sintegra_situacao_cadastral": results["directd_sintegra"].get("situacao_cadastral") or "",
		"total_cost": total_cost,
		"client_total_cost": round(total_cost * CLIENT_PRICE_MULTIPLIER, 2),
		"raw_details_json": json.dumps(
			{
				"cgu": cgu_data,
				"escavador": results["escavador"],
				"infosimples": results["infosimples"],
				"directd_score": results["directd_score"],
				"directd_protestos": results["directd_protestos"],
				"directd_processos": results["directd_processos"],
				"directd_fgts": results["directd_fgts"],
				"directd_sintegra": results["directd_sintegra"],
				"failures": failures,
			},
			ensure_ascii=False,
		),
	})
	report_doc.insert(ignore_permissions=True)

	# Atualização da Organização e Deal + Comentários na timeline
	_apply_report_to_deal_and_org(report_doc.name, org_doc, deal_doc, is_cached=False, evaluation=evaluation)

	return report_doc.name


def _apply_report_to_deal_and_org(
	report_name: str,
	org_doc,
	deal_doc,
	is_cached: bool = False,
	evaluation: dict[str, Any] | None = None,
):
	"""Associa o relatório à Organização e ao Card, registrando a atividade."""
	report = frappe.get_doc("Cred Hub Credit Report", report_name)
	verdict = report.verdict

	# Atualiza Organização
	org_doc.db_set("credit_verdict", verdict, update_modified=False)
	org_doc.db_set("credit_report", report.name, update_modified=False)

	# Se for deal
	if deal_doc:
		deal_doc.db_set("credit_verdict", verdict, update_modified=False)
		deal_doc.db_set("credit_report", report.name, update_modified=False)

		# Comentário na linha do tempo
		if not is_cached and evaluation:
			timeline_text = format_timeline_summary(evaluation, org_doc.cnpj)
			deal_doc.add_comment("Comment", timeline_text)
		elif is_cached:
			deal_doc.add_comment(
				"Comment",
				_("Compliance report reused from cache (checked on {0}, valid until {1}): verdict {2}.").format(
					report.checked_on, report.valid_until, verdict
				),
			)
