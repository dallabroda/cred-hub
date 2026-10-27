"""Chaves da API do HubTech: criadas e geridas só pelo admin, sem ocupar vaga do plano (proposta credtech#175).

Cada chave tem um usuário técnico oculto (`credhub_api_key` preenchido):
- papéis Sales User e Pipeline Owner, para as rotas do CRM, mais o marcador HubTech API;
- é Dono só dos funis liberados na chave, então as permissões do CRM limitam os cards a esses funis;
- não conta no plano (billing.active_users) e fica fora das listas de usuários e da distribuição (crm).

Autenticação: `Authorization: Bearer hub_<key_id>_<segredo>`. O Frappe deixa passar um Bearer que não é token
OAuth e roda os auth_hooks, que é onde `authenticate` entra. Só o hash do segredo é gravado.

Cada chamada é conferida contra uma lista PERMITIDA de rotas por escopo: rota fora da lista recebe 403, então uma
rota nova do CRM fica fechada até ser liberada aqui. As escritas ficam no log (Cred Hub API Log).
"""

import hashlib
import hmac
import ipaddress
import secrets
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, nowdate

KEY = "Cred Hub API Key"
LOG = "Cred Hub API Log"
API_ROLE = "HubTech API"
TECHNICAL_ROLES = ("Sales User", "Pipeline Owner", API_ROLE)
PREFIX = "hub_"
MAX_KEYS = 10
LOG_DAYS = 90
# Rate limit por chave, por minuto (decisão do dono, credtech#175)
CALLS_PER_MINUTE = 120
WRITES_PER_MINUTE = 20

# Escopos: (campo da chave, rótulo). Leitura é GET; escrita é criar e alterar. Excluir não existe na 1ª versão.
SCOPES = {
	"cards_read": "Cards: read",
	"cards_write": "Cards: write",
	"contacts_read": "Contacts: read",
	"contacts_write": "Contacts: write",
	"organizations_read": "Organizations: read",
	"organizations_write": "Organizations: write",
	"leads_write": "Leads: create",
	"pipelines_read": "Pipelines: read",
	"fields_admin": "Fields and rules (admin)",
}

# /api/resource/<doctype> e /api/v2/document/<doctype>: escopo de leitura e de escrita
RESOURCES = {
	"CRM Deal": ("cards_read", "cards_write"),
	"Contact": ("contacts_read", "contacts_write"),
	"CRM Organization": ("organizations_read", "organizations_write"),
	"CRM Lead": (None, "leads_write"),
	"CRM Deal Status": ("pipelines_read", None),
	"CRM Pipeline": ("pipelines_read", None),
}

# /api/method/<rota>: escopo exigido. Escrita é decidida pela rota, não pelo verbo HTTP.
METHODS = {
	"crm.api.pipeline.get_pipelines": "pipelines_read",
	"crm.api.pipeline.get_pipeline_config": "pipelines_read",
	"crm.api.pipeline.get_stage_rules": "cards_read",
	"crm.api.pipeline.get_missing_fields": "cards_read",
	"crm.api.pipeline.get_field_catalog": "cards_read",
	"crm.api.pipeline.move_card": "cards_write",
	"crm.api.pipeline.change_pipeline": "cards_write",
	"crm.api.pipeline.save_pipeline": "fields_admin",
	"crm.api.pipeline.create_field": "fields_admin",
	"crm.api.pipeline.update_field": "fields_admin",
	"crm.api.pipeline.disable_field": "fields_admin",
	# Formulários dinâmicos (dev 3): o schema vai com a leitura de cada recurso; a configuração é de admin
	"crm.api.forms.get_form_schema": "cards_read",
	"crm.api.forms.create_field": "fields_admin",
	"crm.api.forms.update_field": "fields_admin",
	"crm.api.forms.disable_field": "fields_admin",
	"crm.api.forms.save_form_config": "fields_admin",
}
WRITE_METHODS = {name for name, scope in METHODS.items() if scope.endswith("_write") or scope == "fields_admin"}
# frappe.client.* só para leitura, com o doctype conferido contra RESOURCES
CLIENT_READS = ("frappe.client.get_list", "frappe.client.get", "frappe.client.get_count", "frappe.client.get_value")


class KeyNotAllowed(frappe.PermissionError):
	pass


# ---------- Segredo ----------


def _hash(secret: str) -> str:
	return hashlib.sha256(secret.encode()).hexdigest()


def _new_secret():
	key_id = secrets.token_hex(6)
	secret = secrets.token_urlsafe(32)
	return key_id, secret, f"{PREFIX}{key_id}_{secret}"


def _split_token(token: str):
	if not token.startswith(PREFIX):
		return None, None
	key_id, sep, secret = token[len(PREFIX) :].partition("_")
	return (key_id, secret) if sep and key_id and secret else (None, None)


# ---------- Autenticação (auth_hooks) ----------


def authenticate():
	header = frappe.get_request_header("Authorization", "") or ""
	scheme, _sep, token = header.partition(" ")
	if scheme.lower() != "bearer" or not token.startswith(PREFIX):
		return
	key_id, secret = _split_token(token.strip())
	key = frappe.db.get_value(
		KEY,
		{"key_id": key_id} if key_id else {"name": ""},
		["name", "secret_hash", "status", "expires_on", "technical_user", "allowed_ips"],
		as_dict=True,
	)
	if not key or not secret or not hmac.compare_digest(key.secret_hash or "", _hash(secret)):
		raise frappe.AuthenticationError
	if key.status != "Active" or (key.expires_on and getdate(key.expires_on) < getdate(nowdate())):
		raise frappe.AuthenticationError
	if not _ip_allowed(key.allowed_ips, frappe.local.request_ip):
		raise frappe.AuthenticationError
	write = _check_route(key.name)
	_rate_limit(key.name, write)
	frappe.set_user(key.technical_user)
	frappe.local.credhub_api_key = {"name": key.name, "write": write}


def _ip_allowed(allowed: str | None, ip: str | None) -> bool:
	lines = [line.strip() for line in (allowed or "").splitlines() if line.strip()]
	if not lines:
		return True
	try:
		address = ipaddress.ip_address(ip or "")
	except ValueError:
		return False
	for line in lines:
		try:
			if address in ipaddress.ip_network(line, strict=False):
				return True
		except ValueError:
			continue
	return False


def _scopes(key_name: str) -> set:
	values = frappe.db.get_value(KEY, key_name, list(SCOPES), as_dict=True) or {}
	return {scope for scope in SCOPES if cint(values.get(scope))}


def _deny():
	raise KeyNotAllowed(_("This API key is not allowed to do that."))


def _route():
	"""(kind, target, verbo): kind é resource, method, other (barrado) ou None fora da API."""
	path = unquote(frappe.request.path or "")
	verb = (frappe.request.method or "GET").upper()
	for prefix, kind in (
		("/api/resource/", "resource"),
		("/api/v2/document/", "resource"),
		("/api/method/", "method"),
		("/api/v2/method/", "method"),
	):
		if path.startswith(prefix):
			parts = path[len(prefix) :].strip("/").split("/")
			# <doctype> ou <doctype>/<name>; mais que isso (métodos do documento etc.) não é rota da chave
			if kind == "resource" and len(parts) > 2:
				return "other", None, verb
			return kind, parts[0], verb
	return None, None, verb


def _check_route(key_name: str) -> bool:
	"""Confere a rota contra os escopos da chave e devolve se é escrita. Fora da lista: 403."""
	kind, target, verb = _route()
	scopes = _scopes(key_name)
	if kind == "resource":
		read, write = RESOURCES.get(target, (None, None))
		if verb in ("GET", "HEAD"):
			if read in scopes:
				return False
		elif verb in ("POST", "PUT", "PATCH") and write in scopes:
			return True
		_deny()
	if kind == "method":
		if target in CLIENT_READS:
			doctype = frappe.local.form_dict.get("doctype")
			if RESOURCES.get(doctype, (None, None))[0] in scopes:
				return False
			_deny()
		scope = METHODS.get(target)
		if scope and scope in scopes:
			if target == "crm.api.pipeline.save_pipeline":
				_check_save_pipeline(key_name)
			return target in WRITE_METHODS
	_deny()


# save_pipeline pela chave: só campos e regras de um funil da chave, nunca criar funil, membros ou distribuição
SAVE_PIPELINE_KEYS = {"name", "fields", "field_rules"}


def _check_save_pipeline(key_name: str):
	data = frappe.local.form_dict.get("doc")
	try:
		data = frappe.parse_json(data) if isinstance(data, str) else data
	except Exception:
		_deny()
	if not isinstance(data, dict) or not data.get("name") or set(data) - SAVE_PIPELINE_KEYS:
		_deny()
	allowed = frappe.get_all("Cred Hub API Key Pipeline", {"parent": key_name, "parenttype": KEY}, pluck="pipeline")
	if data["name"] not in allowed:
		_deny()


def _rate_limit(key_name: str, write: bool):
	minute = now_datetime().strftime("%Y%m%d%H%M")
	limits = [(CALLS_PER_MINUTE, "all")] + ([(WRITES_PER_MINUTE, "write")] if write else [])
	for limit, counter in limits:
		cache_key = frappe.cache.make_key(f"credhub_api:{key_name}:{counter}:{minute}")
		count = frappe.cache.incrby(cache_key, 1)
		if count == 1:
			frappe.cache.expire(cache_key, 120)
		if count > limit:
			frappe.local.response_headers.set("Retry-After", "60")
			raise frappe.RateLimitExceededError(_("Too many requests for this API key. Wait a minute and try again."))


# ---------- Depois da chamada (after_request): último uso e log das escritas ----------


def after_request(response=None, request=None):
	info = getattr(frappe.local, "credhub_api_key", None)
	if not info:
		return
	try:
		frappe.db.set_value(KEY, info["name"], {"last_used_on": now_datetime(), "last_used_ip": frappe.local.request_ip}, update_modified=False)
		if info["write"]:
			kind, target, verb = _route()
			frappe.get_doc(
				{
					"doctype": LOG,
					"api_key": info["name"],
					"route": f"{verb} {frappe.request.path}"[:240],
					"reference_doctype": target if kind == "resource" else None,
					"status_code": getattr(response, "status_code", None),
					"ip_address": frappe.local.request_ip,
					"called_on": now_datetime(),
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Cred Hub API: falha ao registrar o uso da chave")


def delete_old_logs():
	frappe.db.delete(LOG, {"called_on": ("<", add_days(now_datetime(), -LOG_DAYS))})


# ---------- Usuário técnico ----------


def _technical_email(key_id: str) -> str:
	host = (frappe.local.site or "site").replace(":", "-")
	return f"api-{key_id}@{host}.invalid"


def _create_technical_user(key_id: str, label: str) -> str:
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": _technical_email(key_id),
			"first_name": _("API: {0}").format(label)[:140],
			"user_type": "System User",
			"send_welcome_email": 0,
			"credhub_api_key": key_id,
			"roles": [{"role": role} for role in TECHNICAL_ROLES],
		}
	)
	user.flags.ignore_permissions = True
	user.flags.no_welcome_mail = True
	user.insert()
	return user.name


def _sync_pipelines(user: str, pipelines: list[str]):
	"""O usuário técnico é Dono dos funis da chave e de nenhum outro."""
	wanted = set(pipelines)
	current = set(frappe.get_all("CRM Pipeline Member", {"parenttype": "CRM Pipeline", "user": user}, pluck="parent"))
	for pipeline in current | wanted:
		doc = frappe.get_doc("CRM Pipeline", pipeline)
		rows = [m for m in doc.members if m.user == user]
		if pipeline in wanted and not rows:
			doc.append("members", {"user": user, "role": "Owner"})
		elif pipeline in wanted:
			for row in rows:
				row.role = "Owner"
		else:
			doc.members = [m for m in doc.members if m.user != user]
		doc.flags.ignore_permissions = True
		doc.save()


def _disable_technical_user(user: str):
	if not user or not frappe.db.exists("User", user):
		return
	_sync_pipelines(user, [])
	frappe.db.set_value("User", user, "enabled", 0)


# ---------- Telas (só admin) ----------


def _only_admin():
	"""Só o administrador da empresa; uma chave nunca gere chaves (o usuário técnico não é System Manager)."""
	if getattr(frappe.local, "credhub_api_key", None):
		_deny()
	frappe.only_for("System Manager")


def _clean_pipelines(pipelines) -> list[str]:
	pipelines = frappe.parse_json(pipelines) if isinstance(pipelines, str) else (pipelines or [])
	unknown = [p for p in pipelines if not frappe.db.exists("CRM Pipeline", p)]
	if unknown:
		frappe.throw(_("Pipeline {0} does not exist.").format(frappe.bold(unknown[0])))
	return list(dict.fromkeys(pipelines))


def _clean_scopes(scopes) -> dict:
	scopes = frappe.parse_json(scopes) if isinstance(scopes, str) else (scopes or [])
	unknown = [s for s in scopes if s not in SCOPES]
	if unknown:
		frappe.throw(_("Unknown scope: {0}").format(frappe.bold(unknown[0])))
	if not scopes:
		frappe.throw(_("Choose at least one permission for the key."))
	return {scope: int(scope in scopes) for scope in SCOPES}


def _check_card_pipelines(values: dict, pipelines: list[str]):
	if (values["cards_read"] or values["cards_write"] or values["fields_admin"]) and not pipelines:
		frappe.throw(_("Choose at least one pipeline for the cards."))


def _key_info(doc) -> dict:
	return {
		"name": doc.name,
		"label": doc.label,
		"prefix": f"{PREFIX}{doc.key_id}",
		"secret_hint": doc.secret_hint,
		"status": doc.status,
		"scopes": [scope for scope in SCOPES if cint(doc.get(scope))],
		"pipelines": [row.pipeline for row in doc.pipelines],
		"allowed_ips": doc.allowed_ips,
		"expires_on": doc.expires_on,
		"created_by": doc.owner,
		"created_on": doc.creation,
		"last_used_on": doc.last_used_on,
		"last_used_ip": doc.last_used_ip,
	}


@frappe.whitelist()
def get_api_keys() -> dict:
	_only_admin()
	names = frappe.get_all(KEY, pluck="name", order_by="creation desc")
	return {
		"keys": [_key_info(frappe.get_doc(KEY, name)) for name in names],
		"scopes": [{"value": k, "label": _(v)} for k, v in SCOPES.items()],
		"limit": MAX_KEYS,
	}


@frappe.whitelist(methods=["POST"])
def create_api_key(label: str, scopes, pipelines=None, allowed_ips: str | None = None, expires_on: str | None = None) -> dict:
	"""Cria a chave e devolve o token completo UMA vez (só o hash fica gravado)."""
	_only_admin()
	label = (label or "").strip()
	if not label:
		frappe.throw(_("Give the key a name."))
	if frappe.db.count(KEY, {"status": "Active"}) >= MAX_KEYS:
		frappe.throw(_("This company already has {0} active API keys. Revoke one to create another.").format(MAX_KEYS))
	values = _clean_scopes(scopes)
	pipelines = _clean_pipelines(pipelines)
	_check_card_pipelines(values, pipelines)
	_check_ips(allowed_ips)
	key_id, secret, token = _new_secret()
	user = _create_technical_user(key_id, label)
	doc = frappe.get_doc(
		{
			"doctype": KEY,
			"label": label,
			"key_id": key_id,
			"secret_hash": _hash(secret),
			"secret_hint": secret[-4:],
			"status": "Active",
			"technical_user": user,
			"allowed_ips": allowed_ips,
			"expires_on": expires_on or None,
			"pipelines": [{"pipeline": p} for p in pipelines],
			**values,
		}
	).insert(ignore_permissions=True)
	_sync_pipelines(user, pipelines)
	return {**_key_info(doc), "token": token}


@frappe.whitelist(methods=["POST"])
def update_api_key(name: str, label=None, scopes=None, pipelines=None, allowed_ips=None, expires_on=None) -> dict:
	"""Renomeia e muda escopos, funis, IPs e validade: vale na próxima chamada."""
	_only_admin()
	doc = frappe.get_doc(KEY, name)
	if doc.status != "Active":
		frappe.throw(_("This key was revoked."))
	if label is not None:
		if not label.strip():
			frappe.throw(_("Give the key a name."))
		doc.label = label.strip()
	if scopes is not None:
		doc.update(_clean_scopes(scopes))
	if pipelines is not None:
		doc.set("pipelines", [{"pipeline": p} for p in _clean_pipelines(pipelines)])
	if allowed_ips is not None:
		_check_ips(allowed_ips)
		doc.allowed_ips = allowed_ips
	if expires_on is not None:
		doc.expires_on = expires_on or None
	_check_card_pipelines({s: cint(doc.get(s)) for s in SCOPES}, [r.pipeline for r in doc.pipelines])
	doc.save(ignore_permissions=True)
	_sync_pipelines(doc.technical_user, [r.pipeline for r in doc.pipelines])
	if label is not None:
		frappe.db.set_value("User", doc.technical_user, "first_name", _("API: {0}").format(doc.label)[:140])
	return _key_info(doc)


@frappe.whitelist(methods=["POST"])
def rotate_api_key(name: str) -> dict:
	"""Segredo novo; o antigo para de valer na hora. Devolve o token novo UMA vez."""
	_only_admin()
	doc = frappe.get_doc(KEY, name)
	if doc.status != "Active":
		frappe.throw(_("This key was revoked."))
	_key_id, secret, _token = _new_secret()
	doc.secret_hash = _hash(secret)
	doc.secret_hint = secret[-4:]
	doc.save(ignore_permissions=True)
	return {**_key_info(doc), "token": f"{PREFIX}{doc.key_id}_{secret}"}


@frappe.whitelist(methods=["POST"])
def revoke_api_key(name: str) -> dict:
	"""A chave para na hora e o usuário técnico é desativado; o histórico fica."""
	_only_admin()
	doc = frappe.get_doc(KEY, name)
	doc.status = "Revoked"
	doc.save(ignore_permissions=True)
	_disable_technical_user(doc.technical_user)
	return _key_info(doc)


def _check_ips(allowed_ips: str | None):
	for line in (allowed_ips or "").splitlines():
		line = line.strip()
		if not line:
			continue
		try:
			ipaddress.ip_network(line, strict=False)
		except ValueError:
			frappe.throw(_("{0} is not a valid IP address or range.").format(frappe.bold(line)))


def is_api_user(user: str | None) -> bool:
	# Antes do migrate o campo ainda não existe: ninguém é usuário técnico
	if not user or not frappe.get_meta("User").has_field("credhub_api_key"):
		return False
	return bool(frappe.db.get_value("User", user, "credhub_api_key"))


def block_login(login_manager=None):
	"""on_session_creation: o usuário técnico só existe para a chave; entrar pela tela é negado."""
	if is_api_user(frappe.session.user):
		raise frappe.AuthenticationError(_("This user belongs to an API key and cannot sign in."))


def install():
	"""after_install/after_migrate: campo que marca o usuário técnico e o papel das chaves."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"User": [
				{
					"fieldname": "credhub_api_key",
					"fieldtype": "Data",
					"label": "API Key",
					"insert_after": "credhub_paused",
					"read_only": 1,
					"hidden": 1,
					"search_index": 1,
				}
			]
		},
		update=True,
	)
	if not frappe.db.exists("Role", API_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": API_ROLE, "desk_access": 0}).insert(ignore_permissions=True)
