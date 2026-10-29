"""Página de cadastro de empresa nova (credtech#25): /cadastro, só no site central (credhub_signup no site_config)."""

import frappe
from frappe import _
from frappe.core.doctype.navbar_settings.navbar_settings import get_app_logo

from cred_hub.signup import _domain, signup_open
from cred_hub.signup_access import get_prefill

no_cache = 1

TERMS_URL = "https://credtechdigital.com.br/termos/#cred-hub"
PRIVACY_URL = "https://credtechdigital.com.br/privacidade/"


def get_context(context):
	if not signup_open():
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect
	context.title = f"{_('Create your company')} · HubTech CRM"
	context.logo = get_app_logo()
	context.domain = _domain()
	context.terms_url = TERMS_URL
	context.privacy_url = PRIVACY_URL
	# Quem chegou sem conta: pelo login social do hub (de=chave com e-mail e nome) ou de outra empresa (sem_conta=1)
	context.prefill = get_prefill(frappe.form_dict.get("de"))
	# O e-mail vindo do "Teste grátis" do site (from_site) não é "entrou sem conta"
	context.no_account = bool(context.prefill and not context.prefill.get("from_site")) or bool(
		frappe.form_dict.get("sem_conta")
	)
	# Textos do JavaScript, já traduzidos no idioma do visitante
	context.messages = {
		"checking": _("Checking..."),
		"available": _("Available"),
		"required": _("Fill in this field."),
		"invalid_email": _("Enter a valid e-mail address."),
		"accept_terms": _("Accept the terms to continue."),
		"choose_address": _("Choose an available address."),
		"sending": _("Sending..."),
		"submit": _("Create my company"),
		"resent": _("We sent a new link. Use the newest e-mail."),
		"generic_error": _("Something went wrong. Please try again in a few minutes."),
	}
	return context
