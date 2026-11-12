# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document


class CredHubWebhook(Document):
	def validate(self):
		if not self.secret:
			# Gera segredo HMAC automaticamente se não fornecido
			self.secret = secrets.token_hex(24)
		self.validate_url()
		self.validate_payload_fields()

	def validate_url(self):
		"""Only https and a public host name (audit L8): the body carries personal data and is signed with the
		secret; localhost and private addresses are refused here and again, resolved, at delivery."""
		from cred_hub.email_company import safe_http

		url = (self.webhook_url or "").strip()
		if not url.lower().startswith("https://"):
			frappe.throw(_("The webhook address must start with https://"))
		try:
			safe_http.check_url(url)
		except safe_http.BlockedAddress:
			frappe.throw(_("This webhook address is not allowed (local or private network)."))
		self.webhook_url = url

	def validate_payload_fields(self):
		"""Only fields the payload policy lets out (cred_hub.api_schema); a typo or a denied field is refused here,
		not silently dropped at delivery time."""
		from cred_hub.api_schema import allowed_payload_keys
		from cred_hub.webhooks import parse_payload_fields

		fields = parse_payload_fields(self.get("payload_fields"))
		unknown = [f for f in fields if f not in allowed_payload_keys()]
		if unknown:
			frappe.throw(_("These fields cannot be sent by a webhook: {0}").format(", ".join(unknown)))
		self.payload_fields = "\n".join(fields)

	def on_update(self):
		from cred_hub.webhooks import clear_active_cache

		clear_active_cache()

	def on_trash(self):
		from cred_hub.webhooks import clear_active_cache

		clear_active_cache()
