"""Webhook do Cred Chat (evento `conversation_created`), um por empresa: cada site do Cred Hub tem o próprio
token, visto em Cred Hub Settings. URL: /api/method/cred_hub.chat.webhook.conversation_created?token=<token>
"""

import hmac

import frappe

from cred_hub.chat.leads import handle_conversation
from cred_hub.chat.payload import parse_conversation

SETTINGS = "Cred Hub Settings"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def conversation_created(token: str | None = None, **kwargs):
	expected = frappe.db.get_single_value(SETTINGS, "webhook_token") or ""
	# Com corpo JSON (caso do Cred Chat) o Frappe não põe a query string nos argumentos: lê da URL
	url_token = frappe.request.args.get("token") if frappe.request else None
	given = token or url_token or frappe.get_request_header("X-Cred-Hub-Token") or ""
	if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
		frappe.throw("Invalid token", frappe.AuthenticationError)

	settings = frappe.get_single(SETTINGS)
	if not settings.enable_chat_leads:
		return {"status": "disabled"}

	payload = frappe.request.get_json(silent=True) if frappe.request else None
	payload = payload or dict(kwargs)
	if payload.get("event") != "conversation_created":
		return {"status": "ignored", "reason": "event"}

	# O webhook chega como visitante; quem grava é o sistema
	frappe.set_user("Administrator")
	return handle_conversation(parse_conversation(payload), settings.chat_url, settings.chat_account_id or None)
