"""Página do link de confirmação (credtech#25): /cadastro/confirmar?token=... Confirma o e-mail e acompanha a
criação da empresa até o endereço novo ficar pronto."""

import frappe
from frappe import _
from frappe.core.doctype.navbar_settings.navbar_settings import get_app_logo

from cred_hub.signup import signup_open

no_cache = 1

SUPPORT_WHATSAPP = "https://wa.me/5548988692538"


def get_context(context):
	if not signup_open():
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect
	context.title = f"{_('Create your company')} · HubTech CRM"
	context.logo = get_app_logo()
	context.support_url = SUPPORT_WHATSAPP
	# Título e texto de cada situação do cadastro, já traduzidos (o JavaScript só escolhe)
	context.messages = {
		"checking": [_("Confirming your e-mail..."), ""],
		"creating": [
			_("Creating your company..."),
			_("This takes about two minutes. You can keep this page open."),
		],
		"active": [
			_("Your company is ready!"),
			_(
				"We sent you an e-mail to set your password. You can also sign in with Google, Microsoft or GitHub using the same e-mail."
			),
		],
		"failed": [
			_("We could not create your company"),
			_("Our team can finish it for you. Talk to us on WhatsApp."),
		],
		"expired": [_("This link has expired"), _("Links are valid for 24 hours. Please sign up again.")],
		"invalid": [_("This link is invalid"), _("Check that you opened the full link from the e-mail, or sign up again.")],
		"slow": [
			_("This is taking longer than usual"),
			_("Your company is still being created. We will e-mail you when it is ready, or talk to us on WhatsApp."),
		],
		"error": [_("Something went wrong"), _("Please try again in a few minutes.")],
		"open": _("Sign in to my company"),
		"signup_again": _("Sign up again"),
		"whatsapp": _("Talk to us on WhatsApp"),
	}
	return context
