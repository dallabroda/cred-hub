"""APIs REST (Frappe Whitelist) para telas de Configurações e Cards do CRM."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

from cred_hub.credit_check.clients.escavador import fetch_lawsuits
from cred_hub.credit_check.clients.infosimples import query_pgfn
from cred_hub.credit_check.clients.serasa import (
	BureauGateBlocked,
	SerasaError,
	SerasaNotConfigured,
	trigger_serasa_relato,
)
from cred_hub.credit_check.enrich import get_credit_settings, run_background_check

# Custo real aproximado de cada chamada de teste de conexão — cobrado mesmo numa consulta de teste
TEST_CONNECTION_COST = {
	"escavador": 0.50,
	"infosimples": 0.70,
}


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
def test_provider_connection(provider: str, token: str, cnpj: str, confirm_cost: bool = False) -> dict[str, Any]:
	"""Valida se o token informado conecta com sucesso ao provedor.

	Custo real e explícito: exige um CNPJ escolhido por quem está testando (nunca um CNPJ de
	terceiro fixo no código) e a confirmação de que esta chamada tem custo, para o custo nunca
	ficar escondido num botão "testar".
	"""
	frappe.only_for("System Manager")
	token = (token or "").strip()
	cnpj = (cnpj or "").strip()
	if not token:
		frappe.throw(_("Enter the API token to test."))
	if not cnpj:
		frappe.throw(_("Enter a CNPJ to run the test query against."))
	if provider not in TEST_CONNECTION_COST:
		frappe.throw(_("Unknown provider: {0}").format(provider))
	if not confirm_cost:
		frappe.throw(
			_("This test runs a real paid query (~R$ {0}). Confirm the cost to continue.").format(
				f"{TEST_CONNECTION_COST[provider]:.2f}"
			)
		)

	if provider == "escavador":
		try:
			fetch_lawsuits(cnpj, token)
			return {"ok": True, "message": _("Connected to the Escavador API (v2) successfully!")}
		except Exception as e:
			frappe.throw(_("Escavador connection test failed: {0}").format(str(e)))

	elif provider == "infosimples":
		try:
			query_pgfn(cnpj, token)
			return {"ok": True, "message": _("Connected to the Infosimples API (v2) successfully!")}
		except Exception as e:
			frappe.throw(_("Infosimples connection test failed: {0}").format(str(e)))


@frappe.whitelist()
def request_bureau_final(deal_name: str, bureau: str = "serasa") -> dict[str, Any]:
	"""Dispara a consulta ao bureau final (Serasa), garantindo pré-aprovação."""
	frappe.only_for(["System Manager", "Sales Manager"])
	_check_deal_and_org_permission(deal_name, None)

	deal = frappe.get_doc("CRM Deal", deal_name)
	if not deal.credit_verdict:
		frappe.throw(_("This deal has not gone through the credit pre-analysis yet."))

	org_cnpj = frappe.db.get_value("CRM Organization", deal.organization, "cnpj") if deal.organization else ""
	if not org_cnpj:
		frappe.throw(_("The deal must be linked to an organization with a valid CNPJ."))

	settings_doc = frappe.get_single("Cred Hub Credit Settings")
	endpoint_url = settings_doc.get("serasa_endpoint_url") or ""
	serasa_token = settings_doc.get_password("serasa_token") if settings_doc.get("serasa_token") else ""

	try:
		result = trigger_serasa_relato(org_cnpj, frappe.session.user, deal.credit_verdict, endpoint_url, serasa_token)
	except BureauGateBlocked as e:
		frappe.throw(str(e))
	except SerasaNotConfigured as e:
		frappe.throw(str(e))
	except SerasaError as e:
		frappe.throw(_("Serasa query failed: {0}").format(str(e)))

	deal.add_comment(
		"Comment",
		_("Final bureau query ({0}) requested by {1}. Provider status: {2}.").format(
			bureau.upper(), frappe.session.user, result.get("status")
		),
	)
	return result
