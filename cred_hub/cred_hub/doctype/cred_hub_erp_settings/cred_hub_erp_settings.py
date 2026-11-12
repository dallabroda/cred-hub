# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class CredHubERPSettings(Document):
	# WBA / Smart Securities integration (credtech#367): cred_hub.erp
	def validate(self):
		from cred_hub.erp import settings

		self.overdue_days = max(cint(self.overdue_days), 0)
		self.paid_lookback_days = max(cint(self.paid_lookback_days), 1)
		for field in ("wba_base_url", "smart_base_url", "qitech_base_url"):
			url = (self.get(field) or "").strip().rstrip("/")
			if url and not url.lower().startswith("https://"):
				# Credentials and tokens travel on every call: never in clear text
				frappe.throw(_("The ERP API URL must start with https://"))
			self.set(field, url)
		credentials = ("erp_provider", "wba_base_url", "wba_tenant_id", "wba_username", "wba_password",
			"smart_base_url", "smart_client_id", "smart_client_secret", "rest_config", "rest_secrets", "qitech_base_url",
			"qitech_api_key", "qitech_private_key", "qitech_algorithm")
		cls = settings.connector_class(self)
		if cls:
			# What only the chosen connector knows (e.g. the generic REST configuration), before the maps below
			cls.validate_settings(self)
		if any(self.has_value_changed(field) for field in credentials):
			settings.forget_token()
		if self.has_value_changed("erp_provider"):
			# Rules are ERP-specific: switching ERP starts from that ERP's defaults
			self.set("cedente_field_map", [])
			self.set("title_field_map", [])
			self.set("sacado_field_map", [])
			self.set("cedente_registry_map", [])
			self.erp_columns = None
		if self.erp_provider and not self.cedente_field_map:
			settings.load_default_rules(self)
		if self.erp_provider and self.has_value_changed("erp_provider"):
			settings.load_default_import_maps(self)
		settings.validate_maps(self)
		self.validate_cedente_trigger()

	def on_update(self):
		from cred_hub.erp import settings

		cls = settings.connector_class(self)
		if cls:
			cls.settings_saved(self)

	def validate_cedente_trigger(self):
		"""Cedente registration (credtech#367): only the credit funnel, and the stage must belong to it."""
		if not self.get("cedente_send_enabled"):
			return
		if not self.get("cedente_pipeline"):
			self.cedente_pipeline = frappe.db.get_single_value("Cred Hub Credit Settings", "credit_pipeline")
		if not self.get("cedente_pipeline"):
			frappe.throw(_("Choose the credit funnel for the cedente registration."))
		from cred_hub.erp.settings import is_sandbox_pipeline

		if is_sandbox_pipeline(self.cedente_pipeline):
			frappe.throw(_("The Sandbox funnel holds test data and never registers cedentes in the ERP."))
		collection = frappe.db.get_single_value("Cred Hub Provider Settings", "collection_pipeline")
		if collection and self.cedente_pipeline == collection:
			frappe.throw(_("The cedente registration is for the credit funnel: collection cedentes are already in the ERP."))
		if self.get("cedente_trigger") == "Card enters a stage":
			if not self.get("cedente_stage"):
				frappe.throw(_("Choose the stage that sends the cedente to the ERP."))
			if frappe.db.get_value("CRM Deal Status", self.cedente_stage, "pipeline") != self.cedente_pipeline:
				frappe.throw(_("The stage must belong to the chosen funnel."))
