"""APIs REST (Frappe Whitelist) para telas de Configurações e Cards do CRM."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import query_pgfn
from cred_hub.credit_check.clients.serasa import trigger_serasa_relato
from cred_hub.credit_check.enrich import get_credit_settings, run_background_check


@frappe.whitelist()
def trigger_check(deal_name: str | None = None, organization_name: str | None = None, force: bool = False) -> dict[str, Any]:
	"""Dispara a esteira manualmente a partir da interface do Card ou Organização."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])

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
	return {"message": _("Análise de crédito e conformidade agendada em segundo plano."), "job_id": str(job)}


@frappe.whitelist()
def get_report_details(report_name: str) -> dict[str, Any]:
	"""Retorna os dados detalhados do relatório para o modal do Card."""
	if not frappe.db.exists("Cred Hub Credit Report", report_name):
		frappe.throw(_("Relatório não encontrado."))

	report = frappe.get_doc("Cred Hub Credit Report", report_name)
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
		"raw_details": raw_details,
	}


@frappe.whitelist()
def test_provider_connection(provider: str, token: str) -> dict[str, Any]:
	"""Valida se o token informado conecta com sucesso ao provedor."""
	frappe.only_for("System Manager")
	token = (token or "").strip()
	if not token:
		frappe.throw(_("Informe o token da API para testar."))

	# CNPJ de teste (Petrobras ou similar para teste de conexão)
	test_cnpj = "33000167000101"

	if provider == "escavador":
		try:
			fetch_lawsuits(test_cnpj, token)
			return {"ok": True, "message": _("Conexão com a Escavador API (v2) realizada com sucesso!")}
		except Exception as e:
			frappe.throw(_("Falha ao testar Escavador: {0}").format(str(e)))

	elif provider == "infosimples":
		try:
			query_pgfn(test_cnpj, token)
			return {"ok": True, "message": _("Conexão com a Infosimples API (v2) realizada com sucesso!")}
		except Exception as e:
			frappe.throw(_("Falha ao testar Infosimples: {0}").format(str(e)))

	frappe.throw(_("Provedor desconhecido: {0}").format(provider))


@frappe.whitelist()
def request_bureau_final(deal_name: str, bureau: str = "serasa") -> dict[str, Any]:
	"""Dispara a consulta ao bureau final (Serasa), garantindo pré-aprovação."""
	frappe.only_for(["System Manager", "Sales Manager"])

	deal = frappe.get_doc("CRM Deal", deal_name)
	if not deal.credit_verdict:
		frappe.throw(_("Este card ainda não passou pela pré-análise de crédito."))

	org_cnpj = frappe.db.get_value("CRM Organization", deal.organization, "cnpj") if deal.organization else ""
	if not org_cnpj:
		frappe.throw(_("O card precisa estar vinculado a uma organização com CNPJ válido."))

	result = trigger_serasa_relato(org_cnpj, frappe.session.user, deal.credit_verdict)
	deal.add_comment("Comment", _("Consulta ao bureau final ({0}) autorizada e solicitada pelo usuário {1}.").format(bureau.upper(), frappe.session.user))
	return result
