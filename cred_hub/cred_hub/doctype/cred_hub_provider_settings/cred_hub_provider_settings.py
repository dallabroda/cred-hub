# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint

from cred_hub.cnpj.mapping import only_digits


class CredHubProviderSettings(Document):
	# Assertiva Localize and CENPROT (credtech#290): cred_hub.integrations
	def validate(self):
		from cred_hub.integrations import assertiva, protest

		self.assertiva_cache_days = max(cint(self.assertiva_cache_days), 0)
		self.creditor_document = only_digits(self.creditor_document)
		self.creditor_zip = only_digits(self.creditor_zip)
		self.creditor_state = (self.creditor_state or "").strip().upper()
		if self.has_value_changed("assertiva_client_id") or self.has_value_changed("assertiva_client_secret"):
			assertiva.forget_token()
		if self.has_value_changed("cenprot_username") or self.has_value_changed("cenprot_password"):
			protest.forget_token()
		if self.cenprot_enabled and not self.collection_pipeline:
			protest.create_collection_pipeline(self)
