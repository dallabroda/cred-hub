"""Manipulador de eventos do Frappe para CRM Deal e CRM Organization."""

from __future__ import annotations

import frappe
from cred_hub.cnpj.mapping import is_valid_cnpj
from cred_hub.credit_check.enrich import get_credit_settings


def on_deal_update(doc, method=None):
	"""Dispara a esteira assíncrona quando o card é movido para a etapa configurada."""
	if not doc.has_value_changed("status"):
		return

	settings = get_credit_settings()
	if not settings.get("enabled") or not settings.get("auto_trigger_stage"):
		return

	# Verifica se a nova etapa corresponde ao gatilho
	if doc.status != settings["auto_trigger_stage"]:
		return

	# 1. Primeira Organização (Tomador / Devedor Principal)
	if doc.organization and frappe.db.exists("CRM Organization", doc.organization):
		org_cnpj = frappe.db.get_value("CRM Organization", doc.organization, "cnpj")
		if org_cnpj and is_valid_cnpj(org_cnpj):
			frappe.enqueue(
				"cred_hub.credit_check.enrich.run_background_check",
				queue="long",
				job_id=f"credit_check::{doc.name}::{doc.organization}",
				deduplicate=True,
				enqueue_after_commit=True,
				deal_name=doc.name,
				organization_name=doc.organization,
			)

	# 2. Segunda Organização (Sacado — issue #195)
	if settings.get("check_second_organization") and doc.get("second_organization"):
		sec_org = doc.second_organization
		if frappe.db.exists("CRM Organization", sec_org):
			sec_cnpj = frappe.db.get_value("CRM Organization", sec_org, "cnpj")
			if sec_cnpj and is_valid_cnpj(sec_cnpj):
				frappe.enqueue(
					"cred_hub.credit_check.enrich.run_background_check",
					queue="long",
					job_id=f"credit_check_sec::{doc.name}::{sec_org}",
					deduplicate=True,
					enqueue_after_commit=True,
					deal_name=doc.name,
					organization_name=sec_org,
				)
