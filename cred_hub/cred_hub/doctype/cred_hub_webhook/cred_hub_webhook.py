# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

import secrets
import frappe
from frappe.model.document import Document


class CredHubWebhook(Document):
	def validate(self):
		if not self.secret:
			# Gera segredo HMAC automaticamente se não fornecido
			self.secret = secrets.token_hex(24)
