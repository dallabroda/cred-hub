"""APIs REST (Frappe Whitelist) para telas de Configurações e Cards do CRM."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.clients.directd import DirectDAuthError, DirectDError, fetch_fgts_regularidade
from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import InfosimplesError, query_pgfn
from cred_hub.credit_check.clients.serasa import (
	DEFAULT_REPORT,
	SerasaClient,
	SerasaDocumentNotFound,
	SerasaError,
	SerasaNotConfigured,
)
from cred_hub.credit_check.enrich import get_credit_settings, run_background_check
from cred_hub.credit_check import dossier as credit_dossier
from cred_hub.credit_check import opinion as credit_opinion
from cred_hub.credit_check import package as credit_package
from cred_hub.credit_check import policy as credit_policy
from cred_hub.credit_check import partners as partners_lib
from cred_hub.credit_check import onboarding as credit_onboarding
from cred_hub.credit_check import template as credit_template
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
def trigger_check(
	deal_name: str | None = None,
	organization_name: str | None = None,
	force: bool = False,
	tier_1_only: bool = False,
) -> dict[str, Any]:
	"""Dispara a esteira manualmente a partir da interface do Card ou Organização."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(deal_name, organization_name)

	org_id = organization_name
	if not org_id and deal_name and frappe.db.exists("CRM Deal", deal_name):
		org_id = frappe.db.get_value("CRM Deal", deal_name, "organization")

	if org_id and not _truthy(force) and not getattr(frappe.flags, "in_test", False):
		from cred_hub.credit_check.enrich import find_valid_cached_report
		cached_name = find_valid_cached_report(org_id)
		if cached_name:
			reuse_days = credit_package.config().get("reuse_days", 30)
			rep = frappe.db.get_value("Cred Hub Credit Report", cached_name, ["checked_on", "valid_until"], as_dict=True) or {}
			return {
				"cached": True,
				"report_name": cached_name,
				"checked_on": str(rep.get("checked_on") or ""),
				"valid_until": str(rep.get("valid_until") or ""),
				"reuse_days": reuse_days,
				"message": _(
					"Esta empresa já possui triagem válida realizada em {0}. O prazo configurado para novas consultas é de {1} dias (válido até {2} — alterável em Configurações > Bureaus e Crédito). Nenhuma consulta duplicada foi cobrada."
				).format(
					frappe.utils.format_date(rep.get("checked_on")),
					reuse_days,
					frappe.utils.format_date(rep.get("valid_until")),
				),
			}

	job = frappe.enqueue(
		"cred_hub.credit_check.enrich.run_background_check",
		queue="long",
		job_id=f"manual_credit_check::{deal_name or organization_name}",
		deduplicate=True,
		deal_name=deal_name,
		organization_name=organization_name,
		force=bool(force),
		user=frappe.session.user,
		tier_1_only=_truthy(tier_1_only),
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
		"directd_sintegra_uf": enrich.DIRECTD_SINTEGRA_UF_ESTIMATED_COST,
		**{key: registry.price_reference for key, registry in PREMIUM_BUREAUS.items()},
		"test_connection": TEST_CONNECTION_COST,
	}


def _verdict_label(verdict: str) -> str:
	"""Nome do resultado do pré-filtro para mensagens ao operador. 'Error' (um provedor falhou) não
	traduz como a palavra genérica 'Error', que valeria pra o app inteiro."""
	if verdict == "Error":
		return _("Verification failed")
	return _(verdict)


_SERASA_TOKEN_KEY = "cred_hub:serasa_token"


def _serasa_report(settings_doc, cnpj: str) -> dict[str, Any]:
	"""Consulta o Relatório Avançado PJ na Serasa com o contrato do cliente. O token (vale cerca de 1 hora) fica
	em cache por credencial e ambiente, para não entrar de novo a cada consulta."""
	client_id = settings_doc.get("serasa_client_id") or ""
	environment = settings_doc.get("serasa_environment") or "Production"
	secret = settings_doc.get_password("serasa_client_secret", raise_exception=False) or ""
	key = f"{_SERASA_TOKEN_KEY}:{environment}:{hashlib.sha256(client_id.encode()).hexdigest()[:16]}"
	cached = frappe.cache.get_value(key) or {}
	client = SerasaClient(
		client_id,
		secret,
		environment,
		token=cached.get("token", ""),
		token_expires=cached.get("expires", 0),
	)
	try:
		return client.fetch_report(cnpj, settings_doc.get("serasa_report_name") or DEFAULT_REPORT)
	finally:
		if client.token:
			frappe.cache.set_value(key, {"token": client.token, "expires": client.token_expires}, expires_in_sec=3300)


@frappe.whitelist()
def test_serasa_connection(client_id: str = "", client_secret: str = "", environment: str = "Production") -> dict[str, Any]:
	"""Confere o client id e o client secret do contrato Serasa só entrando na API (login). Não faz consulta
	nenhuma, então não custa nada. Sem valor digitado (ou com o segredo salvo, que volta mascarado), usa o salvo."""
	frappe.only_for("System Manager")
	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	client_id = (client_id or "").strip() or (settings_doc.get("serasa_client_id") or "")
	client_secret = (client_secret or "").strip()
	if not client_secret or _is_masked(client_secret):
		client_secret = settings_doc.get_password("serasa_client_secret", raise_exception=False) or ""
	try:
		SerasaClient(client_id, client_secret, environment or "Production").login()
	except SerasaNotConfigured as e:
		frappe.throw(str(e))
	except SerasaError as e:
		frappe.throw(_("Serasa connection test failed: {0}").format(str(e)))
	return {"ok": True, "message": _("Connected to Serasa successfully! This test makes no paid query.")}


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
	document: str = "",
	contact_name: str | None = None,
) -> None:
	"""Grava a auditoria da consulta ao bureau final: quem pediu, o pré-filtro mostrado, se o
	operador confirmou um alerta e o preço de tabela do provedor (cobrado na conta do cliente; a
	CredTech não cobra nada, por isso `client_price` fica 0). `document` é o CPF ou CNPJ de quem foi
	consultado; com `contact_name` a consulta é de um sócio, e `cnpj` guarda o da empresa."""
	frappe.get_doc({
		"doctype": "Cred Hub Premium Bureau Query",
		"bureau": bureau,
		"organization": organization_name,
		"deal": deal_name,
		"subject_type": "Partner" if contact_name else "Organization",
		"contact": contact_name,
		"document": document or cnpj,
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
			"label": "Serasa Experian (Relatório Avançado PJ)",
			"provider": "Serasa Experian",
			"price_reference": None,  # depende do contrato do cliente com a Serasa
			"configured": bool(settings_doc.get("serasa_client_id") and settings_doc.get("serasa_client_secret")),
			"partner_ok": False,  # Relato PJ é só de empresa
			"tier": 3,
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
				"partner_ok": registry.key in partners_lib.PARTNER_BUREAUS,
				"tier": registry.tier,
			})
	return bureaus


class FinalQueryError(Exception):
	"""O provedor não respondeu a consulta; a mensagem é para o operador."""


def _execute_final_query(settings_doc, bureau: str, document: str) -> tuple[dict[str, Any], dict[str, Any]]:
	"""Faz uma consulta a bureau final com a chave do cliente. Devolve (retorno bruto para a auditoria, dados para a
	tela). Falha do provedor vira FinalQueryError."""
	if bureau == "serasa":
		try:
			result = _serasa_report(settings_doc, document)
		except SerasaNotConfigured as e:
			raise FinalQueryError(str(e)) from e
		except SerasaDocumentNotFound as e:
			raise FinalQueryError(_("Serasa does not have this document: {0}").format(str(e))) from e
		except (SerasaError, ValueError) as e:
			raise FinalQueryError(_("Serasa query failed: {0}").format(str(e))) from e
		return result, result
	registry = PREMIUM_BUREAUS[bureau]
	token = settings_doc.get_password("directd_token", raise_exception=False) or ""
	try:
		result = registry.fetch(document, token)
	except (DirectDAuthError, DirectDError, ValueError) as e:
		raise FinalQueryError(_("{0} query failed: {1}").format(registry.label, str(e))) from e
	return (result.get("raw") or result), result


@frappe.whitelist()
def get_final_bureau_options(deal_name: str | None = None, organization_name: str | None = None) -> dict[str, Any]:
	"""O que a tela mostra antes de uma consulta a bureau final (credtech#316, transparência): os
	bureaus ligados, o preço de tabela de cada um (cobrado pelo provedor na conta do cliente), o
	resultado do pré-filtro e as consultas já feitas dentro do prazo de validade (reuse_days)."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _final_bureau_context(deal_name, organization_name)
	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	pkg_conf = credit_package.config(settings_doc)
	reuse_days = pkg_conf.get("reuse_days", 30)
	bureaus = _final_bureaus(settings_doc)

	if reuse_days > 0:
		since = frappe.utils.add_days(now_datetime(), -reuse_days)
		recent_rows = frappe.get_all(
			"Cred Hub Premium Bureau Query",
			filters={
				"organization": ctx["organization"],
				"status": credit_package.DONE,
				"requested_on": (">=", since),
			},
			fields=["bureau", "document", "contact", "requested_on"],
			order_by="requested_on desc",
			limit_page_length=100,
		)
	else:
		recent_rows = []

	for b in bureaus:
		comp_hit = next((r for r in recent_rows if r.bureau == b["key"] and not r.contact), None)
		if comp_hit:
			b["recent_company_query"] = {
				"requested_on": frappe.utils.format_datetime(comp_hit.requested_on),
				"valid_until": frappe.utils.format_date(frappe.utils.add_days(comp_hit.requested_on, reuse_days)),
				"reuse_days": reuse_days,
			}
		else:
			b["recent_company_query"] = None

	return {
		"verdict": ctx["verdict"],
		"verdict_summary": ctx["summary"],
		"needs_acknowledgement": pre_filter_needs_acknowledgement(ctx["verdict"]),
		"can_request": bool(set(frappe.get_roles()) & set(FINAL_BUREAU_ROLES)),
		"reuse_days": reuse_days,
		"bureaus": bureaus,
	}


@frappe.whitelist()
def request_bureau_final(
	deal_name: str | None = None,
	organization_name: str | None = None,
	bureau: str = "serasa",
	confirm_cost: bool = False,
	acknowledged_warning: bool = False,
	contact_name: str | None = None,
	force_requery: bool = False,
) -> dict[str, Any]:
	"""Consulta um bureau final (Serasa ou Boa Vista / Dossiê QUOD / SCR Bacen pela DirectD) com a
	chave do próprio cliente — a CredTech não cobra nada (credtech#316)."""
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
			_("The pre-filter result for this organization is {0}. Review the alert and confirm to query anyway.").format(_verdict_label(verdict))
		)

	deal = ctx["deal"]
	organization_name = ctx["organization"]
	org_cnpj = ctx["cnpj"]

	# Quem é consultado: a empresa (CNPJ) ou um sócio (CPF do Contato)
	document, subject_name = org_cnpj, ""
	if contact_name:
		if not option.get("partner_ok"):
			frappe.throw(_("{0} only accepts companies (CNPJ), not partners (CPF).").format(option["label"]))
		document, subject_name = partners_lib.cpf_digits(organization_name, contact_name)

	# Proteção contra consulta duplicada dentro do prazo configurável (padrão 30 dias)
	reuse_days = credit_package.config(settings_doc).get("reuse_days", 30)
	if reuse_days > 0 and not _truthy(force_requery) and not getattr(frappe.flags, "in_test", False):
		since = frappe.utils.add_days(now_datetime(), -reuse_days)
		recent_hits = frappe.get_all(
			"Cred Hub Premium Bureau Query",
			filters={
				"organization": organization_name,
				"bureau": bureau,
				"document": document,
				"status": credit_package.DONE,
				"requested_on": (">=", since),
			},
			fields=["name", "requested_on"],
			order_by="requested_on desc",
			limit=1,
		)
		if recent_hits:
			last_dt = recent_hits[0].requested_on
			valid_dt = frappe.utils.add_days(last_dt, reuse_days)
			frappe.throw(
				_(
					"A consulta '{0}' já foi realizada em {1}. O prazo para nova consulta sem cobrança duplicada é de {2} dias (válido até {3} — você pode alterar esse prazo em Configurações > Bureaus e Crédito)."
				).format(
					option["label"],
					frappe.utils.format_datetime(last_dt),
					reuse_days,
					frappe.utils.format_date(valid_dt),
				)
			)

	try:
		raw, result = _execute_final_query(settings_doc, bureau, org_cnpj if bureau == "serasa" else document)
	except FinalQueryError as e:
		frappe.throw(str(e))
	status = credit_package.DONE

	price_reference = option["price_reference"]
	_log_premium_bureau_query(
		bureau,
		organization_name,
		deal.name if deal else None,
		org_cnpj,
		price_reference or 0.0,
		status,
		raw,
		verdict,
		needs_ack,
		document=document,
		contact_name=contact_name,
	)

	price_text = f"R$ {price_reference:.2f}" if price_reference else _("as per your contract")
	message = _("Final bureau query ({0}) requested by {1}. Billed by {2} on the client's account (list price: {3}).").format(
		option["label"], frappe.session.user, option["provider"], price_text
	)
	if contact_name:
		message += " " + _("Partner consulted: {0}.").format(subject_name)
	if needs_ack:
		message += " " + _("Queried even with the pre-filter alert: {0}.").format(_verdict_label(verdict))
	comment_target = deal or frappe.get_doc("CRM Organization", organization_name)
	comment_target.add_comment("Comment", message)

	return {
		"status": status,
		"bureau": option["label"],
		"price_reference": price_reference,
		"warning_acknowledged": needs_ack,
		"data": result,
	}


@frappe.whitelist()
def get_partners(organization_name: str) -> list[dict[str, Any]]:
	"""Sócios da Organização (Contatos ligados a ela) e se já têm CPF completo. O operador escolhe
	quais consultar; o sócio-administrador ou majoritário vem sinalizado como `is_admin`."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return partners_lib.partners_of(organization_name)


@frappe.whitelist()
def save_partner_cpf(organization_name: str, contact_name: str, cpf: str) -> dict[str, Any]:
	"""Grava o CPF completo de um sócio (a Receita só mostra o CPF mascarado). Só aceita CPF válido."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return partners_lib.save_cpf(organization_name, contact_name, cpf)


def _bureau_label(bureau: str) -> str:
	if bureau == "serasa":
		return "Serasa Experian (Relato PJ)"
	registry = PREMIUM_BUREAUS.get(bureau)
	return registry.label if registry else bureau


@frappe.whitelist()
def get_bureau_results(organization_name: str) -> list[dict[str, Any]]:
	"""Todas as consultas a bureau final feitas para a Organização e os sócios dela, da mais recente
	para a mais antiga, sem o retorno completo (use `get_bureau_result` para abrir um)."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	rows = frappe.get_all(
		"Cred Hub Premium Bureau Query",
		filters={"organization": organization_name},
		fields=[
			"name",
			"bureau",
			"subject_type",
			"contact",
			"document",
			"requested_by",
			"requested_on",
			"pre_filter_verdict",
			"warning_acknowledged",
			"cost",
			"status",
		],
		order_by="requested_on desc",
		limit_page_length=200,
	)
	contact_names = {
		row.name: row.full_name
		for row in frappe.get_all(
			"Contact",
			filters={"name": ("in", [r.contact for r in rows if r.contact] or [""])},
			fields=["name", "full_name"],
		)
	}
	return [
		{
			"name": row.name,
			"bureau": row.bureau,
			"bureau_label": _bureau_label(row.bureau),
			"subject_type": row.subject_type or "Organization",
			"subject_name": contact_names.get(row.contact, "") if row.contact else "",
			"document": row.document or "",
			"requested_by": row.requested_by,
			"requested_on": row.requested_on,
			"pre_filter_verdict": row.pre_filter_verdict or "",
			"warning_acknowledged": bool(row.warning_acknowledged),
			"price_reference": row.cost or 0.0,
			"status": row.status or "",
		}
		for row in rows
	]


@frappe.whitelist()
def get_bureau_result(query_name: str) -> dict[str, Any]:
	"""O retorno completo de uma consulta a bureau final, com `rendered_html` executivo para leitura e `raw` para auditoria."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	if not frappe.db.exists("Cred Hub Premium Bureau Query", query_name):
		frappe.throw(_("Query not found."))
	doc = frappe.get_doc("Cred Hub Premium Bureau Query", query_name)
	_check_deal_and_org_permission(doc.get("deal"), doc.get("organization"))
	try:
		raw = json.loads(doc.raw_response_json or "{}")
	except ValueError:
		raw = {"raw_text": doc.raw_response_json}
	return {
		"name": doc.name,
		"bureau": doc.bureau,
		"bureau_label": _bureau_label(doc.bureau),
		"document": doc.get("document") or doc.get("cnpj") or "",
		"requested_on": doc.requested_on,
		"rendered_html": credit_dossier.render_query_html(doc.bureau, raw),
		"raw": raw,
	}


@frappe.whitelist()
def get_dossier_highlights(organization_name: str) -> dict[str, Any]:
	"""Indicadores executivos consolidados e alertas de fraude extraídos das consultas já feitas para a Organização."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return credit_dossier.summarize_highlights(organization_name)


@frappe.whitelist()
def get_credit_template_status() -> dict[str, Any]:
	"""Se o funil "Crédito PJ" já está instalado e quais etapas ele cria."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	return credit_template.status()


@frappe.whitelist()
def install_credit_template() -> dict[str, Any]:
	"""Instala o funil "Crédito PJ" (6 etapas de análise, um ganho e uma perda, e 4 campos de crédito no Card).
	Só administrador; uma vez por site. Nada é obrigatório por etapa e nada custa: o funil só organiza o trabalho."""
	frappe.only_for("System Manager")
	pipeline = credit_template.install()
	return {"pipeline": pipeline, **credit_template.status()}


@frappe.whitelist()
def generate_credit_dossier(organization_name: str, deal_name: str | None = None) -> dict[str, Any]:
	"""Gera o dossiê (PDF 1): o retorno completo de todas as consultas da empresa e dos sócios, sem recomendação,
	mais o JSON original de cada uma. Anexa na Organização (e no card, se vier de um). Cada geração é um arquivo novo."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(deal_name, organization_name)
	return credit_dossier.generate(organization_name, deal_name)


@frappe.whitelist()
def list_credit_dossiers(organization_name: str) -> list[dict[str, Any]]:
	"""Os dossiês já gerados da Organização (PDF e JSON), do mais novo para o mais antigo."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return credit_dossier.list_files(organization_name)


@frappe.whitelist()
def get_credit_opinion_status(organization_name: str) -> dict[str, Any]:
	"""Quantos pareceres da IA o mês já usou, se o próximo é extra (e quanto custa) e se a IA está configurada."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return credit_opinion.status()


@frappe.whitelist()
def request_credit_opinion(
	organization_name: str, deal_name: str | None = None, confirm_extra_cost: bool = False
) -> dict[str, Any]:
	"""Pede o parecer de crédito da IA (PDF 2). Gera em segundo plano; acompanhe por list_credit_opinions."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(deal_name, organization_name)
	return credit_opinion.request(organization_name, deal_name, confirm_extra_cost)


@frappe.whitelist()
def list_credit_opinions(organization_name: str) -> list[dict[str, Any]]:
	"""Os pareceres da Organização, do mais novo para o mais antigo, com a situação de cada um."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	_check_deal_and_org_permission(None, organization_name)
	return credit_opinion.list_opinions(organization_name)


@frappe.whitelist()
def get_credit_policy() -> dict[str, Any]:
	"""A política de crédito em vigor (ou a padrão) e o texto padrão, para "voltar ao padrão"."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	current = credit_policy.current()
	return {**current, "default_methodology": credit_policy.DEFAULT_METHODOLOGY, "default_parameters": credit_policy.DEFAULT_PARAMETERS}


@frappe.whitelist()
def save_credit_policy(methodology: str, parameters: Any = None) -> dict[str, Any]:
	"""Salva uma versão nova da política de crédito. Só administradores."""
	frappe.only_for("System Manager")
	return credit_policy.save(methodology, frappe.parse_json(parameters) if isinstance(parameters, str) else parameters)


@frappe.whitelist()
def get_dossier_package() -> dict[str, Any]:
	"""O pacote mínimo do dossiê em vigor e todos os itens que podem entrar nele (credtech#316)."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	return {**credit_package.config(), "options": credit_package.options(), "max_reuse_days": credit_package.MAX_REUSE_DAYS}


@frappe.whitelist()
def save_dossier_package(items: Any, reuse_days: Any) -> dict[str, Any]:
	"""Salva o pacote mínimo do dossiê. Só administradores."""
	frappe.only_for("System Manager")
	return credit_package.save(items, reuse_days)


def _resolve_tier_items(settings_doc, tier: int | str | None) -> list[str] | None:
	if not tier:
		return None
	t = cint(tier)
	if t == 2:
		return ["company:dossie_quod", "company:scr_bacen"]
	if t == 3:
		deep_pj = "company:serasa" if (settings_doc.get("serasa_enabled") and not settings_doc.get("boavista_risco_enabled")) else "company:boavista_risco"
		items = [deep_pj]
		if settings_doc.get("scr_bacen_enabled"):
			items.append("company:scr_bacen")
		if settings_doc.get("processos_completa_enabled"):
			items.append("company:processos_completa")
		if settings_doc.get("boavista_acerta_pf_enabled"):
			items.append("partners:boavista_acerta_pf")
		elif settings_doc.get("dossie_quod_enabled"):
			items.append("partners:dossie_quod")
		return items
	return None


def _parse_contacts(selected_contacts: Any) -> list[str] | None:
	if selected_contacts is None:
		return None
	if isinstance(selected_contacts, str):
		selected_contacts = frappe.parse_json(selected_contacts)
	if isinstance(selected_contacts, list):
		return [str(c) for c in selected_contacts if c]
	return None


@frappe.whitelist()
def get_dossier_package_status(
	organization_name: str,
	deal_name: str | None = None,
	selected_contacts: Any = None,
	tier: int | str | None = None,
) -> dict[str, Any]:
	"""O que o botão "Gerar dossiê" mostra antes: cada item do pacote (já feito no prazo, falta ou não configurado),
	o custo de tabela do que falta (cobrado pelo provedor na conta do cliente), o pré-filtro e o parecer da IA."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _final_bureau_context(deal_name, organization_name)
	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	items_override = _resolve_tier_items(settings_doc, tier)
	contacts_list = _parse_contacts(selected_contacts)
	current = credit_package.status(
		ctx["organization"],
		settings_doc=settings_doc,
		selected_contacts=contacts_list,
		items_override=items_override,
	)
	deal_amount = 0.0
	if ctx["deal"]:
		deal_amount = flt(
			ctx["deal"].get("deal_value")
			or ctx["deal"].get("amount")
			or ctx["deal"].get("annual_revenue")
			or 0.0
		)
	elif organization_name and frappe.db.exists("CRM Organization", organization_name):
		deal_amount = flt(frappe.db.get_value("CRM Organization", organization_name, "annual_revenue") or 0.0)

	return {
		**current,
		"verdict": ctx["verdict"],
		"verdict_summary": ctx["summary"],
		"needs_acknowledgement": bool(current["missing"]) and pre_filter_needs_acknowledgement(ctx["verdict"]),
		"can_run_queries": True,  # qualquer usuário gera o dossiê, com as consultas pagas do pacote (dono, 02/11/2026)
		"running": is_job_enqueued(credit_package.job_id(ctx["organization"])),
		"opinion": credit_opinion.status(),
		"deal_amount": deal_amount,
	}


@frappe.whitelist()
def generate_credit_package(
	organization_name: str,
	deal_name: str | None = None,
	confirm_cost: bool = False,
	acknowledged_warning: bool = False,
	confirm_extra_cost: bool = False,
	selected_contacts: Any = None,
	tier: int | str | None = None,
	requested_amount: float | str | None = None,
) -> dict[str, Any]:
	"""O botão "Gerar dossiê" (credtech#316): faz as consultas do pacote mínimo que faltam, com a chave do cliente,
	gera o PDF 1 (retornos) e o PDF 2 (parecer da IA), tudo em segundo plano.

	Transparência: consulta que falta só com `confirm_cost` (a tela mostrou o preço de tabela e que quem cobra é o
	provedor) e, com alerta no pré-filtro, com `acknowledged_warning`. Qualquer usuário gera (decisão do dono em 02/11/2026). Sem IA
	disponível (não configurada, cota do mês usada ou sem saldo), o dossiê sai sem o parecer e a resposta diz por quê."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _final_bureau_context(deal_name, organization_name)
	organization_name = ctx["organization"]
	deal_name = ctx["deal"].name if ctx["deal"] else None
	if is_job_enqueued(credit_package.job_id(organization_name)):
		frappe.throw(_("A credit dossier is already being generated for this organization. Wait for it to finish."))

	if requested_amount is not None and flt(requested_amount) > 0:
		try:
			from cred_hub.credit_check.committee import _load_committee_raw, _save_committee_raw

			comm = _load_committee_raw(organization_name)
			comm["requested_limit"] = flt(requested_amount)
			_save_committee_raw(organization_name, comm, deal_name=deal_name)
		except Exception:
			pass

	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	items_override = _resolve_tier_items(settings_doc, tier)
	contacts_list = _parse_contacts(selected_contacts)
	current = credit_package.status(
		organization_name,
		settings_doc=settings_doc,
		selected_contacts=contacts_list,
		items_override=items_override,
	)
	if current["unavailable"]:
		labels = ", ".join(sorted({entry["label"] for entry in current["unavailable"]}))
		frappe.throw(
			_(
				"Minimum package: {0} is not enabled or has no credential in the Credit settings. An admin needs to turn it on or take it out of the package."
			).format(labels)
		)
	needs_ack = False
	if current["missing"]:
		labels = ", ".join(f"{entry['label']} ({entry['subject_name']})" for entry in current["missing"])
		if not _truthy(confirm_cost):
			frappe.throw(
				_("The minimum package needs paid queries billed by the provider on your account: {0}. Confirm the cost to continue.").format(
					labels
				)
			)
		needs_ack = pre_filter_needs_acknowledgement(ctx["verdict"])
		if needs_ack and not _truthy(acknowledged_warning):
			frappe.throw(
				_("The pre-filter result for this organization is {0}. Review the alert and confirm to query anyway.").format(
					_verdict_label(ctx["verdict"])
				)
			)

	answer = credit_opinion.check(organization_name, confirm_extra_cost, require_queries=False)
	if answer["needs_confirmation"]:
		frappe.throw(answer["reason"])
	opinion_name, opinion_skipped = None, ""
	if answer["ok"]:
		opinion_name = credit_opinion.create(organization_name, deal_name, answer["is_extra"], enqueue=False)["name"]
	else:
		opinion_skipped = answer["reason"]

	frappe.enqueue(
		"cred_hub.credit_check.package.run_job",
		queue="long",
		timeout=1800,
		job_id=credit_package.job_id(organization_name),
		deduplicate=True,
		enqueue_after_commit=True,
		organization_name=organization_name,
		deal_name=deal_name,
		verdict=ctx["verdict"],
		warning_acknowledged=needs_ack,
		opinion_name=opinion_name,
		selected_contacts=contacts_list,
		items_override=items_override,
	)
	return {
		"queued": True,
		"queries": len(current["missing"]),
		"opinion": opinion_name,
		"opinion_skipped": opinion_skipped,
	}


@frappe.whitelist()
def get_checklist_template() -> dict[str, Any]:
	"""Modelo do checklist de documentos de crédito (Pasta Digital) configurado na empresa."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	return credit_onboarding.get_checklist_template()


@frappe.whitelist()
def save_checklist_template(items: Any) -> dict[str, Any]:
	"""Salva o modelo de checklist de documentos de crédito. Só administradores."""
	frappe.only_for("System Manager")
	return credit_onboarding.save_checklist_template(items)


@frappe.whitelist()
def get_rating_policy() -> dict[str, Any]:
	"""Retorna os parâmetros configurados para o Rating Interno (AA–E) e Calculadora de Limite Sugerido."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	from cred_hub.credit_check.committee import get_rating_policy as _get_policy

	return _get_policy()


@frappe.whitelist()
def save_rating_policy(policy: Any = None, reset_default: bool = False) -> dict[str, Any]:
	"""Salva (ou restaura para o padrão) os parâmetros do Rating Interno (AA–E) e Calculadora de Limite Sugerido."""
	frappe.only_for("System Manager")
	from cred_hub.credit_check.committee import save_rating_policy as _save_policy

	return _save_policy(policy_data=policy, reset_default=_truthy(reset_default))


def _onboarding_context(
	deal_name: str | None = None,
	organization_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Resolve a Organização e opcionalmente o Card para operações da Pasta Digital e Relatório de Visita."""
	resolved_deal = deal_name or deal
	resolved_org = organization_name or organization
	if not resolved_deal and not resolved_org:
		frappe.throw(_("Inform a deal or an organization."))
	_check_deal_and_org_permission(resolved_deal, resolved_org)

	deal_doc = frappe.get_doc("CRM Deal", resolved_deal) if resolved_deal else None
	if deal_doc and not resolved_org:
		resolved_org = deal_doc.organization
	if not resolved_org or not frappe.db.exists("CRM Organization", resolved_org):
		frappe.throw(_("Organization not found."))

	return {
		"deal": deal_doc,
		"organization": resolved_org,
	}


@frappe.whitelist()
def get_credit_onboarding(
	organization_name: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Checklist de documentos (Pasta Digital) e Relatório de Visita Técnica/Comercial da empresa."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.get_onboarding(ctx["organization"], ctx["deal"].name if ctx["deal"] else None)


@frappe.whitelist()
def update_checklist_item(
	organization_name: str | None = None,
	item_key: str = "",
	status: str | None = None,
	file_url: str | None = None,
	file_name: str | None = None,
	notes: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
	valid_until: str | None = None,
) -> dict[str, Any]:
	"""Anexa arquivo, define vencimento (se aplicável), dá check (Conferido) ou registra pendência/rejeição em um documento do checklist."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.update_checklist_item(
		organization_name=ctx["organization"],
		item_key=item_key,
		status=status,
		file_url=file_url,
		file_name=file_name,
		notes=notes,
		deal_name=ctx["deal"].name if ctx["deal"] else None,
		valid_until=valid_until,
	)


@frappe.whitelist()
def update_credit_checklist_item(
	organization_name: str | None = None,
	item_key: str = "",
	status: str | None = None,
	file_url: str | None = None,
	file_name: str | None = None,
	notes: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
	valid_until: str | None = None,
) -> dict[str, Any]:
	"""Alias REST para update_checklist_item."""
	return update_checklist_item(
		organization_name=organization_name,
		item_key=item_key,
		status=status,
		file_url=file_url,
		file_name=file_name,
		notes=notes,
		deal_name=deal_name,
		organization=organization,
		deal=deal,
		valid_until=valid_until,
	)


@frappe.whitelist()
def save_visit_report(
	organization_name: str | None = None,
	visit_data: Any = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Salva o Relatório de Visita Técnica / Comercial (in loco ou remota) com fotos e parecer do visitador."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.save_visit_report(
		organization_name=ctx["organization"],
		visit_data=visit_data,
		deal_name=ctx["deal"].name if ctx["deal"] else None,
	)


@frappe.whitelist()
def save_credit_visit_report(
	organization_name: str | None = None,
	visit_data: Any = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Alias REST para save_visit_report."""
	return save_visit_report(
		organization_name=organization_name,
		visit_data=visit_data,
		deal_name=deal_name,
		organization=organization,
		deal=deal,
	)


@frappe.whitelist()
def add_custom_checklist_item(
	organization_name: str | None = None,
	label: str = "",
	category: str = "Específico",
	required: bool = True,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Adiciona um documento específico avulso ao checklist desta empresa/card."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.add_custom_checklist_item(
		organization_name=ctx["organization"],
		label=label,
		category=category,
		required=_truthy(required),
		deal_name=ctx["deal"].name if ctx["deal"] else None,
	)


@frappe.whitelist()
def remove_custom_checklist_item(
	organization_name: str | None = None,
	item_key: str = "",
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Remove um documento específico avulso do checklist desta empresa/card."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.remove_custom_checklist_item(
		organization_name=ctx["organization"],
		item_key=item_key,
		deal_name=ctx["deal"].name if ctx["deal"] else None,
	)


@frappe.whitelist()
def generate_portal_link(
	organization_name: str | None = None,
	deal_name: str | None = None,
	regenerate: bool = False,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Gera (ou regenera) o Link Externo Blindado da Pasta Digital para o cliente/contador enviar documentos."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.generate_portal_link(
		organization_name=ctx["organization"],
		deal_name=ctx["deal"].name if ctx["deal"] else None,
		regenerate=_truthy(regenerate),
	)


@frappe.whitelist()
def create_onboarding_portal_link(
	organization_name: str | None = None,
	deal_name: str | None = None,
	regenerate: bool = False,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Alias REST para generate_portal_link."""
	return generate_portal_link(
		organization_name=organization_name,
		deal_name=deal_name,
		regenerate=regenerate,
		organization=organization,
		deal=deal,
	)


@frappe.whitelist()
def revoke_portal_link(
	organization_name: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Revoga imediatamente o Link Externo da Pasta Digital."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	return credit_onboarding.revoke_portal_link(
		organization_name=ctx["organization"],
		deal_name=ctx["deal"].name if ctx["deal"] else None,
	)


@frappe.whitelist()
def check_deal(
	deal_name: str | None = None,
	organization_name: str | None = None,
	force: bool = False,
	deal: str | None = None,
	organization: str | None = None,
	tier_1_only: bool = False,
) -> dict[str, Any]:
	"""Alias REST para trigger_check."""
	return trigger_check(
		deal_name=deal_name or deal,
		organization_name=organization_name or organization,
		force=force,
		tier_1_only=tier_1_only,
	)


@frappe.whitelist(allow_guest=True)
def public_get_portal_info(token: str) -> dict[str, Any]:
	"""Metadados mínimos do Link Externo antes da confirmação do CNPJ (não expõe documentos nem arquivos)."""
	return credit_onboarding.public_get_portal_info(token)


@frappe.whitelist(allow_guest=True)
def public_verify_and_list(token: str, cnpj: str = "") -> dict[str, Any]:
	"""Valida o CNPJ e retorna a lista de documentos em modo Caixa de Correio Blindada (sem URLs de download)."""
	return credit_onboarding.public_verify_and_list(token, cnpj)


@frappe.whitelist(allow_guest=True)
def public_upload_document(
	token: str,
	cnpj: str,
	item_key: str,
	file_name: str,
	file_content_base64: str,
	custom_label: str = "",
	valid_until: str = "",
) -> dict[str, Any]:
	"""Recebe um documento enviado pelo cliente no Link Externo Blindado e salva como arquivo privado no HubTech."""
	return credit_onboarding.public_upload_document(
		token=token,
		cnpj=cnpj,
		item_key=item_key,
		file_name=file_name,
		file_content_base64=file_content_base64,
		custom_label=custom_label,
		valid_until=valid_until,
	)


@frappe.whitelist()
def analyze_checklist_document(
	organization_name: str | None = None,
	item_key: str = "",
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Executa a leitura inteligente (OCR/Parser) de um documento anexado na Pasta Digital e atualiza o resumo extraído."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	org_name = ctx["organization"]
	deal_id = ctx["deal"].name if ctx["deal"] else None
	state = credit_onboarding._load_org_state(org_name)
	checklist_state = state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
	entry = checklist_state.get(item_key) if isinstance(checklist_state.get(item_key), dict) else {}
	file_url = str(entry.get("file_url") or "").strip()
	file_name = str(entry.get("file_name") or "").strip()
	if not file_url:
		frappe.throw(_("Attach a file to this checklist document before running smart extraction."))
	from cred_hub.credit_check.committee import extract_document_insights

	extracted = extract_document_insights(
		organization_name=org_name,
		item_key=item_key,
		file_url=file_url,
		file_name=file_name,
	)
	entry["extracted_data"] = extracted
	checklist_state[item_key] = entry
	state["checklist"] = checklist_state
	credit_onboarding._save_org_state(org_name, state, deal_name=deal_id)
	return credit_onboarding.get_onboarding(org_name, deal_id)


@frappe.whitelist()
def get_credit_committee(
	organization_name: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Retorna o Rating Interno (AA–E), Calculadora de Limite Sugerido, Grupo Econômico e Mesa de Comitê de Crédito."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	from cred_hub.credit_check.committee import get_committee_state

	return get_committee_state(ctx["organization"], ctx["deal"].name if ctx["deal"] else None)


@frappe.whitelist()
def save_committee_proposal(
	organization_name: str | None = None,
	deal_name: str | None = None,
	proposed_limit: float = 0,
	proposed_concentration: float = 0,
	required_quorum: int = 1,
	validity_days: int = 180,
	conditions_notes: str = "",
	organization: str | None = None,
	deal: str | None = None,
	requested_limit: float | None = None,
) -> dict[str, Any]:
	"""Salva a proposta de limite/alçada, quórum exigido e condicionantes na Mesa de Comitê de Crédito."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	from cred_hub.credit_check.committee import save_committee_proposal as _save_proposal

	proposal_dict = {"requested_limit": requested_limit} if requested_limit is not None else None
	_save_proposal(
		organization_name=ctx["organization"],
		proposal_data=proposal_dict,
		deal_name=ctx["deal"].name if ctx["deal"] else None,
		proposed_limit=proposed_limit,
		proposed_concentration=proposed_concentration,
		required_quorum=required_quorum,
		validity_days=validity_days,
		conditions_notes=conditions_notes,
	)
	return credit_onboarding.get_onboarding(ctx["organization"], ctx["deal"].name if ctx["deal"] else None)


@frappe.whitelist()
def cast_committee_vote(
	organization_name: str | None = None,
	deal_name: str | None = None,
	vote: str = "approve",
	voted_limit: float | None = None,
	notes: str = "",
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Registra o voto auditável do membro de alçada na Mesa de Comitê de Crédito."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	from cred_hub.credit_check.committee import cast_committee_vote as _cast_vote

	_cast_vote(
		organization_name=ctx["organization"],
		deal_name=ctx["deal"].name if ctx["deal"] else None,
		vote=vote,
		voted_limit=voted_limit,
		notes=notes,
	)
	return credit_onboarding.get_onboarding(ctx["organization"], ctx["deal"].name if ctx["deal"] else None)


@frappe.whitelist()
def get_whatsapp_onboarding_reminder(
	organization_name: str | None = None,
	deal_name: str | None = None,
	organization: str | None = None,
	deal: str | None = None,
) -> dict[str, Any]:
	"""Gera a mensagem pronta e telefone para cobrar documentos pendentes/recusados/vencidos via WhatsApp."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	ctx = _onboarding_context(deal_name, organization_name, organization, deal)
	from cred_hub.credit_check.committee import build_whatsapp_onboarding_reminder

	return build_whatsapp_onboarding_reminder(ctx["organization"], ctx["deal"].name if ctx["deal"] else None)


# ==============================================================================
# VERTICAL DE CRÉDITO, BIRÔ, LOCALIZE & PROSPECÇÃO B2B (6 PRODUTOS + CRM SYNC)
# ==============================================================================


@frappe.whitelist()
def get_bureau_vertical_catalog() -> dict[str, Any]:
	"""Retorna os 6 produtos oficiais (PJ Básico, PJ Avançado, PF Básico, PF Avançado,
	Localize Só Telefones e Localize Completo c/ CENPROT) e o saldo atual da carteira."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	from cred_hub.billing import wallet_balance
	from cred_hub.credit_check.bureau_vertical import get_catalog_dict

	return {
		"products": get_catalog_dict(),
		"wallet_balance": wallet_balance(),
	}


@frappe.whitelist(methods=["POST"])
def run_bureau_vertical_product(
	product_key: str,
	document: str,
	requested_amount: float = 0.0,
	sync_to_crm: int | bool = 1,
	create_deal: int | bool = 0,
	deal_stage: str = "",
	force: int | bool = 0,
	charge_wallet: int | bool = 0,
) -> dict[str, Any]:
	"""Executa qualquer um dos 6 produtos na vertical de Crédito & Localize, utilizando cache de 30 dias,
	aplicando o Motor de Crédito e sincronizando nativamente com Organizações/Contatos/Cards do HubTech CRM."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	from cred_hub.billing import refund_credits, spend_credits
	from cred_hub.cnpj import client as cnpj_client
	from cred_hub.credit_check.bureau_vertical import (
		BUREAU_PRODUCTS,
		run_product_query,
		sync_result_to_hubtech_crm,
	)
	from cred_hub.credit_check.platform_credentials import platform_bigdatacorp_token
	from cred_hub.integrations import bureau_log

	key = (product_key or "").strip().upper()
	if key not in BUREAU_PRODUCTS:
		frappe.throw(_("Invalid bureau product: {0}.").format(product_key))
	spec = BUREAU_PRODUCTS[key]
	digits = only_digits(document)

	settings = get_credit_settings()
	token = platform_bigdatacorp_token(allow_empty=True) or settings.get("bigdatacorp_token") or ""
	directd_token = settings.get("directd_token") or ""

	operation = f"vertical_{key.lower()}"
	cached_entry = None if _truthy(force) else bureau_log.recent("BigDataCorp", operation, digits, days=30)
	if cached_entry and isinstance(cached_entry.data, dict):
		result = dict(cached_entry.data)
		result["cached"] = True
	else:
		price_to_charge = spec.suggested_price_brl if _truthy(charge_wallet) else 0.0
		if price_to_charge > 0:
			spend_credits(price_to_charge, description=f"{spec.label} ({digits})")
		try:
			opencnpj_data = None
			if len(digits) == 14:
				try:
					opencnpj_data = cnpj_client.fetch(digits)
				except Exception:
					opencnpj_data = None
			policy_cfg = {**settings, **(credit_policy.current().get("parameters") or {})}
			result = run_product_query(
				product_key=key,
				document=digits,
				token=token,
				opencnpj_data=opencnpj_data,
				directd_token=directd_token,
				policy_settings=policy_cfg,
				requested_amount=flt(requested_amount),
			)
			result["cached"] = False
			bureau_log.record(
				"BigDataCorp",
				operation,
				bureau_log.SUCCESS,
				document=digits,
				http_status=200,
				protocol=str(result.get("query_id") or ""),
				purpose="Credit protection and collection",
				response=result,
			)
		except Exception:
			if price_to_charge > 0:
				refund_credits(price_to_charge, description=f"Estorno {spec.label} ({digits})")
			raise

	if _truthy(sync_to_crm):
		result["crm_sync"] = sync_result_to_hubtech_crm(
			result,
			create_deal=_truthy(create_deal),
			deal_stage=deal_stage,
		)
	return result


@frappe.whitelist()
def search_bureau_b2b_leads(
	uf: str = "",
	cnaes: str | list[str] | None = None,
	id_municipio: str = "",
	cep_prefix: str = "",
	portes: str | list[str] | None = None,
	capital_social_min: float = 0.0,
	capital_social_max: float = 0.0,
	somente_simples: int | bool = 0,
	excluir_mei: int | bool = 1,
	idade_minima_anos: int = 0,
	somente_com_telefone: int | bool = 0,
	limit: int = 100,
) -> dict[str, Any]:
	"""Monta a consulta parametrizada de prospecção de leads B2B na Receita Federal (`basedosdados.br_rf_cnpj`)."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	from cred_hub.credit_check.bureau_vertical import build_bigquery_leads_sql

	if isinstance(cnaes, str):
		cnae_list = [c.strip() for c in cnaes.split(",") if c.strip()]
	else:
		cnae_list = list(cnaes or [])

	if isinstance(portes, str):
		porte_list = [p.strip() for p in portes.split(",") if p.strip()]
	else:
		porte_list = list(portes or [])

	sql, params = build_bigquery_leads_sql(
		uf=uf,
		cnaes=cnae_list,
		id_municipio=id_municipio,
		cep_prefix=cep_prefix,
		portes=porte_list,
		capital_social_min=flt(capital_social_min),
		capital_social_max=flt(capital_social_max),
		somente_simples=_truthy(somente_simples),
		excluir_mei=_truthy(excluir_mei),
		idade_minima_anos=cint(idade_minima_anos),
		somente_com_telefone=_truthy(somente_com_telefone),
		limit=cint(limit or 100),
	)
	return {"ok": True, "sql": sql, "params": params}

