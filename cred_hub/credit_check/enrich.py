"""Worker e orquestrador de segundo plano da esteira de crédito e compliance.

Executa consultas assíncronas no Escavador e Infosimples, aplica regras de corte,
grava o DocType 'Cred Hub Credit Report', atualiza a Organização e o Card, e posta
o resumo na linha do tempo.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from cred_hub.cnpj.mapping import format_cnpj, is_valid_cnpj, only_digits
from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import fetch_fiscal_and_protests
from cred_hub.credit_check.rules import (
	VERDICT_REJECTED,
	evaluate_credit_risk,
	format_timeline_summary,
)

CACHE_DAYS = 30
ESCAVADOR_ESTIMATED_COST = 0.50
INFOSIMPLES_ESTIMATED_COST = 0.70


def get_credit_settings() -> dict[str, Any]:
	"""Lê as configurações de bureau e análise de risco do tenant."""
	if not frappe.db.exists("DocType", "Cred Hub Credit Settings"):
		return {"enabled": False}

	doc = frappe.get_single("Cred Hub Credit Settings")
	return {
		"enabled": bool(doc.get("enabled")),
		"escavador_enabled": bool(doc.get("escavador_enabled")),
		"escavador_token": doc.get_password("escavador_token") if doc.get("escavador_token") else "",
		"infosimples_enabled": bool(doc.get("infosimples_enabled")),
		"infosimples_token": doc.get_password("infosimples_token") if doc.get("infosimples_token") else "",
		"auto_trigger_stage": doc.get("auto_trigger_stage") or "",
		"check_second_organization": bool(doc.get("check_second_organization")),
		"max_lawsuits_defendant": int(doc.get("max_lawsuits_defendant") or 0),
		"max_lawsuit_value": float(doc.get("max_lawsuit_value") or 0.0),
		"block_on_bankruptcy": bool(doc.get("block_on_bankruptcy", 1)),
		"max_pgfn_debt": float(doc.get("max_pgfn_debt") or 0.0),
		"block_on_cndt": bool(doc.get("block_on_cndt", 1)),
		"block_on_protest": bool(doc.get("block_on_protest", 1)),
	}


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

	# Conferência de cache (30 dias)
	if not force:
		cached_report_name = find_valid_cached_report(org_doc.name)
		if cached_report_name:
			_apply_report_to_deal_and_org(cached_report_name, org_doc, deal_doc, is_cached=True)
			return cached_report_name

	# Chamada às APIs externas
	escavador_token = settings.get("escavador_token")
	infosimples_token = settings.get("infosimples_token")

	escavador_data = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	infosimples_data = {
		"pgfn": {"total_debt": 0.0, "status": "Regular", "inscriptions_count": 0},
		"cnd_federal": {"is_regular": True, "status": "Regular", "pdf_url": ""},
		"cndt": {"is_regular": True, "status": "Regular", "pdf_url": ""},
		"cenprot": {"has_protests": False, "protests_count": 0},
	}
	total_cost = 0.0

	# 1. Escavador
	if settings.get("escavador_enabled") and escavador_token:
		try:
			escavador_data = fetch_lawsuits(digits, escavador_token)
			total_cost += ESCAVADOR_ESTIMATED_COST
		except Exception as e:
			frappe.log_error(title=f"Cred Hub: Escavador API falhou para CNPJ {cnpj}", message=str(e))

	# 2. Infosimples
	if settings.get("infosimples_enabled") and infosimples_token:
		try:
			infosimples_data = fetch_fiscal_and_protests(digits, infosimples_token)
			total_cost += INFOSIMPLES_ESTIMATED_COST
		except Exception as e:
			frappe.log_error(title=f"Cred Hub: Infosimples API falhou para CNPJ {cnpj}", message=str(e))

	# 3. Avaliação de Risco
	evaluation = evaluate_credit_risk(escavador_data, infosimples_data, settings)
	verdict = evaluation.get("verdict") or "Error"
	summary = evaluation.get("summary") or ""
	metrics = evaluation.get("metrics") or {}

	# 4. Gravação do DocType 'Cred Hub Credit Report'
	today_date = today()
	valid_until_date = add_days(today_date, CACHE_DAYS)

	report_doc = frappe.get_doc({
		"doctype": "Cred Hub Credit Report",
		"organization": org_doc.name,
		"deal": deal_doc.name if deal_doc else None,
		"cnpj": format_cnpj(digits),
		"checked_on": today_date,
		"valid_until": valid_until_date,
		"verdict": verdict,
		"verdict_summary": summary,
		"lawsuits_total": metrics.get("lawsuits_total", 0),
		"lawsuits_defendant": metrics.get("lawsuits_defendant", 0),
		"lawsuits_highest_value": metrics.get("lawsuits_highest_value", 0.0),
		"has_bankruptcy_recovery": 1 if metrics.get("has_bankruptcy") else 0,
		"pgfn_status": metrics.get("pgfn_debt", 0.0) > 0 and "Inscrito na Dívida Ativa" or "Regular",
		"pgfn_total_debt": metrics.get("pgfn_debt", 0.0),
		"cnd_federal_status": metrics.get("cnd_federal_status", "Regular"),
		"cnd_federal_pdf_url": metrics.get("cnd_federal_pdf", ""),
		"cndt_status": metrics.get("cndt_status", "Regular"),
		"cndt_pdf_url": metrics.get("cndt_pdf", ""),
		"cenprot_has_protests": 1 if metrics.get("cenprot_has_protest") else 0,
		"cenprot_protests_count": metrics.get("cenprot_protests_count", 0),
		"total_cost": total_cost,
		"raw_details_json": json.dumps({"escavador": escavador_data, "infosimples": infosimples_data}, ensure_ascii=False),
	})
	report_doc.insert(ignore_permissions=True)

	# 5. Atualização da Organização e Deal + Comentários na timeline
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
				_("Relatório de conformidade reaproveitado do cache (consultado em {0}, válido até {1}): parecer {2}.").format(
					report.checked_on, report.valid_until, verdict
				),
			)
