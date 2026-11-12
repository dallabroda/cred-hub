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
		self.validate_payload_fields()

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
