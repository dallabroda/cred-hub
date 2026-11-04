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
	context.title = "HubTech CRM"
	context.lang = frappe.local.lang or "pt-BR"
	context.logged_in = logged_in
	context.chat_origin = chat
	context.csrf_token = frappe.sessions.get_csrf_token() if logged_in else ""
	context.login_url = get_url("/login?redirect-to=/crm")
	# Textos do painel (layout do Figma, credtech: painel do HubTech na conversa). Os textos comuns do card
	# ("Won", "Stage"...) já vêm traduzidos pelo app crm; os próprios do painel estão em locale/pt_BR.po.
	context.messages = {
		"loading": _("Loading..."),
		"login_title": _("Sign in to HubTech CRM"),
		"login_text": _(
			"Sign in with your HubTech CRM login to see and move this contact's cards. After signing in, come back to this conversation."
		),
		"login": _("Sign in to Cred Hub"),
		"outside_chat": _("Open this panel from a conversation in Cred Chat."),
		"no_contact": _("This conversation has no phone number or e-mail to look for."),
		"no_cards": _("No card for this contact"),
		"no_cards_text": _("We found no cards in HubTech CRM with this contact's phone number or e-mail."),
		"open_hub": _("Go to HubTech CRM"),
		"card_of": _("Card {0} of {1}"),
		"choose_card": _("Choose card"),
		"send_email": _("Send e-mail"),
		"no_email": _("This card has no e-mail."),
		"copy_link": _("Copy card link"),
		"link_copied": _("Card link copied"),
		"copy_failed": _("Could not copy the link."),
		"open_attachments": _("Open attachments in HubTech CRM"),
		"progress": _("Card progress"),
		"open_in_hub": _("Open in HubTech CRM"),
		"stage": _("Stage"),
		"outcome": _("Card outcome"),
		"won": _("Won"),
		"lost": _("Lost"),
		"reopen": _("Reopen"),
		"read_only": _("You can't edit this card."),
		"stage_updated": _("Stage updated to {0}"),
		"marked_won": _("Card marked as won."),
		"marked_lost": _("Card marked as lost."),
		"reopened": _("Card reopened."),
		"registry": _("Company Registry (CNPJ)"),
		"edit_in_org": _("Edit in organization"),
		"cnpj": _("CNPJ"),
		"registration_status": _("Registration Status"),
		"registry_phone": _("Registry Phone"),
		"registry_email": _("Registry Email"),
		"card_fields": _("Card fields"),
		"organization": _("Organization"),
		"deal_value": _("Deal Value"),
		"expected_closure_date": _("Expected Closure Date"),
		"next_step": _("Next Step"),
		"source": _("Source"),
		"contacts": _("Contacts"),
		"primary": _("Primary"),
		"no_contacts": _("No contact linked to this card."),
		"open_contact": _("Open contact in HubTech CRM"),
		"show_details": _("Show details"),
		"lost_title": _("Lost Reason"),
		"lost_text": _("Why was this card lost?"),
		"lost_choose": _("Choose a reason"),
		"lost_notes": _("Lost Notes"),
		"lost_required": _("Lost Reason is required"),
		"lost_notes_required": _("Describe the reason when it is \"Other\"."),
		"mark_lost": _("Mark as lost"),
		"cancel": _("Cancel"),
		"close": _("Close"),
		"error": _("Something went wrong. Please try again in a few minutes."),
	}
