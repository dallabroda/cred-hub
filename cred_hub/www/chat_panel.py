"""Painel do Cred Hub dentro da conversa do Cred Chat (credtech#45): /chat-panel, aberto como Dashboard App.
Só o Cred Chat pode pôr esta página em iframe; os dados vêm de cred_hub.chat_panel com a sessão do usuário."""

import frappe
from frappe import _
from frappe.utils import get_url

from cred_hub.welcome import _chat_url

no_cache = 1


def get_context(context):
	chat = _chat_url()
	# O nginx do Cred Hub manda X-Frame-Options SAMEORIGIN; com frame-ancestors o navegador vale só este
	frappe.local.response_headers["Content-Security-Policy"] = f"frame-ancestors 'self' {chat}"
	logged_in = frappe.session.user != "Guest"
	context.no_breadcrumbs = True
	context.title = "Cred Hub"
	context.lang = frappe.local.lang or "pt-BR"
	context.logged_in = logged_in
	context.chat_origin = chat
	context.csrf_token = frappe.sessions.get_csrf_token() if logged_in else ""
	context.login_url = get_url("/login?redirect-to=/crm")
	context.messages = {
		"loading": _("Loading..."),
		"login_title": _("Sign in to Cred Hub to see this contact's cards."),
		"login": _("Sign in to Cred Hub"),
		"login_hint": _("After signing in, come back to this conversation."),
		"outside_chat": _("Open this panel from a conversation in Cred Chat."),
		"no_contact": _("This conversation has no phone number or e-mail to look for."),
		"no_cards": _("No card for this contact."),
		"open_hub": _("Open Cred Hub"),
		"open_card": _("Open card"),
		"stage": _("Stage"),
		"value": _("Value"),
		"owner": _("Owner"),
		"save": _("Save"),
		"saved": _("Saved"),
		"saving": _("Saving..."),
		"error": _("Something went wrong. Please try again in a few minutes."),
	}
