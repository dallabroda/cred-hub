"""Generic REST connector (ADR 0007 §9, credtech#375): any system with a REST/JSON API, described by configuration.

The behavior comes from the site's configuration (Cred Hub ERP Settings: `rest_config` JSON + `rest_secrets` table),
parsed and enforced by `cred_hub.erp.rest`; the HTTP goes through `cred_hub.erp.safe_api` (ADR §6). What the
system can do (`capabilities`) and its limits (`pace`) are derived from which endpoints the configuration has, so
the shared code keeps asking the contract and never asks "is it the generic one?".

Class-level answers (capabilities, pace, catalog, default rules, columns) read the configuration of the current
site; `with_config(config)` binds one explicitly (tests, preview of a template).
"""

from datetime import date

from ..formats import digits
from ..rest import (
	ConfigError,
	RestClient,
	RestConfig,
	fill_body,
	format_date,
	json_path,
	registry_row,
	to_title,
)
from .base import (
	ALREADY,
	CEDENTE,
	CREATED,
	PENDING,
	SACADO,
	SENT,
	TITLE,
	Capabilities,
	Connector,
	Credential,
	Pace,
	RegisterOutcome,
	register,
)

GENERIC = "Generic REST"
SETTINGS = "Cred Hub ERP Settings"
# Set while the settings are being saved, so the class-level answers use the configuration being saved
SAVING_FLAG = "cred_hub_erp_rest_config"
PENDING_KEY = "cred_hub_erp_cedente_sent"


def _(text: str) -> str:
	try:
		import frappe

		return frappe._(text)
	except Exception:  # noqa: BLE001 - pure use (tests) has no translation
		return text


def site_config() -> RestConfig | None:
	"""The configuration of the current site when the generic connector is the chosen system, else None."""
	try:
		import frappe
	except ImportError:
		return None
	if not getattr(frappe.local, "site", None):
		return None
	saving = frappe.flags.get(SAVING_FLAG)
	if saving is not None:
		return saving or None
	try:
		settings = frappe.get_cached_doc(SETTINGS)
	except Exception:  # noqa: BLE001 - no settings yet (fresh site)
		return None
	if settings.get("erp_provider") != GENERIC or not (settings.get("rest_config") or "").strip():
		return None
	cache = getattr(frappe.local, "cred_hub_rest_configs", None)
	if cache is None:
		cache = frappe.local.cred_hub_rest_configs = {}
	raw = settings.rest_config
	if raw not in cache:
		try:
			cache[raw] = RestConfig.parse(raw)
		except ConfigError:
			cache[raw] = None
	return cache[raw]


def capabilities_of(config: RestConfig | None) -> Capabilities:
	if not config:
		return Capabilities()
	return Capabilities(
		register_cedente=config.has("register_cedente"),
		overdue_titles=config.has("overdue_titles"),
		settled_titles=config.has("settled_titles"),
		sacado_registry=config.has("sacados"),
		cedente_registry=config.has("cedentes"),
		lists=False,
	)


def pace_of(config: RestConfig | None) -> Pace:
	if not config:
		return Pace()
	pace = config.data["pace"]
	return Pace(report_cooldown=int(pace["report_cooldown"]), min_interval=float(pace["min_interval"]))


class _Configured(type):
	"""Class-level capabilities/pace come from the configuration (shared code reads `cls.capabilities`)."""

	@property
	def capabilities(cls) -> Capabilities:
		return capabilities_of(cls.current_config())

	@property
	def pace(cls) -> Pace:
		return pace_of(cls.current_config())


@register
class RestConnector(Connector, metaclass=_Configured):
	key = GENERIC
	label = "Generic REST"
	# The storage of the dynamic credentials (ADR §3): the configuration and the table of named secrets. The
	# secrets one configuration needs are `config.secret_names()` (the screen builds the form from them)
	credentials = (
		Credential("rest_config", "REST configuration", description="JSON: address, auth, endpoints, pagination"),
		Credential("rest_secrets", "REST secrets", secret=True, required=False, description="Name + secret value"),
	)
	config: RestConfig | None = None

	# Instance level: the configuration the client was built with
	capabilities = property(lambda self: capabilities_of(self.client.config))
	pace = property(lambda self: pace_of(self.client.config))

	@classmethod
	def current_config(cls) -> RestConfig | None:
		return cls.config if cls.config is not None else site_config()

	@classmethod
	def with_config(cls, config: RestConfig) -> type["RestConnector"]:
		return _Configured(f"{cls.__name__}Bound", (cls,), {"config": config, "__module__": cls.__module__})

	# Credentials ------------------------------------------------------------------------------------------------
	@classmethod
	def read_credentials(cls, settings, secret) -> dict:
		return {
			"rest_config": settings.get("rest_config") or "",
			"rest_secrets": {
				(row.secret_name or "").strip(): secret(row, "secret_value")
				for row in settings.get("rest_secrets") or []
				if (row.secret_name or "").strip()
			},
		}

	@classmethod
	def missing_credentials(cls, values: dict) -> list[str]:
		raw = values.get("rest_config")
		if not raw:
			return [cls.credentials[0].label]
		try:
			config = RestConfig.parse(raw)
		except ConfigError as e:
			return [f"{cls.credentials[0].label} ({e})"]
		secrets = values.get("rest_secrets") or {}
		return [name for name in config.secret_names() if not secrets.get(name)]

	@classmethod
	def validate_settings(cls, settings):
		import frappe

		raw = (settings.get("rest_config") or "").strip()
		if not raw:
			frappe.flags[SAVING_FLAG] = False
			return
		try:
			config = RestConfig.parse(raw)
		except ConfigError as e:
			frappe.throw(_("REST configuration: {0}").format(str(e)), title=_("Generic REST"))
		settings.rest_config = config.as_json()
		frappe.flags[SAVING_FLAG] = config
		names = [(row.secret_name or "").strip() for row in settings.get("rest_secrets") or []]
		if len(names) != len(set(names)):
			frappe.throw(_("Each REST secret needs a unique name."))
		missing = [n for n in config.secret_names() if n not in names]
		if missing:
			frappe.msgprint(_("Fill the REST secrets: {0}").format(", ".join(missing)), alert=True)

	@classmethod
	def settings_saved(cls, settings):
		import frappe

		# From now on the (fresh) cached settings are the source again
		frappe.flags.pop(SAVING_FLAG, None)
		frappe.local.cred_hub_rest_configs = {}

	@classmethod
	def make_client(cls, values: dict, **kwargs):
		from ..base import ErpAuthError, ErpError

		raw = values.get("rest_config") or (cls.config.data if cls.config is not None else None)
		try:
			config = RestConfig.parse(raw)
		except ConfigError as e:
			raise ErpError(f"The REST configuration is not valid: {e}", "config") from None
		secrets = values.get("rest_secrets") or {}
		missing = [name for name in config.secret_names() if not secrets.get(name)]
		if missing:
			raise ErpAuthError(f"Fill the REST secrets: {', '.join(missing)}.")
		return RestClient(config, secrets, **kwargs)

	# Description ------------------------------------------------------------------------------------------------
	@classmethod
	def catalog(cls) -> list[dict]:
		config = cls.current_config()
		return [
			{"erp_field": f["erp_field"], "label": f["label"], "kind": "data", "transform": f["transform"],
			 "required": "yes" if f["required"] else ""}
			for f in (config.register_fields() if config else [])
		]

	@classmethod
	def default_rules(cls) -> list[dict]:
		config = cls.current_config()
		return [
			{"erp_field": f["erp_field"], "source": f["source"], "transform": f["transform"],
			 "default_value": f["default_value"], "required": int(f["required"]), "label": f["label"]}
			for f in (config.register_fields() if config else [])
			if f["source"] or f["default_value"] not in (None, "")
		]

	@classmethod
	def _spec(cls, kind: str) -> dict | None:
		config = cls.current_config()
		op = {TITLE: "overdue_titles", SACADO: "sacados", CEDENTE: "cedentes"}.get(kind)
		return config.endpoint(op) if config and op else None

	@classmethod
	def documented_columns(cls, kind: str) -> list[str]:
		spec = cls._spec(kind)
		if not spec:
			return []
		if kind == TITLE:
			return list(dict.fromkeys(spec["fields"].values()))
		return [c for c in (spec["document"], spec.get("name")) if c]

	@classmethod
	def fixed_columns(cls, kind: str) -> dict:
		spec = cls._spec(kind)
		if not spec:
			return {}
		if kind == TITLE:
			identity = {"erp_id": "erp_title_id", "due_date": "due_date", "sacado_document": "sacado",
				"sacado_name": "sacado", "cedente_document": "cedente", "cedente_name": "cedente"}
			return {spec["fields"][k]: what for k, what in identity.items() if k in spec["fields"]}
		return {"CPF/CNPJ": "document", **({"Nome": "name"} if spec.get("name") else {})}

	def finalize_cedente(self, payload: dict):
		"""Nothing system-specific: the configured body template wraps the de-para payload when it is sent."""
		return dict(payload)

	# Operations -------------------------------------------------------------------------------------------------
	def _endpoint(self, op: str) -> dict:
		spec = self.client.config.endpoint(op)
		if not spec:
			raise NotImplementedError(op)
		return spec

	def ping(self) -> dict:
		return self.client.ping()

	def _titles(self, op: str, values: dict):
		spec = self._endpoint(op)
		fmt = spec.get("date_format")
		values = {k: format_date(v, fmt) for k, v in values.items()}
		for item in self.client.pages(spec, values):
			yield to_title(item, spec, self.key)

	def overdue_titles(self, until):
		return self._titles("overdue_titles", {"until": until, "today": date.today()})

	def settled_titles(self, since, until):
		return self._titles("settled_titles", {"since": since, "until": until, "today": date.today()})

	def _registry(self, op: str):
		spec = self._endpoint(op)
		for item in self.client.pages(spec, {"today": format_date(date.today(), "iso")}):
			yield registry_row(item, spec)

	def sacados(self):
		return self._registry("sacados")

	def cedentes(self):
		return self._registry("cedentes")

	def register_cedente(self, context) -> RegisterOutcome:
		document = digits(context.organization.get("cnpj"))
		return self.send_registration(self.finalize_cedente(context.result.payload), document, _pending_store())

	def send_registration(self, payload: dict, document: str, pending) -> RegisterOutcome:
		"""Check first (lookup), never resend while waiting (pending marker), then send once."""
		from ..base import ErpError

		spec = self._endpoint("register_cedente")
		values = {"document": document, "today": format_date(date.today(), "iso")}
		system = self.client.config.system or _("the system")
		lookup = spec.get("lookup")
		if lookup:
			found = self._lookup(lookup, values)
			if found is not None:
				return RegisterOutcome(ALREADY, found or None, _(
					"The cedente was already registered in {0} (code {1}). Nothing was sent."
				).format(system, found or "-"))
		marker = f"{PENDING_KEY}:{self.key}:{document}"
		if pending.get(marker):
			return RegisterOutcome(PENDING, None, _(
				"The cedente was already sent to {0} and is waiting for it to register. Nothing was sent again."
			).format(system))
		body = fill_body(spec.get("body", "{{payload}}"), payload, values)
		already = spec["already"]
		try:
			status, answer = self.client.call(spec, values, body=body)
		except ErpError as e:
			text = str(e).lower()
			if e.http_status in already["status"] or any(signal in text for signal in already["contains"]):
				return RegisterOutcome(ALREADY, None, _(
					"{0} answered that the cedente already exists. Nothing else was sent."
				).format(system))
			raise
		text = str(answer).lower() if answer is not None else ""
		if status in already["status"] or any(signal in text for signal in already["contains"]):
			return RegisterOutcome(ALREADY, None, _(
				"{0} answered that the cedente already exists. Nothing else was sent."
			).format(system))
		code = json_path(answer, spec["id_path"]) if spec["id_path"] else None
		if code not in (None, ""):
			return RegisterOutcome(CREATED, str(code), _("Cedente registered in {0} (code {1}).").format(system, code))
		pending.set(marker)
		return RegisterOutcome(SENT, None, _(
			"Cedente registration sent to {0}. The system did not return a code: confirm it there."
		).format(system))

	def _lookup(self, spec: dict, values: dict):
		"""The code of the existing registration, "" when it exists without a code, None when it does not exist."""
		from ..base import ErpError

		try:
			_status, answer = self.client.call(spec, values)
		except ErpError as e:
			if e.http_status == 404:
				return None
			raise
		found = json_path(answer, spec.get("items") or "")
		if isinstance(found, list):
			found = found[0] if found else None
		if not found:
			return None
		code = json_path(found, spec["id_path"]) if spec.get("id_path") else None
		return str(code) if code not in (None, "") else ""


class _CachePending:
	"""Marks a cedente sent without a code for 24 h, shared by every worker (Redis), like the WBA one."""

	def get(self, key):
		import frappe

		return frappe.cache.get_value(key)

	def set(self, key):
		import frappe

		frappe.cache.set_value(key, 1, expires_in_sec=24 * 3600)


def _pending_store():
	return _CachePending()
