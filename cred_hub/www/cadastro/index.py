"""Página de cadastro de empresa nova (credtech#25): /cadastro, só no site central (credhub_signup no site_config)."""

import frappe
from frappe import _
from frappe.core.doctype.navbar_settings.navbar_settings import get_app_logo

from cred_hub.signup import _domain

no_cache = 1

TERMS_URL = "https://credtechdigital.com.br/termos/#cred-hub"
PRIVACY_URL = "https://credtechdigital.com.br/privacidade/"


def get_context(context):
	if not frappe.conf.get("credhub_signup"):
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect
	context.title = _("Create your company")
	context.logo = get_app_logo()
	context.domain = _domain()
	context.terms_url = TERMS_URL
	context.privacy_url = PRIVACY_URL
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
		"generic_error": _("Something went wrong. Please try again in a few minutes."),
	}
	return context
