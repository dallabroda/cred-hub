"""Configurações do Cred Hub em cada empresa (site): integração com o Cred Chat."""

import frappe
from frappe.model.document import Document
from frappe.utils import get_url

WEBHOOK_METHOD = "/api/method/cred_hub.chat.webhook.conversation_created"


class CredHubSettings(Document):
	def validate(self):
		if not self.webhook_token:
			self.webhook_token = frappe.generate_hash(length=32)
		self.chat_url = (self.chat_url or "").strip().rstrip("/")
		self.webhook_url = get_url(f"{WEBHOOK_METHOD}?token={self.webhook_token}")
