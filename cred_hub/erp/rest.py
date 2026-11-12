"""Generic configurable REST connector: the engine (ADR 0007 §6, §8, §9; credtech#375). Pure Python, no Frappe.

A client company whose system has no native connector describes its API in a JSON configuration (stored on
Cred Hub ERP Settings, field `rest_config`; secrets in the `rest_secrets` table). Everything that is not a secret is
in the configuration; secrets are referenced by name, as `{secret:NAME}`, and never stored in it. Shape:

	{
	  "version": 1,
	  "system": "Acme ERP",                         # name shown to the admin
	  "base_url": "https://api.acme.com.br/v2",     # https only (ADR §6)
	  "auth": {"type": "bearer", "token": "{secret:token}"}
	        | {"type": "header", "header": "X-Api-Key", "value": "{secret:api_key}"}
	        | {"type": "basic", "username": "{secret:user}", "password": "{secret:password}"}
	        | {"type": "oauth2", "token_url": "https://auth.acme.com.br/oauth/token", "client_id": "{secret:id}",
	           "client_secret": "{secret:secret}", "scope": "read", "client_auth": "body" | "basic"}
	        | {"type": "none"},
	  "headers": {"X-Tenant": "{secret:tenant}"},   # extra headers (allow-list of names, safe_api.check_header)
	  "pace": {"min_interval": 1, "report_cooldown": 0},   # seconds between calls / between two heavy reports
	  "limits": {"timeout": 30, "max_bytes": 10485760, "max_pages": 500, "max_redirects": 0},
	  "endpoints": {                                # an operation not configured is a capability the system lacks
	    "ping": {"path": "/health"},
	    "overdue_titles": {
	      "method": "GET", "path": "/titulos",
	      "query": {"situacao": "aberto", "vencimento_ate": "{until}"},   # placeholders: {until} {since} {today}
	      "date_format": "br",                     # how {until}/{since} are written: iso | br | iso_datetime | strftime
	      "items": "data.items",                   # where the list is in the answer ("" = the answer is the list)
	      "pagination": {"type": "page", "page_param": "page", "size_param": "size", "size": 100, "start": 1,
	                     "last_path": "last", "total_pages_path": "totalPages"},
	      "fields": {"erp_id": "id", "number": "numero", "due_date": "vencimento", "amount": "valor",
	                 "sacado_document": "sacado.cnpj", "sacado_name": "sacado.nome", "situation": "status", ...},
	      "outcomes": {"paid": ["LIQUIDADO"], "repurchased": ["RECOMPRADO"]},  # value of `situation` -> outcome
	      "default_outcome": "open"
	    },
	    "settled_titles": {... same, with {since} and {until}, default_outcome "paid"},
	    "sacados": {"path": "/sacados", "items": "data", "pagination": {...}, "document": "cnpj", "name": "razao"},
	    "cedentes": {... same as sacados},
	    "register_cedente": {
	      "method": "POST", "path": "/cedentes",
	      "body": {"cedente": "{{payload}}"},     # "{{payload}}" / "{{payload.a.b}}" / "{document}" are filled in
	      "id_path": "data.id",                    # where the created id is (empty answer -> "sent")
	      "already": {"status": [409], "contains": ["já cadastrado"]},   # answers meaning "it already existed"
	      "lookup": {"path": "/cedentes", "query": {"cnpj": "{document}"}, "items": "data", "id_path": "id"},
	      "fields": [{"erp_field": "cnpj", "label": "CNPJ", "transform": "digits", "required": true,
	                  "source": "organization.cnpj"}, ...]                  # catalog + suggested de-para
	    }
	  }
	}

Pagination types: none | page | offset (offset_param, limit_param, limit, total_path) | cursor (cursor_param,
cursor_path) | next_link (next_path; same host only). Every kind stops at the end, at `max_pages`, and refuses a page
that repeats the previous one (a misconfigured pagination never loops forever).
"""

import base64
import hashlib
import json
import re
import time
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlsplit

import requests

from . import safe_api
from .base import (
	BaseClient,
	ErpAuthError,
	ErpError,
	ErpMaintenance,
	ErpRateLimited,
	Token,
	error_text,
	json_or_none,
)
from .formats import digits, to_date, to_decimal

VERSION = 1
OPERATIONS = ("ping", "overdue_titles", "settled_titles", "sacados", "cedentes", "register_cedente")
TITLE_OPERATIONS = ("overdue_titles", "settled_titles")
REGISTRY_OPERATIONS = ("sacados", "cedentes")
AUTH_TYPES = ("none", "bearer", "header", "basic", "oauth2")
PAGINATION_TYPES = ("none", "page", "offset", "cursor", "next_link")
METHODS = ("GET", "POST", "PUT", "PATCH")
DATE_PRESETS = {"iso": "%Y-%m-%d", "br": "%d/%m/%Y", "iso_datetime": "%Y-%m-%dT00:00:00", "compact": "%Y%m%d"}
_STRFTIME_OK = re.compile(r"^(?:%[dmYyHMS]|[^%])+$")
# Placeholders each operation may use (besides {secret:NAME}, allowed only in auth and headers)
PLACEHOLDERS = {
	"ping": {"today"},
	"overdue_titles": {"until", "today"},
	"settled_titles": {"since", "until", "today"},
	"sacados": {"today"},
	"cedentes": {"today"},
	"register_cedente": {"document", "today"},
	"lookup": {"document", "today"},
}
_PLACEHOLDER = re.compile(r"\{(secret:[A-Za-z0-9_.-]{1,64}|[a-z_]{2,20})\}")
_PAYLOAD = re.compile(r"^\{\{payload(?:\.([A-Za-z0-9_.\[\]-]+))?\}\}$")
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

# Common título keys a configuration maps (erp, outcome and raw are filled by the engine)
TITLE_FIELDS = (
	"erp_id", "operation", "number", "our_number", "kind", "sacado_document", "sacado_name", "cedente_document",
	"cedente_name", "issue_date", "original_due_date", "due_date", "amount", "situation", "digitable_line",
	"paid_date", "paid_amount",
)
DATE_FIELDS = {"issue_date", "original_due_date", "due_date", "paid_date"}
MONEY_FIELDS = {"amount", "paid_amount"}
DIGIT_FIELDS = {"sacado_document", "cedente_document", "digitable_line"}
OUTCOMES = ("open", "paid", "repurchased")

DEFAULT_LIMITS = {"timeout": 30, "max_bytes": 10 * 1024 * 1024, "max_pages": 500, "max_redirects": 0}
MAX_LIMITS = {"timeout": 120, "max_bytes": 50 * 1024 * 1024, "max_pages": 10000, "max_redirects": 3}
# What a template never carries (ADR §8): the client's address and anything client-specific
TEMPLATE_DROPPED = ("base_url",)


class ConfigError(ValueError):
	"""The configuration is not usable; the message tells the admin what to fix (English, translated by the UI)."""


# JSON paths -----------------------------------------------------------------------------------------------------


def path_tokens(path: str) -> list:
	if not path:
		return []
	tokens = []
	for name, index in _PATH_TOKEN.findall(path):
		tokens.append(int(index) if index else name)
	if "".join(f"[{t}]" if isinstance(t, int) else f".{t}" for t in tokens).lstrip(".") != path:
		raise ConfigError(f"Invalid path {path!r}: use names separated by dots and [n] for list positions.")
	return tokens


def json_path(data, path: str):
	"""Value at `path` ("data.items", "result[0].id"; "" = data itself), or None when any step is missing."""
	value = data
	for token in path_tokens(path):
		if isinstance(token, int):
			if not isinstance(value, list) or token >= len(value):
				return None
			value = value[token]
		else:
			if not isinstance(value, dict) or token not in value:
				return None
			value = value[token]
	return value


def flatten(item, prefix: str = "", out: dict | None = None, depth: int = 0) -> dict:
	"""Nested JSON -> {"a.b": v, "c[0].d": v}: the de-para picks columns by name, so nested fields get one too."""
	out = {} if out is None else out
	if isinstance(item, dict) and depth < 6:
		for key, value in item.items():
			flatten(value, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)
	elif isinstance(item, list) and depth < 6 and item and all(isinstance(v, dict) for v in item):
		for index, value in enumerate(item[:20]):
			flatten(value, f"{prefix}[{index}]", out, depth + 1)
	else:
		out[prefix or "value"] = item
	return out


# Dates and placeholders -----------------------------------------------------------------------------------------


def date_pattern(fmt: str | None) -> str:
	fmt = (fmt or "iso").strip()
	if fmt in DATE_PRESETS:
		return DATE_PRESETS[fmt]
	if fmt == "epoch":
		return fmt
	if not _STRFTIME_OK.match(fmt):
		raise ConfigError(f"Invalid date format {fmt!r}: use iso, br, iso_datetime, compact, epoch or %d %m %Y %H %M %S.")
	return fmt


def format_date(value, fmt: str | None) -> str:
	value = to_date(value) if not isinstance(value, date) else value
	pattern = date_pattern(fmt)
	if pattern == "epoch":
		return str(int(datetime(value.year, value.month, value.day).timestamp()))
	return value.strftime(pattern)


def placeholders_in(value) -> set:
	found = set()
	if isinstance(value, str):
		if not _PAYLOAD.match(value.strip()):
			found.update(_PLACEHOLDER.findall(value))
	elif isinstance(value, dict):
		for v in value.values():
			found |= placeholders_in(v)
	elif isinstance(value, list):
		for v in value:
			found |= placeholders_in(v)
	return found


def fill(text, values: dict, secrets: dict | None = None, quote_values: bool = False):
	"""Replaces {name} and {secret:NAME} in a string. Unknown names were refused when the config was parsed."""
	if not isinstance(text, str):
		return text

	def replace(match):
		name = match.group(1)
		if name.startswith("secret:"):
			secret = (secrets or {}).get(name[7:])
			if not secret:
				raise ErpAuthError(f"The secret {name[7:]!r} is empty: fill it in the connector secrets.")
			value = secret
		elif name in values:
			value = values[name]
		else:
			return match.group(0)
		value = "" if value is None else str(value)
		return quote(value, safe="") if quote_values else value

	return _PLACEHOLDER.sub(replace, text)


def fill_body(template, payload, values: dict):
	"""Request body from the template: "{{payload}}" -> the whole payload, "{{payload.a.b}}" -> one value."""
	if isinstance(template, dict):
		return {k: fill_body(v, payload, values) for k, v in template.items()}
	if isinstance(template, list):
		return [fill_body(v, payload, values) for v in template]
	if isinstance(template, str):
		match = _PAYLOAD.match(template.strip())
		if match:
			return payload if not match.group(1) else json_path(payload, match.group(1))
		return fill(template, values)
	return template


# Configuration --------------------------------------------------------------------------------------------------


def _int(value, name, low, high, default):
	if value in (None, ""):
		return default
	try:
		number = float(value)
	except (TypeError, ValueError):
		raise ConfigError(f"{name} must be a number.") from None
	if not low <= number <= high:
		raise ConfigError(f"{name} must be between {low} and {high}.")
	return number


def _check_base_url(url: str, what: str) -> str:
	url = (url or "").strip().rstrip("/")
	if not url:
		raise ConfigError(f"Fill the {what}.")
	try:
		safe_api.check_url(url)
	except safe_api.BlockedAddress as e:
		raise ConfigError(f"The {what} is not allowed ({e}). Use https:// and the public name of the API.") from None
	if urlsplit(url).query or urlsplit(url).fragment:
		raise ConfigError(f"The {what} cannot have a query string.")
	return url


class RestConfig:
	"""A parsed and validated configuration. `data` is the normalized dict (what is saved and exported)."""

	def __init__(self, data: dict):
		self.data = data

	# Parsing ----------------------------------------------------------------------------------------------------
	@classmethod
	def parse(cls, raw, require_url: bool = True) -> "RestConfig":
		if isinstance(raw, (str, bytes)):
			if not raw.strip():
				raise ConfigError("The REST configuration is empty.")
			try:
				raw = json.loads(raw)
			except ValueError as e:
				raise ConfigError(f"The REST configuration is not valid JSON: {e}") from None
		if not isinstance(raw, dict):
			raise ConfigError("The REST configuration must be a JSON object.")
		data = json.loads(json.dumps(raw))  # deep copy, JSON only
		unknown = set(data) - {"version", "system", "base_url", "auth", "headers", "pace", "limits", "endpoints", "notes"}
		if unknown:
			raise ConfigError(f"Unknown configuration keys: {', '.join(sorted(unknown))}.")
		if int(data.get("version") or VERSION) != VERSION:
			raise ConfigError(f"Configuration version {data.get('version')} is not supported.")
		data["version"] = VERSION
		data["system"] = str(data.get("system") or "").strip()[:80]
		if require_url or data.get("base_url"):
			data["base_url"] = _check_base_url(data.get("base_url"), "API URL")
		else:
			data.pop("base_url", None)
		data["auth"] = cls._auth(data.get("auth") or {"type": "none"})
		headers = data.get("headers") or {}
		if not isinstance(headers, dict):
			raise ConfigError("headers must be an object {name: value}.")
		for name, value in headers.items():
			try:
				safe_api.check_header(name, value)
			except safe_api.BlockedAddress as e:
				raise ConfigError(f"Header not allowed: {e}") from None
			if name.lower() == "authorization":
				raise ConfigError("Set the Authorization header through auth, not headers.")
		data["headers"] = {str(k): str(v) for k, v in headers.items()}
		pace = data.get("pace") or {}
		data["pace"] = {
			"min_interval": _int(pace.get("min_interval"), "pace.min_interval", 0, 60, 0),
			"report_cooldown": int(_int(pace.get("report_cooldown"), "pace.report_cooldown", 0, 86400, 0)),
		}
		limits = data.get("limits") or {}
		data["limits"] = {
			key: int(_int(limits.get(key), f"limits.{key}", 0 if key == "max_redirects" else 1, MAX_LIMITS[key], default))
			for key, default in DEFAULT_LIMITS.items()
		}
		endpoints = data.get("endpoints") or {}
		if not isinstance(endpoints, dict):
			raise ConfigError("endpoints must be an object.")
		unknown = set(endpoints) - set(OPERATIONS)
		if unknown:
			raise ConfigError(f"Unknown operations: {', '.join(sorted(unknown))}. Use {', '.join(OPERATIONS)}.")
		if "ping" not in endpoints:
			raise ConfigError("Configure the ping endpoint (the cheapest call that proves the access works).")
		data["endpoints"] = {op: cls._endpoint(op, spec) for op, spec in endpoints.items()}
		if bool("overdue_titles" in endpoints) != bool("settled_titles" in endpoints):
			raise ConfigError("The collection sync needs both overdue_titles and settled_titles.")
		return cls(data)

	@staticmethod
	def _auth(auth: dict) -> dict:
		if not isinstance(auth, dict):
			raise ConfigError("auth must be an object.")
		kind = auth.get("type") or "none"
		if kind not in AUTH_TYPES:
			raise ConfigError(f"Unknown auth type {kind!r}. Use {', '.join(AUTH_TYPES)}.")
		need = {
			"none": (), "bearer": ("token",), "header": ("header", "value"), "basic": ("username", "password"),
			"oauth2": ("token_url", "client_id", "client_secret"),
		}[kind]
		for key in need:
			if not str(auth.get(key) or "").strip():
				raise ConfigError(f"auth.{key} is required for {kind}.")
		secret_keys = {"token", "value", "password", "client_secret"}
		for key in secret_keys & set(need):
			if not re.fullmatch(r"\{secret:[A-Za-z0-9_.-]{1,64}\}", str(auth[key]).strip()):
				raise ConfigError(f"auth.{key} must reference a secret, like {{secret:token}}: never the value itself.")
		if kind == "header":
			try:
				safe_api.check_header(auth["header"], "x")
			except safe_api.BlockedAddress as e:
				raise ConfigError(f"Header not allowed: {e}") from None
		if kind == "oauth2":
			auth["token_url"] = _check_base_url(auth["token_url"], "OAuth2 token URL")
			auth["client_auth"] = auth.get("client_auth") or "body"
			if auth["client_auth"] not in ("body", "basic"):
				raise ConfigError("auth.client_auth must be body or basic.")
		auth["type"] = kind
		return auth

	@classmethod
	def _endpoint(cls, op: str, spec: dict, allowed: set | None = None) -> dict:
		if not isinstance(spec, dict):
			raise ConfigError(f"{op} must be an object.")
		method = (spec.get("method") or ("POST" if op == "register_cedente" else "GET")).upper()
		if method not in METHODS:
			raise ConfigError(f"{op}.method must be one of {', '.join(METHODS)}.")
		path = str(spec.get("path") or "").strip()
		if not path.startswith("/") or "://" in path or "?" in path or "#" in path or ".." in path:
			raise ConfigError(f"{op}.path must start with / and have no query string (use query).")
		query = spec.get("query") or {}
		if not isinstance(query, dict):
			raise ConfigError(f"{op}.query must be an object.")
		allowed = PLACEHOLDERS[op] if allowed is None else allowed
		used = placeholders_in([path, query, spec.get("body")])
		if any(p.startswith("secret:") for p in used):
			raise ConfigError(f"{op}: secrets go only in auth and headers, never in the path, query or body.")
		wrong = sorted(used - allowed)
		if wrong:
			raise ConfigError(f"{op} cannot use {', '.join('{' + w + '}' for w in wrong)}.")
		result = {"method": method, "path": path, "query": {str(k): v for k, v in query.items()}}
		if "body" in spec:
			result["body"] = spec["body"]
		if "date_format" in spec or op in TITLE_OPERATIONS:
			date_pattern(spec.get("date_format"))
			result["date_format"] = spec.get("date_format") or "iso"
		if op != "ping" or "items" in spec:
			result["items"] = str(spec.get("items") or "")
			path_tokens(result["items"])
		if op in TITLE_OPERATIONS + REGISTRY_OPERATIONS:
			result["pagination"] = cls._pagination(op, spec.get("pagination") or {"type": "none"})
		if op in TITLE_OPERATIONS:
			fields = spec.get("fields") or {}
			if not isinstance(fields, dict) or not fields.get("erp_id") or not fields.get("due_date"):
				raise ConfigError(f"{op}.fields must map at least erp_id and due_date.")
			unknown = set(fields) - set(TITLE_FIELDS)
			if unknown:
				raise ConfigError(f"{op}.fields has unknown keys: {', '.join(sorted(unknown))}.")
			for value in fields.values():
				path_tokens(str(value))
			result["fields"] = {k: str(v) for k, v in fields.items() if v}
			outcomes = spec.get("outcomes") or {}
			if not isinstance(outcomes, dict) or set(outcomes) - set(OUTCOMES):
				raise ConfigError(f"{op}.outcomes maps open / paid / repurchased to lists of values.")
			result["outcomes"] = {
				k: [str(v).strip().lower() for v in (vals if isinstance(vals, list) else [vals])] for k, vals in outcomes.items()
			}
			default = spec.get("default_outcome") or ("open" if op == "overdue_titles" else "paid")
			if default not in OUTCOMES:
				raise ConfigError(f"{op}.default_outcome must be open, paid or repurchased.")
			result["default_outcome"] = default
		if op in REGISTRY_OPERATIONS:
			if not spec.get("document"):
				raise ConfigError(f"{op}.document: where the CPF/CNPJ is in each item.")
			result["document"] = str(spec["document"])
			result["name"] = str(spec.get("name") or "")
			path_tokens(result["document"])
			path_tokens(result["name"])
		if op == "register_cedente":
			cls._register(spec, result)
		return result

	@classmethod
	def _register(cls, spec: dict, result: dict):
		result["id_path"] = str(spec.get("id_path") or "")
		path_tokens(result["id_path"])
		already = spec.get("already") or {}
		result["already"] = {
			"status": [int(s) for s in already.get("status") or []],
			"contains": [str(s).lower() for s in already.get("contains") or [] if str(s).strip()],
		}
		if spec.get("lookup"):
			lookup = cls._endpoint("lookup", {**spec["lookup"], "method": spec["lookup"].get("method") or "GET"})
			lookup["id_path"] = str(spec["lookup"].get("id_path") or "")
			path_tokens(lookup["id_path"])
			result["lookup"] = lookup
		fields = spec.get("fields") or []
		if not fields or not isinstance(fields, list):
			raise ConfigError("register_cedente.fields lists the fields the system accepts (erp_field, label...).")
		from .mapper import TRANSFORMS

		clean, seen = [], set()
		for f in fields:
			name = str((f or {}).get("erp_field") or "").strip()
			if not name or name in seen:
				raise ConfigError("Each register_cedente field needs a unique erp_field.")
			# "none" is the engine's alias of "" (the de-para rows only accept "")
			transform = "" if f.get("transform") in (None, "none") else f.get("transform")
			if transform not in TRANSFORMS:
				raise ConfigError(f"Unknown transform {transform!r} in {name}.")
			seen.add(name)
			clean.append({
				"erp_field": name, "label": str(f.get("label") or name), "transform": transform,
				"required": bool(f.get("required")), "source": str(f.get("source") or ""),
				"default_value": f.get("default_value"),
			})
		result["fields"] = clean

	@staticmethod
	def _pagination(op: str, spec: dict) -> dict:
		if not isinstance(spec, dict):
			raise ConfigError(f"{op}.pagination must be an object.")
		kind = spec.get("type") or "none"
		if kind not in PAGINATION_TYPES:
			raise ConfigError(f"{op}.pagination.type must be one of {', '.join(PAGINATION_TYPES)}.")
		need = {"none": (), "page": ("page_param",), "offset": ("offset_param", "limit_param"),
			"cursor": ("cursor_param", "cursor_path"), "next_link": ("next_path",)}[kind]
		for key in need:
			if not str(spec.get(key) or "").strip():
				raise ConfigError(f"{op}.pagination.{key} is required for {kind}.")
		result = {k: v for k, v in spec.items() if v not in (None, "")}
		result["type"] = kind
		for key in ("size", "limit"):
			if key in result:
				result[key] = int(_int(result[key], f"{op}.pagination.{key}", 1, 10000, 100))
		if "start" in result:
			result["start"] = int(_int(result["start"], f"{op}.pagination.start", 0, 1, 0))
		for key in ("last_path", "total_pages_path", "total_path", "cursor_path", "next_path"):
			path_tokens(str(result.get(key) or ""))
		return result

	# Reading ----------------------------------------------------------------------------------------------------
	@property
	def base_url(self) -> str:
		return self.data.get("base_url") or ""

	@property
	def system(self) -> str:
		return self.data.get("system") or ""

	def endpoint(self, op: str) -> dict | None:
		return self.data["endpoints"].get(op)

	def has(self, op: str) -> bool:
		return op in self.data["endpoints"]

	def secret_names(self) -> list[str]:
		"""Secrets the configuration references (the credentials of this connector are dynamic, ADR §3)."""
		names = {p[7:] for p in placeholders_in([self.data["auth"], self.data["headers"]]) if p.startswith("secret:")}
		return sorted(names)

	def register_fields(self) -> list[dict]:
		spec = self.endpoint("register_cedente")
		return spec["fields"] if spec else []

	def fingerprint(self) -> str:
		return hashlib.sha256(json.dumps(self.data, sort_keys=True).encode()).hexdigest()[:16]

	def as_json(self) -> str:
		return json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=False)

	def template(self) -> dict:
		"""The configuration without the client's address (ADR §8). Secrets are never in it (only their names)."""
		data = json.loads(json.dumps(self.data))
		for key in TEMPLATE_DROPPED:
			data.pop(key, None)
		data["template"] = True
		return data

	@classmethod
	def from_template(cls, template, base_url: str) -> "RestConfig":
		if isinstance(template, str):
			try:
				template = json.loads(template)
			except ValueError as e:
				raise ConfigError(f"The template is not valid JSON: {e}") from None
		if not isinstance(template, dict):
			raise ConfigError("The template must be a JSON object.")
		data = {k: v for k, v in template.items() if k not in ("template", *TEMPLATE_DROPPED)}
		data["base_url"] = base_url
		return cls.parse(data, require_url=bool(base_url))


# HTTP client ----------------------------------------------------------------------------------------------------

STATIC = "static"


class RestClient(BaseClient):
	"""HTTP client driven by a RestConfig. `secrets` = {name: value}, used only to build headers and redact."""

	def __init__(self, config: RestConfig, secrets: dict | None = None, session=None, **kwargs):
		self.config = config
		self.secrets = {k: v for k, v in (secrets or {}).items() if v}
		limits = config.data["limits"]
		session = session or safe_api.SafeApiSession(max_bytes=limits["max_bytes"], max_redirects=limits["max_redirects"])
		super().__init__(config.base_url, session=session, **kwargs)
		self.min_interval = float(config.data["pace"]["min_interval"])
		self.timeout = limits["timeout"]
		self._host = urlsplit(config.base_url).hostname

	# Security helpers -------------------------------------------------------------------------------------------
	def redact(self, text) -> str:
		"""Secrets out of any text that may reach a log or the admin; URLs lose their query string."""
		text = str(text or "")
		for value in sorted(self.secrets.values(), key=len, reverse=True):
			if len(value) >= 4:
				text = text.replace(value, "***")
		return re.sub(r"(https?://[^\s?#\"']+)\?[^\s\"']*", r"\1?***", text)

	def _url(self, target: str) -> str:
		url = target if target.startswith("https://") else f"{self.base_url}{target}"
		host = urlsplit(url).hostname
		if host != self._host and url != self.config.data["auth"].get("token_url"):
			raise ErpError("The API sent us to another address; the connector only calls the configured API.", "blocked")
		return url

	def _send(self, method: str, target: str, **kwargs):
		self._throttle()
		self.calls += 1
		headers = kwargs.pop("headers", None) or self._headers()
		try:
			return self.session.request(method, self._url(target), headers=headers, timeout=self.timeout, **kwargs)
		except safe_api.BlockedAddress as e:
			raise ErpError(f"The API address was blocked for security: {self.redact(e)}", "blocked") from None
		except safe_api.TooLarge:
			raise ErpError("The API answer is larger than the configured limit.", "too_large") from None
		except requests.RequestException as e:
			raise ErpError("Could not reach the API. Try again in a few minutes.", "network") from e

	# Auth -------------------------------------------------------------------------------------------------------
	def _auth_value(self, key: str) -> str:
		return fill(self.config.data["auth"].get(key) or "", {}, self.secrets)

	def _login(self) -> Token:
		auth = self.config.data["auth"]
		if auth["type"] != "oauth2":
			# Static credentials: nothing to renew; a 401 retries once and then is a credential error
			# (finite: the shared token cache stores the expiry as a TTL)
			return Token(STATIC, self.clock() + 24 * 3600)
		form = {"grant_type": "client_credentials"}
		if auth.get("scope"):
			form["scope"] = auth["scope"]
		headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
		client_id, client_secret = self._auth_value("client_id"), self._auth_value("client_secret")
		if auth["client_auth"] == "basic":
			pair = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
			headers["Authorization"] = f"Basic {pair}"
		else:
			form.update(client_id=client_id, client_secret=client_secret)
		response = self._send("POST", auth["token_url"], data=form, headers=headers)
		body = json_or_none(response) or {}
		if response.status_code >= 500:
			raise ErpMaintenance("The API login is unavailable.", response.status_code)
		if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
			raise ErpAuthError(
				self.redact(f"The API refused the login: {error_text(response) or response.status_code}"),
				response.status_code,
			)
		lifetime = int(body.get("expires_in") or 3600)
		return Token(str(body["access_token"]), self.clock() + max(lifetime - self.TOKEN_MARGIN, 30))

	def _headers(self) -> dict:
		headers = {"Accept": "application/json"}
		for name, value in self.config.data["headers"].items():
			headers[name] = fill(value, {}, self.secrets)
		auth = self.config.data["auth"]
		kind = auth["type"]
		if kind == "bearer":
			headers["Authorization"] = f"Bearer {self._auth_value('token')}"
		elif kind == "header":
			headers[auth["header"]] = self._auth_value("value")
		elif kind == "basic":
			pair = base64.b64encode(f"{self._auth_value('username')}:{self._auth_value('password')}".encode()).decode()
			headers["Authorization"] = f"Basic {pair}"
		elif kind == "oauth2":
			headers["Authorization"] = f"Bearer {self.token.value}"
		return headers

	def _check(self, response):
		status = response.status_code
		if 200 <= status < 300:
			return
		text = self.redact(error_text(response))
		if status == 429:
			try:
				wait = int(float((getattr(response, "headers", None) or {}).get("Retry-After") or 60))
			except ValueError:
				wait = 60
			raise ErpRateLimited(f"The API asked to wait before calling again. {text}".strip(), max(wait, 1))
		if status >= 500:
			raise ErpMaintenance(f"The API is unavailable ({status}). {text}".strip(), status)
		if status == 403:
			raise ErpAuthError(f"The API refused the access (403). {text}".strip(), status)
		raise ErpError(f"The API answered {status}: {text or '-'}", "error", status)

	# Calls ------------------------------------------------------------------------------------------------------
	def call(self, spec: dict, values: dict, extra_query: dict | None = None, body=None, url: str | None = None):
		"""One request of an endpoint; returns (status, parsed JSON or None)."""
		kwargs = {}
		if url is None:
			target = fill(spec["path"], values, quote_values=True)
			query = {k: fill(v, values) for k, v in spec["query"].items()}
			query.update(extra_query or {})
			if query:
				kwargs["params"] = query
		else:
			target = url
		if body is not None:
			kwargs["json"] = body
		response = self.request(spec["method"], target, **kwargs)
		parsed = json_or_none(response)
		if parsed is None and (getattr(response, "text", "") or "").strip():
			raise ErpError("The API answer is not JSON. Check the endpoint path.", "error", response.status_code)
		return response.status_code, parsed

	def pages(self, spec: dict, values: dict):
		"""Every item of a listing endpoint, page after page, with the configured pagination."""
		pagination = spec.get("pagination") or {"type": "none"}
		kind = pagination["type"]
		max_pages = self.config.data["limits"]["max_pages"]
		seen_pages, seen_marks = set(), set()
		page = int(pagination.get("start", 1 if kind == "page" else 0))
		offset = int(pagination.get("start", 0)) if kind == "offset" else 0
		size = int(pagination.get("size") or pagination.get("limit") or 100)
		cursor, next_url = None, None
		for number in range(max_pages + 1):
			if number == max_pages:
				raise ErpError(f"The listing passed {max_pages} pages; check the pagination settings.", "pagination")
			extra = {}
			if kind == "page":
				extra[pagination["page_param"]] = page
				if pagination.get("size_param"):
					extra[pagination["size_param"]] = size
			elif kind == "offset":
				extra[pagination["offset_param"]] = offset
				extra[pagination["limit_param"]] = size
			elif kind == "cursor" and cursor is not None:
				extra[pagination["cursor_param"]] = cursor
			_status, body = self.call(spec, values, extra, url=next_url)
			items = json_path(body, spec.get("items") or "")
			if items is None:
				items = []
			if not isinstance(items, list):
				raise ErpError(f"The list of items is not at {spec.get('items') or 'the root'} in the answer.", "error")
			fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
			if items and fingerprint in seen_pages:
				raise ErpError("The API returned the same page twice; check the pagination settings.", "pagination")
			seen_pages.add(fingerprint)
			yield from items
			if kind == "none" or not items:
				return
			if kind == "page":
				if pagination.get("last_path") and json_path(body, pagination["last_path"]) is True:
					return
				total = json_path(body, pagination.get("total_pages_path") or "") if pagination.get("total_pages_path") else None
				if isinstance(total, (int, float)) and page - int(pagination.get("start", 1)) + 1 >= total:
					return
				if pagination.get("size_param") and len(items) < size:
					return
				page += 1
			elif kind == "offset":
				offset += len(items)
				total = json_path(body, pagination.get("total_path") or "") if pagination.get("total_path") else None
				if len(items) < size or (isinstance(total, (int, float)) and offset >= total):
					return
			elif kind == "cursor":
				cursor = json_path(body, pagination["cursor_path"])
				if cursor in (None, "", False):
					return
				if cursor in seen_marks:
					raise ErpError("The API returned the same cursor twice; check the pagination settings.", "pagination")
				seen_marks.add(cursor)
			elif kind == "next_link":
				link = json_path(body, pagination["next_path"])
				if not link:
					return
				next_url = urljoin(f"{self.base_url}/", str(link))
				if next_url in seen_marks:
					raise ErpError("The API returned the same next link twice; check the pagination settings.", "pagination")
				seen_marks.add(next_url)

	def ping(self) -> dict:
		started = time.monotonic()
		status, _body = self.call(self.config.endpoint("ping"), {"today": format_date(date.today(), "iso")})
		return {"status": status, "endpoint": self.config.endpoint("ping")["path"], "ms": int((time.monotonic() - started) * 1000)}


# Common título shape --------------------------------------------------------------------------------------------


def outcome_of(situation, spec: dict) -> str:
	value = str(situation or "").strip().lower()
	for outcome in OUTCOMES:
		if value and value in spec["outcomes"].get(outcome, []):
			return outcome
	return spec["default_outcome"]


def to_title(item: dict, spec: dict, erp: str) -> dict:
	if not isinstance(item, dict):
		raise ErpError("An item of the títulos list is not an object; check items in the configuration.", "error")
	fields = spec["fields"]
	title = {"erp": erp}
	for key in TITLE_FIELDS:
		value = json_path(item, fields[key]) if key in fields else None
		if key in DATE_FIELDS:
			value = to_date(value)
		elif key in MONEY_FIELDS:
			value = to_decimal(value)
		elif key in DIGIT_FIELDS:
			value = digits(value)
		else:
			value = "" if value is None else str(value).strip()
		title[key] = value
	if not title["erp_id"]:
		raise ErpError(f"A título came without its id at {fields['erp_id']!r}; check fields.erp_id.", "error")
	title["outcome"] = outcome_of(title["situation"], spec)
	title["raw"] = flatten(item)
	return title


def registry_row(item: dict, spec: dict) -> dict:
	"""A sacado/cedente item for the de-para: flattened columns plus the document and name the sync reads."""
	row = flatten(item) if isinstance(item, dict) else {"value": item}
	row["CPF/CNPJ"] = digits(json_path(item, spec["document"]))
	if spec.get("name"):
		row["Nome"] = str(json_path(item, spec["name"]) or "").strip()
	return row
