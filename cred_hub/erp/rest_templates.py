"""Templates of the generic REST connector (ADR 0007 §8, credtech#375): export a working configuration without the
client's address and secrets, and load a template into a new configuration. System Manager only.

A template carries the endpoints, pagination, item paths, field paths, outcome values and suggested de-para of a
system. It never carries the API address (`base_url`) nor any secret value: the configuration only has secret NAMES
(`{secret:token}`), the values stay in the encrypted `rest_secrets` table, which is not exported. A template only
becomes public after CredTech reviews it (curation is a process, not code).
"""

import json

import frappe
from frappe import _

from .connectors.rest import GENERIC
from .rest import ConfigError, RestConfig

SETTINGS = "Cred Hub ERP Settings"


@frappe.whitelist(methods=["GET", "POST"])
def export_template() -> dict:
	"""The saved generic REST configuration as a template (JSON object)."""
	frappe.only_for("System Manager")
	raw = frappe.db.get_single_value(SETTINGS, "rest_config")
	if not raw:
		frappe.throw(_("There is no REST configuration to export."))
	try:
		return RestConfig.parse(raw).template()
	except ConfigError as e:
		frappe.throw(_("REST configuration: {0}").format(str(e)))


@frappe.whitelist(methods=["POST"])
def import_template(template, base_url: str = "") -> dict:
	"""Loads a template as the site's generic REST configuration. The API address is the one given (or the one
	already saved); secrets are filled afterwards in the REST secrets table. Returns the secret names to fill."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc(SETTINGS)
	if not base_url and settings.get("rest_config"):
		try:
			base_url = json.loads(settings.rest_config).get("base_url") or ""
		except ValueError:
			base_url = ""
	if not (base_url or "").strip():
		frappe.throw(_("Inform the API URL of this client to load the template."))
	try:
		config = RestConfig.from_template(template, base_url)
	except ConfigError as e:
		frappe.throw(_("Template: {0}").format(str(e)))
	if settings.erp_provider != GENERIC:
		settings.erp_provider = GENERIC
	settings.rest_config = config.as_json()
	settings.save()
	filled = {(row.secret_name or "").strip() for row in settings.get("rest_secrets") or []}
	return {"system": config.system, "secrets_to_fill": [n for n in config.secret_names() if n not in filled]}
