"""Página pública da Pasta Digital de Crédito (/pasta-credito?token=...) em modo Caixa de Correio Blindada (só envio, sem download)."""

import frappe
from frappe.core.doctype.navbar_settings.navbar_settings import get_app_logo

no_cache = 1


def get_context(context):
	context.no_breadcrumbs = True
	context.token = (frappe.form_dict.get("token") or "").strip()
	context.brand_name = frappe.db.get_single_value("FCRM Settings", "brand_name") or "HubTech CRM"
	context.logo = get_app_logo() or ""
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.title = f"Pasta Digital de Crédito · {context.brand_name}"
	return context
