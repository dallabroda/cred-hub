"""Frappe glue for the ERP integration (credtech#367): settings, client factory, token cache, connection test.

Credentials live in the Single "Cred Hub ERP Settings" (Password fields, only System Manager). One Frappe site per
client company, so the settings are already per company.
"""

import time

import frappe
from frappe import _

from . import PROVIDERS, SMART, WBA
from .base import ErpError, Token
from .mapper import DEFAULT_RULES
from .smart import SmartClient
from .wba import WbaClient

SETTINGS = "Cred Hub ERP Settings"
TOKEN_KEY = "cred_hub_erp_token"


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def _secret(settings, fieldname: str) -> str:
	return settings.get_password(fieldname, raise_exception=False) or ""


def forget_token():
	frappe.cache.delete_value(TOKEN_KEY)
	# New credentials or another ERP: the lists of names come from the new account
	frappe.cache.delete_value("cred_hub_erp_lists")
	frappe.cache.delete_value("cred_hub_erp_field_lists")


def _cached_token() -> Token:
	data = frappe.cache.get_value(TOKEN_KEY) or {}
	return Token(data.get("value") or "", float(data.get("expires_at") or 0))


def _store_token(token: Token):
	if token.value:
		ttl = max(int(token.expires_at - time.time()), 1)
		frappe.cache.set_value(TOKEN_KEY, {"value": token.value, "expires_at": token.expires_at}, expires_in_sec=ttl)


def load_default_rules(settings):
	for rule in DEFAULT_RULES.get(settings.erp_provider) or []:
		settings.append("cedente_field_map", rule)


def client(settings=None, session=None):
	"""The configured ERP client, reusing the token cached in Redis so each job does not log in again."""
	settings = settings or get_settings()
	provider = settings.erp_provider
	if provider == WBA:
		erp = WbaClient(
			settings.wba_tenant_id, settings.wba_username, _secret(settings, "wba_password"),
			base_url=settings.wba_base_url, session=session, token=_cached_token(), on_token=_store_token,
		)
	elif provider == SMART:
		erp = SmartClient(
			settings.smart_client_id, _secret(settings, "smart_client_secret"),
			base_url=settings.smart_base_url, session=session, token=_cached_token(), on_token=_store_token,
		)
	else:
		frappe.throw(_("Choose the ERP in the ERP integration settings."))
	return erp


def user_message(error: ErpError) -> str:
	"""Translated text for the admin; the ERP's own words (Portuguese already) go after the colon."""
	if error.code == "auth":
		return _("The ERP refused the login. Check the credentials and the user's API permissions.") + f" ({error})"
	if error.code == "network":
		return _("Could not reach the ERP. Try again in a few minutes.")
	if error.code == "maintenance":
		return _("The ERP is under maintenance. Try again later.")
	if error.code == "rate_limited":
		return _("The ERP asked to wait before the next call. Try again in a few minutes.")
	return _("The ERP answered with an error: {0}").format(error)


def log(provider: str, operation: str, status: str, *, document: str = "", reference=None, http_status=None,
		response=None, error: str = ""):
	"""Every ERP call worth auditing goes to Cred Hub Bureau Query, like the bureaus. Secrets never go in."""
	from cred_hub.integrations import bureau_log

	return bureau_log.record(
		provider, operation, status, document=document, reference=reference, http_status=http_status,
		response=response, error=error,
	)


@frappe.whitelist(methods=["POST"])
def test_connection() -> dict:
	"""Logs in with the saved credentials and runs the cheapest read. Admins only."""
	frappe.only_for("System Manager")
	settings = get_settings()
	if settings.erp_provider not in PROVIDERS:
		return {"ok": False, "message": _("Choose the ERP and save the credentials first.")}
	forget_token()
	from cred_hub.integrations.bureau_log import ERROR, SUCCESS

	try:
		result = client(settings).ping()
	except ErpError as e:
		log(settings.erp_provider, "Test connection", ERROR, http_status=e.http_status, error=str(e))
		return {"ok": False, "message": user_message(e)}
	log(settings.erp_provider, "Test connection", SUCCESS, response=result)
	return {"ok": True, "message": _("Connected to {0}.").format(settings.erp_provider), "details": result}


@frappe.whitelist(methods=["POST"])
def reset_field_map() -> list:
	"""Back to the suggested de-para of the chosen ERP. Admins only."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc(SETTINGS)
	settings.set("cedente_field_map", [])
	load_default_rules(settings)
	settings.set("title_field_map", [])
	settings.set("sacado_field_map", [])
	settings.set("cedente_registry_map", [])
	load_default_import_maps(settings)
	settings.save()
	return [row.as_dict() for row in settings.cedente_field_map]


@frappe.whitelist(methods=["POST"])
def send_cedente(deal: str) -> dict:
	"""Register the card's company as a cedente in the ERP now (credit funnel; credtech#367)."""
	from .cedente import send_now

	return send_now(deal)



# De-para screen (credtech#367) ----------------------------------------------------------------------------------

# CRM records a cedente field can come from (cred_hub.erp.cedente._records), with their doctypes
CEDENTE_SOURCES = {
	"organization": "CRM Organization",
	"contact": "Contact",
	"address": "Address",
	"deal": "CRM Deal",
}
SKIP_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "naming_series", "amended_from"}


def _fields_of(doctype) -> list[dict]:
	from .import_map import VALUE_TYPES

	return [
		{"fieldname": df.fieldname, "label": _(df.label or df.fieldname), "fieldtype": df.fieldtype}
		for df in frappe.get_meta(doctype).fields
		if df.fieldtype in VALUE_TYPES and df.fieldname not in SKIP_FIELDS and not df.hidden
	]


def cedente_sources() -> list[dict]:
	"""Every CRM field a cedente field can come from: "<entity>.<field>" with its screen name."""
	options = []
	for entity, doctype in CEDENTE_SOURCES.items():
		group = _(doctype.replace("CRM ", ""))
		options += [{"value": f"{entity}.{f['fieldname']}", "label": f"{group} › {f['label']}"} for f in _fields_of(doctype)]
	options.append({"value": "credit.approved_limit", "label": _("Credit committee") + " › " + _("Approved limit")})
	# Fields the default de-para uses that the screen list leaves out (hidden or read-only, e.g. Contact.full_name)
	from .mapper import DEFAULT_RULES

	known = {o["value"] for o in options}
	for rules in DEFAULT_RULES.values():
		for rule in rules:
			for source in (rule["source"] or "").split("|"):
				entity, _sep, fieldname = source.partition(".")
				doctype = CEDENTE_SOURCES.get(entity)
				if source in known or not doctype or not frappe.get_meta(doctype).has_field(fieldname):
					continue
				label = frappe.get_meta(doctype).get_label(fieldname)
				options.append({"value": source, "label": f"{_(doctype.replace('CRM ', ''))} › {_(label)}"})
				known.add(source)
	options.append({"value": "today", "label": _("Date of the registration")})
	return options


def title_targets() -> list[dict]:
	from .import_map import TITLE_ROW, allowed_title_field

	return [{"value": f["fieldname"], "label": f["label"]} for f in _fields_of(TITLE_ROW) if allowed_title_field(f["fieldname"])]


def sacado_targets(kind="sacado") -> list[dict]:
	options = []
	groups = (_("Cedente company"), _("Cedente contact")) if kind == "cedente" else (_("Sacado company"), _("Sacado person or contact"))
	for entity, doctype, group in (("organization", "CRM Organization", groups[0]), ("contact", "Contact", groups[1])):
		options += [{"value": f"{entity}.{f['fieldname']}", "label": f"{group} › {f['label']}"} for f in _fields_of(doctype) if f["fieldname"] not in ("cnpj", "cpf")]
	return options


@frappe.whitelist()
def mapping_options() -> dict:
	"""Everything the de-para screen offers: all the ERP's cedente fields, every column the ERP returns for títulos and
	sacados, and the CRM fields each one can go to. Admins only."""
	from . import catalog, import_map

	frappe.only_for("System Manager")
	settings = get_settings()
	provider = settings.erp_provider
	return {
		"cedente": {
			"fields": [{**f, "label": _(f["label"])} for f in catalog.fields(provider)],
			"sources": cedente_sources(),
		},
		"title": {
			"columns": import_map.columns(settings.erp_columns, import_map.TITLE, provider),
			"fixed": import_map.SMART_TITLE_FIXED if provider == SMART else {},
			"targets": title_targets(),
		},
		"sacado": {
			"columns": import_map.columns(settings.erp_columns, import_map.SACADO, provider),
			"fixed": import_map.SMART_SACADO_FIXED if provider == SMART else {},
			"targets": sacado_targets(),
		},
		"cedente_registry": {
			"columns": import_map.columns(settings.erp_columns, import_map.CEDENTE, provider),
			"fixed": import_map.SMART_CEDENTE_FIXED if provider == SMART else {},
			"targets": sacado_targets("cedente"),
		},
	}


@frappe.whitelist()
def mapping_lists() -> dict:
	"""Names of the ERP lists the cedente fields use (operator, originator...). Apart from mapping_options because
	it calls the ERP (slow the first time, then cached for an hour). Admins only."""
	from . import catalog

	frappe.only_for("System Manager")
	settings = get_settings()
	return _list_names(settings, catalog.fields(settings.erp_provider))


def _list_names(settings, fields) -> dict:
	"""Names of the ERP lists used by the cedente fields (operator, originator...), cached for an hour."""
	resources = sorted({f["resource"] for f in fields if f.get("resource")})
	if not resources or not settings.erp_enabled or settings.erp_provider != SMART:
		return {}
	key = "cred_hub_erp_field_lists"
	cached = frappe.cache.get_value(key)
	if cached:
		return cached
	names = {}
	try:
		erp = client(settings)
	except ErpError:
		return {}
	for resource in resources:
		try:
			names[resource] = sorted({row.get("descricao") for row in erp.lookup(resource) if row.get("descricao")})
		except ErpError:
			# A list this ERP account does not have (or a different name): offered empty, the rest still works
			names[resource] = []
	frappe.cache.set_value(key, names, expires_in_sec=3600)
	return names


def validate_maps(doc):
	"""Called by the settings controller: keeps only rules the ERP and the CRM know, fixes each cedente rule's format
	from the catalog and refuses a required cedente field without a source."""
	from . import catalog, import_map
	from .mapper import AUTO, ERP_LIST

	provider = doc.erp_provider
	known = {f["erp_field"]: f for f in catalog.fields(provider)}
	sources = {o["value"] for o in cedente_sources()}
	kept = []
	for row in doc.get("cedente_field_map") or []:
		spec = known.get(row.erp_field)
		if not spec:
			continue
		row.transform, row.required = spec["transform"], int(spec["required"] == catalog.REQUIRED)
		row.label = row.label or spec["label"]
		if spec["kind"] == catalog.AUTO_KIND:
			row.source = AUTO
		elif spec["kind"] == catalog.LIST:
			row.source = ERP_LIST
		else:
			alternatives = [s.strip() for s in (row.source or "").split("|") if s.strip()]
			unknown = [s for s in alternatives if s not in sources and not _custom_source(s)]
			if unknown:
				frappe.throw(_("The CRM field {0} does not exist.").format(unknown[0]))
			row.source = "|".join(alternatives)
		if not row.source and not (row.default_value or "").strip():
			if row.required:
				frappe.throw(_("The ERP field {0} is required: choose where it comes from.").format(_(spec["label"])))
			continue  # "do not send"
		if row.source == ERP_LIST and not (row.default_value or "").strip():
			continue
		kept.append(row)
	if provider:
		missing = [f for f in catalog.fields(provider) if f["required"] == catalog.REQUIRED and f["erp_field"] not in {r.erp_field for r in kept}]
		if missing and doc.get("cedente_field_map"):
			frappe.throw(_("The ERP field {0} is required: choose where it comes from.").format(_(missing[0]["label"])))
	doc.set("cedente_field_map", kept)

	for table, targets in (
		("title_field_map", title_targets()),
		("sacado_field_map", sacado_targets()),
		("cedente_registry_map", sacado_targets("cedente")),
	):
		allowed = {o["value"] for o in targets}
		kept = [r for r in doc.get(table) or [] if r.erp_column and r.crm_field in allowed]
		for row in doc.get(table) or []:
			if row.erp_column and row.crm_field and row.crm_field not in allowed:
				frappe.msgprint(
					_("The column {0} was left out: {1} cannot receive ERP data.").format(row.erp_column, row.crm_field),
					alert=True,
				)
		doc.set(table, kept)
	for table in ("title_field_map", "sacado_field_map", "cedente_registry_map"):
		seen = set()
		for row in doc.get(table):
			key = (import_map.column_key(row.erp_column), row.crm_field)
			if key in seen:
				frappe.throw(_("The column {0} is mapped twice to the same field.").format(row.erp_column))
			seen.add(key)
	for index, row in enumerate(doc.get("cedente_field_map"), 1):
		row.idx = index


def _custom_source(source) -> bool:
	"""A hidden or custom field typed by support in the Desk ("deal.custom_floating") stays accepted."""
	entity, _sep, fieldname = source.partition(".")
	doctype = CEDENTE_SOURCES.get(entity)
	return bool(doctype and fieldname and frappe.get_meta(doctype).has_field(fieldname))


def load_default_import_maps(settings):
	from .import_map import DEFAULT_SACADO_MAP, DEFAULT_TITLE_MAP

	for column, field in DEFAULT_TITLE_MAP.get(settings.erp_provider) or []:
		settings.append("title_field_map", {"erp_column": column, "crm_field": field})
	for column, field in DEFAULT_SACADO_MAP.get(settings.erp_provider) or []:
		settings.append("sacado_field_map", {"erp_column": column, "crm_field": field})


LISTS_KEY = "cred_hub_erp_lists"


@frappe.whitelist()
def erp_lists() -> dict:
	"""Names of the ERP's own managers and sectors, for the settings screen: the admin picks a name, never an id
	(credtech#367). Admins only; cached for an hour (each list is one paid-by-quota call to the ERP)."""
	frappe.only_for("System Manager")
	settings = get_settings()
	if settings.erp_provider != SMART or not settings.erp_enabled:
		return {"managers": [], "sectors": []}
	cached = frappe.cache.get_value(LISTS_KEY)
	if cached:
		return cached
	try:
		erp = client(settings)
		data = {
			"managers": sorted({m.get("descricao") for m in erp.lookup("gerente") if m.get("descricao")}),
			"sectors": [s.get("descricao") for s in erp.lookup("setor") if s.get("descricao")],
		}
	except ErpError as e:
		# Wrong credentials or ERP under maintenance: keep the error a minute, so each screen opening does not call
		# the ERP again (and spend its per-minute quota)
		data = {"managers": [], "sectors": [], "error": user_message(e)}
		frappe.cache.set_value(LISTS_KEY, data, expires_in_sec=60)
		return data
	frappe.cache.set_value(LISTS_KEY, data, expires_in_sec=3600)
	return data
