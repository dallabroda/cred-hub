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
	settings.save()
	return [row.as_dict() for row in settings.cedente_field_map]


@frappe.whitelist(methods=["POST"])
def send_cedente(deal: str) -> dict:
	"""Register the card's company as a cedente in the ERP now (credit funnel; credtech#367)."""
	from .cedente import send_now

	return send_now(deal)
