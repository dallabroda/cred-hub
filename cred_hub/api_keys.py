"""Chaves da API do HubTech: criadas e geridas só pelo admin, sem ocupar vaga do plano (proposta credtech#175).

Cada chave tem um usuário técnico oculto (`credhub_api_key` preenchido):
- papéis Sales User e Pipeline Owner, para as rotas do CRM, mais o marcador HubTech API;
- é Dono só dos funis liberados na chave, então as permissões do CRM limitam os cards a esses funis;
- não conta no plano (billing.active_users) e fica fora das listas de usuários e da distribuição (crm).

Autenticação: `Authorization: Bearer hub_<key_id>_<segredo>`. O Frappe deixa passar um Bearer que não é token
OAuth e roda os auth_hooks, que é onde `authenticate` entra. Só o hash do segredo é gravado.

Cada chamada é conferida contra uma lista PERMITIDA de rotas por escopo: rota fora da lista recebe 403, então uma
rota nova do CRM fica fechada até ser liberada aqui. As chamadas ficam auditadas em `Cred Hub API Log` com `request_id`
e `duration_ms`, e toda resposta inclui headers `X-Request-Id` e `X-RateLimit-*`.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from datetime import timedelta
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, nowdate

from cred_hub import api_guard
from cred_hub.cnpj.mapping import format_cnpj, only_digits


def _format_cpf(digits: str) -> str:
	return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}" if len(digits) == 11 else digits

KEY = "Cred Hub API Key"
LOG = "Cred Hub API Log"
API_ROLE = "HubTech API"
TECHNICAL_ROLES = ("Sales User", "Pipeline Owner", API_ROLE)
PREFIX = "hub_"
MAX_KEYS = 10
LOG_DAYS = 90
# Rate limit por chave, por minuto (decisão do dono, credtech#175, expandido credtech#288)
CALLS_PER_MINUTE = 300
WRITES_PER_MINUTE = 60
# Teto do limite próprio de uma chave: o servidor é compartilhado entre as empresas (credtech#288)
MAX_CUSTOM_RATE_LIMIT = 1000

# Escopos: (campo da chave, rótulo). Leitura é GET; escrita é criar e alterar. Excluir exige allow_delete.
SCOPES = {
	"cards_read": "Cards: read",
	"cards_write": "Cards: write",
	"contacts_read": "Contacts: read",
	"contacts_write": "Contacts: write",
	"organizations_read": "Organizations: read",
	"organizations_write": "Organizations: write",
	"leads_write": "Incoming contacts: create",
	"pipelines_read": "Pipelines: read",
	"fields_admin": "Fields and rules (admin)",
	"allow_delete": "Records: allow DELETE (Cards, Contacts, Organizations, Files, Notes, Tasks)",
	"files_read": "Files and Documents: read and download",
	"files_write": "Files and Documents: upload",
	"notes_read": "Notes: read",
	"notes_write": "Notes: create and edit",
	"comments_read": "Timeline comments: read",
	"comments_write": "Timeline comments: add",
	"tasks_read": "Tasks: read",
	"tasks_write": "Tasks: create and edit",
	"sync_deletes": "Incremental sync: read deleted records log",
	"credit_read": "Credit & Digital Folder: read",
	"credit_write": "Credit & Digital Folder: write",
	# Consultas pagas a bureaus (a empresa paga ao provedor): só com este escopo, que vem desligado (decisão do dono:
	# o cliente decide os custos)
	"credit_paid_queries": "Credit: run paid bureau queries",
}

# /api/resource/<doctype> e /api/v2/document/<doctype>: escopo de leitura e de escrita
RESOURCES = {
	"CRM Deal": ("cards_read", "cards_write"),
	"Contact": ("contacts_read", "contacts_write"),
	"CRM Organization": ("organizations_read", "organizations_write"),
	"CRM Lead": (None, "leads_write"),
	"CRM Deal Status": ("pipelines_read", None),
	"CRM Pipeline": ("pipelines_read", None),
	"File": ("files_read", "files_write"),
	"FCRM Note": ("notes_read", "notes_write"),
	"CRM Task": ("tasks_read", "tasks_write"),
	"Cred Hub Checklist Item": ("credit_read", "credit_write"),
	"Cred Hub Visit Report": ("credit_read", "credit_write"),
	"Cred Hub Credit Report": ("credit_read", None),
}

# Doctypes protegidos que nunca podem ser excluídos via API Key mesmo com allow_delete.
PROTECTED_AGAINST_DELETE = {
	"CRM Pipeline",
	"CRM Deal Status",
	"Cred Hub API Key",
	"User",
	"CRM Lead",
	"Cred Hub Credit Report",
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
	# Campos e regras por etapa dos funis da chave (o usuário técnico é Dono deles).
	"crm.api.pipeline.save_pipeline": "fields_admin",
	# Formulários dinâmicos (dev 3): o schema vai com a leitura de cada recurso
	"crm.api.forms.get_form_schema": "cards_read",
	# Campos da organização, segunda organização e contato que a etapa mostra no card (Fase 5, credtech#182)
	"crm.api.forms.get_card_blocks": "cards_read",
	# Vínculos de contatos ao card
	"crm.api.card_contacts.get_card_contacts": "cards_read",
	# Upload de arquivos
	"upload_file": "files_write",
	# Comentários / Timeline
	"frappe.desk.form.utils.add_comment": "comments_write",
	"cred_hub.timeline.get_comments": "comments_read",
	# Sincronização incremental com exclusões
	"cred_hub.sync.get_deleted_records": "sync_deletes",
	# Operações em lote (Batch API)
	"cred_hub.bulk.bulk_upsert": "cards_write",
	"cred_hub.bulk.bulk_delete": "cards_write",
	# Busca unificada por CPF/CNPJ e especificação OpenAPI 3.1
	"cred_hub.api_keys.lookup_by_document": "organizations_read",
	"cred_hub.api_keys.get_openapi_spec": "pipelines_read",
	# Domínio de Crédito PJ & Pasta Digital
	"cred_hub.credit_check.api.get_credit_onboarding": "credit_read",
	"cred_hub.credit_check.api.get_credit_committee": "credit_read",
	"cred_hub.credit_check.api.get_report_details": "credit_read",
	"cred_hub.credit_check.api.get_whatsapp_onboarding_reminder": "credit_read",
	"cred_hub.credit_check.api.create_onboarding_portal_link": "credit_write",
	"cred_hub.credit_check.api.generate_portal_link": "credit_write",
	"cred_hub.credit_check.api.revoke_portal_link": "credit_write",
	"cred_hub.credit_check.api.update_credit_checklist_item": "credit_write",
	"cred_hub.credit_check.api.update_checklist_item": "credit_write",
	"cred_hub.credit_check.api.analyze_checklist_document": "credit_write",
	"cred_hub.credit_check.api.save_credit_visit_report": "credit_write",
	"cred_hub.credit_check.api.save_visit_report": "credit_write",
	"cred_hub.credit_check.api.save_committee_proposal": "credit_write",
	"cred_hub.credit_check.api.cast_committee_vote": "credit_write",
	"cred_hub.credit_check.api.check_deal": "credit_paid_queries",
	"cred_hub.credit_check.api.trigger_check": "credit_paid_queries",
}
# Rotas que gastam dinheiro do cliente: só POST (um GET de navegador ou de cache nunca dispara uma consulta paga)
POST_ONLY_METHODS = {name for name, scope in METHODS.items() if scope == "credit_paid_queries"}
WRITE_METHODS = {
	name
	for name, scope in METHODS.items()
	if scope.endswith("_write") or scope in ("fields_admin", "credit_paid_queries")
}
# frappe.client.* e list_records_cursor só para leitura, com o doctype conferido contra RESOURCES
CLIENT_READS = (
	"frappe.client.get_list",
	"frappe.client.get",
	"frappe.client.get_count",
	"frappe.client.get_value",
	"cred_hub.api_keys.list_records_cursor",
)


class KeyNotAllowed(frappe.PermissionError):
	pass


# ---------- Segredo ----------


def _hash(secret: str) -> str:
	site_secret = frappe.conf.get("secret_key") or getattr(frappe.local, "site", "") or "credhub"
	return hmac.new(site_secret.encode(), secret.encode(), hashlib.sha256).hexdigest()


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
	if scheme.lower() in ("token", "basic"):
		_check_legacy_token()
		return
	if scheme.lower() != "bearer" or not token.startswith(PREFIX):
		return
	start_time = time.time()
	request_id = f"req_{secrets.token_hex(8)}"
	# A key never gets a server traceback, whatever the site's allow_error_traceback says (audit M14)
	frappe.local.flags.disable_traceback = True
	_set_header("X-Request-Id", request_id)
	# An IP that keeps failing authentication is stopped before any lookup or hashing
	_check_auth_failures()
	key_id, secret = _split_token(token.strip())
	key = frappe.db.get_value(
		KEY,
		{"key_id": key_id} if key_id else {"name": ""},
		["name", "secret_hash", "status", "expires_on", "technical_user", "allowed_ips", "custom_rate_limit"],
		as_dict=True,
	)
	if not key or not secret:
		_refuse_auth(None)
	expected_hash = _hash(secret)
	legacy_hash = hashlib.sha256(secret.encode()).hexdigest()
	stored_hash = key.secret_hash or ""
	if not (hmac.compare_digest(stored_hash, expected_hash) or hmac.compare_digest(stored_hash, legacy_hash)):
		_refuse_auth(key.name)
	if stored_hash == legacy_hash and stored_hash != expected_hash:
		frappe.db.set_value(KEY, key.name, "secret_hash", expected_hash, update_modified=False)
	if key.status != "Active" or (key.expires_on and getdate(key.expires_on) < getdate(nowdate())):
		_refuse_auth(key.name)
	if not _ip_allowed(key.allowed_ips, frappe.local.request_ip):
		_refuse_auth(key.name)
	try:
		write = _check_route(key.name)
	except KeyNotAllowed:
		_refuse(key, request_id, start_time, 403, KeyNotAllowed)
	rate_info = _rate_limit(key.name, write, key.custom_rate_limit)
	# set_user zera o form_dict (os argumentos da chamada); o OAuth do Frappe faz o mesmo cuidado
	form_dict = frappe.local.form_dict
	frappe.set_user(key.technical_user)
	frappe.local.form_dict = form_dict
	frappe.local.credhub_api_key = {
		"name": key.name,
		"write": write,
		"request_id": request_id,
		"start_time": start_time,
		"rate_limit": rate_info,
	}
	if _route()[1] == "upload_file":
		# with the key's user set: the record the file goes to must be writable by this key (its pipelines)
		api_guard.check_upload_record()
	try:
		if hasattr(frappe.local, "response_headers"):
			frappe.local.response_headers.set("X-Request-Id", request_id)
			frappe.local.response_headers.set("X-RateLimit-Limit", str(rate_info["limit"]))
			frappe.local.response_headers.set("X-RateLimit-Remaining", str(rate_info["remaining"]))
			frappe.local.response_headers.set("X-RateLimit-Reset", str(rate_info["reset"]))
	except Exception:
		pass


def _set_header(name: str, value: str):
	try:
		if hasattr(frappe.local, "response_headers"):
			frappe.local.response_headers.set(name, value)
	except Exception:
		pass


# Falhas de autenticação por IP e por minuto antes do 429 (quem erra a chave não gasta o limite da integração real)
AUTH_FAILURES_PER_MINUTE = 30


def _auth_failures_key() -> str:
	minute = now_datetime().strftime("%Y%m%d%H%M")
	return frappe.cache.make_key(f"credhub_api_authfail:{frappe.local.request_ip}:{minute}")


def _check_auth_failures():
	if cint(frappe.cache.get(_auth_failures_key())) >= AUTH_FAILURES_PER_MINUTE:
		_set_header("Retry-After", str(max(1, 60 - now_datetime().second)))
		raise frappe.RateLimitExceededError(_("Too many failed authentications from this address. Wait a minute."))


def _refuse_auth(key_name: str | None):
	"""401: counted per IP (never in the key's own limit: knowing a key id must not lock the real integration out),
	and logged at most once per key and IP a minute (review of #103)."""
	try:
		failures_key = _auth_failures_key()
		if frappe.cache.incrby(failures_key, 1) == 1:
			frappe.cache.expire(failures_key, 120)
		once = frappe.cache.make_key(f"credhub_api_authlog:{key_name}:{frappe.local.request_ip}")
		if key_name and frappe.cache.set(once, 1, nx=True, ex=60):
			_log_refusal(key_name, 401)
	except Exception:
		frappe.log_error(title="Cred Hub API: falha ao registrar autenticação recusada")
	raise frappe.AuthenticationError


def _log_refusal(key_name: str, status: int, request_id: str | None = None, start_time: float | None = None):
	kind, target, verb = _route()
	frappe.get_doc(
		{
			"doctype": LOG,
			"api_key": key_name,
			"route": f"{verb} {frappe.request.path}"[:240],
			"reference_doctype": target if kind == "resource" else None,
			"status_code": status,
			"ip_address": frappe.local.request_ip,
			"called_on": now_datetime(),
			"request_id": request_id,
			"duration_ms": max(1, int((time.time() - (start_time or time.time())) * 1000)),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _refuse(key, request_id: str, start_time: float, status: int, exc):
	"""403 of a key whose secret was verified: logged and counted in its rate limit, so probing the scopes is visible
	and throttled (audit M13). The log is committed: the request fails."""
	try:
		_rate_limit(key.name, False, key.custom_rate_limit)
	finally:
		try:
			_log_refusal(key.name, status, request_id, start_time)
		except Exception:
			frappe.log_error(title="Cred Hub API: falha ao registrar chamada recusada")
	if exc is KeyNotAllowed:
		_deny()
	raise exc


# ---------- Chaves antigas (token api_key:api_secret de um usuário) ----------


def legacy_tokens_until():
	"""Último dia das chaves antigas: `credhub_legacy_token_until` (AAAA-MM-DD), posto no deploy da API nova com
	30 dias de transição (decisão do dono). Sem a config, as chaves antigas seguem como hoje."""
	value = frappe.conf.get("credhub_legacy_token_until")
	return getdate(value) if value else None


def _check_legacy_token():
	"""O Frappe já autenticou o token do usuário; depois do prazo, só o Administrator do site segue usando."""
	until = legacy_tokens_until()
	user = frappe.session.user
	if not until or getdate(nowdate()) <= until or user in ("Guest", "Administrator"):
		return
	raise frappe.AuthenticationError(
		_("Old API keys stopped working on {0}. Ask your admin for a key in Settings > Integrations > API.").format(
			frappe.utils.format_date(until)
		)
	)


def legacy_key_users() -> list[dict]:
	"""Usuários com chave antiga (gerada em /app/user), para o aviso na tela de API."""
	return frappe.get_all(
		"User",
		filters={"api_key": ("is", "set"), "name": ("not in", ("Administrator", "Guest")), "enabled": 1},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)


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
	meta = frappe.get_meta(KEY)
	available = [s for s in SCOPES if meta.has_field(s)]
	values = frappe.db.get_value(KEY, key_name, available, as_dict=True) or {}
	return {scope for scope in available if cint(values.get(scope))}


def _deny():
	raise KeyNotAllowed(_("This API key is not allowed to do that."))


def _resource_has_name() -> bool:
	path = unquote(frappe.request.path or "")
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if path.startswith(prefix):
			return len([p for p in path[len(prefix) :].split("/") if p]) >= 2
	return False


def _resource_name() -> str | None:
	path = unquote(frappe.request.path or "")
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if path.startswith(prefix):
			parts = [p for p in path[len(prefix) :].split("/") if p]
			return parts[1] if len(parts) >= 2 else None
	return None


def _route():
	"""(kind, target, verbo): kind é resource, method, file_download, other (barrado) ou None fora da API."""
	path = unquote(frappe.request.path or "")
	verb = (frappe.request.method or "GET").upper()
	if path.startswith("/private/files/") or path.startswith("/files/"):
		parts = path.strip("/").split("/")
		return "file_download", parts[-1] if len(parts) > 1 else "", verb
	for prefix, kind in (
		("/api/resource/", "resource"),
		("/api/v2/document/", "resource"),
		("/api/method/", "method"),
		("/api/v2/method/", "method"),
	):
		if path.startswith(prefix):
			parts = path[len(prefix) :].strip("/").split("/")
			if kind == "resource" and len(parts) > 2:
				return "other", None, verb
			return kind, parts[0], verb
	return None, None, verb


# Request arguments that make Frappe run ANOTHER whitelisted method than the one in the path: `cmd` is handled by
# frappe.app before the path (any route), `run_method` by GET /api/resource/<doctype>/<name> (v1 runs a document
# method), `method`/`library_file_name` by upload_file. A key must never reach them,
# or the scope allow-list below is bypassed (audit 11/11/2026, C1/H1).
DISPATCH_ARGS = ("cmd", "run_method")
UPLOAD_DISPATCH_ARGS = ("method", "library_file_name")


def _check_route(key_name: str) -> bool:
	"""Confere a rota contra os escopos da chave e devolve se é escrita. Fora da lista: 403."""
	kind, target, verb = _route()
	form = frappe.local.form_dict or {}
	if any(form.get(arg) not in (None, "") for arg in DISPATCH_ARGS):
		_deny()
	scopes = _scopes(key_name)
	if kind == "file_download":
		if "files_read" in scopes and verb in ("GET", "HEAD"):
			return False
		_deny()
	if kind == "resource":
		read, write = RESOURCES.get(target, (None, None))
		if verb in ("GET", "HEAD"):
			if read and read in scopes:
				return False
		elif verb in ("POST", "PUT", "PATCH") and write and write in scopes:
			# POST /api/resource/<doctype>/<name> runs a document method (Frappe v1): never through a key
			if verb == "POST" and _resource_has_name():
				_deny()
			# system-filled fields and child-row internals are refused (audit M6)
			api_guard.check_write(target, api_guard.request_payload(), _resource_name() if verb != "POST" else None)
			return True
		elif verb == "DELETE":
			if target not in PROTECTED_AGAINST_DELETE and write and write in scopes and "allow_delete" in scopes:
				return True
		_deny()
	if kind == "method":
		if target in CLIENT_READS:
			doctype = frappe.local.form_dict.get("doctype")
			read_scope = RESOURCES.get(doctype, (None, None))[0]
			if read_scope and read_scope in scopes:
				return False
			_deny()
		if target == "cred_hub.api_keys.lookup_by_document":
			if scopes & {"organizations_read", "contacts_read", "cards_read", "credit_read"}:
				return False
			_deny()
		if target in ("cred_hub.bulk.bulk_upsert", "cred_hub.bulk.bulk_delete"):
			doctype = frappe.local.form_dict.get("doctype")
			req_scope = {
				"CRM Deal": "cards_write",
				"Contact": "contacts_write",
				"CRM Organization": "organizations_write",
			}.get(doctype)
			if not req_scope or req_scope not in scopes:
				_deny()
			if target == "cred_hub.bulk.bulk_delete" and "allow_delete" not in scopes:
				_deny()
			return True
		if target == "upload_file":
			if any(form.get(arg) not in (None, "") for arg in UPLOAD_DISPATCH_ARGS):
				_deny()
			if "files_write" not in scopes:
				_deny()
			api_guard.check_upload(scopes, RESOURCES)
			# Files sent by an integration are always private (a public .html/.svg would be served on our domain)
			form["is_private"] = 1
		scope = METHODS.get(target)
		if target in POST_ONLY_METHODS and verb != "POST":
			_deny()
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
	except (TypeError, ValueError):
		_deny()
	if not isinstance(data, dict) or not data.get("name") or set(data) - SAVE_PIPELINE_KEYS:
		_deny()
	allowed = frappe.get_all("Cred Hub API Key Pipeline", {"parent": key_name, "parenttype": KEY}, pluck="pipeline")
	if data["name"] not in allowed:
		_deny()


def _custom_rate_limit(value) -> int | None:
	"""Vazio ou zero: o limite padrão. Acima do teto: recusado (não é cortado em silêncio)."""
	value = cint(value)
	if value <= 0:
		return None
	if value > MAX_CUSTOM_RATE_LIMIT:
		frappe.throw(_("The rate limit can be at most {0} calls per minute.").format(MAX_CUSTOM_RATE_LIMIT))
	return value


def _limits(custom_limit) -> tuple[int, int]:
	"""(calls, writes) per minute. Without a custom limit: 300 and 60. With one (capped at MAX_CUSTOM_RATE_LIMIT,
	the server is shared by every company): that many calls, and half of them may be writes (at least 60, never more
	than the total)."""
	custom = min(cint(custom_limit), MAX_CUSTOM_RATE_LIMIT)
	if custom <= 0:
		return CALLS_PER_MINUTE, WRITES_PER_MINUTE
	return custom, min(custom, max(WRITES_PER_MINUTE, custom // 2))


def _rate_limit(key_name: str, write: bool, custom_limit: int | None = None) -> dict:
	now = now_datetime()
	minute = now.strftime("%Y%m%d%H%M")
	prev_minute = (now - timedelta(minutes=1)).strftime("%Y%m%d%H%M")
	seconds_in_minute = now.second
	weight_prev = max(0.0, (60.0 - seconds_in_minute) / 60.0)
	reset_epoch = int(time.time()) + max(1, 60 - seconds_in_minute)

	if custom_limit is None:
		custom_limit = cint(frappe.db.get_value(KEY, key_name, "custom_rate_limit"))
	total_limit, write_limit = _limits(custom_limit)
	limits = [(total_limit, "all")] + ([(write_limit, "write")] if write else [])
	remaining = total_limit
	for limit, counter in limits:
		cache_key_curr = frappe.cache.make_key(f"credhub_api:{key_name}:{counter}:{minute}")
		cache_key_prev = frappe.cache.make_key(f"credhub_api:{key_name}:{counter}:{prev_minute}")
		curr_count = frappe.cache.incrby(cache_key_curr, 1)
		if curr_count == 1:
			frappe.cache.expire(cache_key_curr, 180)
		prev_count = cint(frappe.cache.get_value(cache_key_prev) or 0)
		estimated_rate = (prev_count * weight_prev) + curr_count
		if counter == "all":
			remaining = max(0, int(limit - estimated_rate))
		if estimated_rate > limit:
			if hasattr(frappe.local, "response_headers"):
				frappe.local.response_headers.set("Retry-After", str(max(1, 60 - seconds_in_minute)))
				frappe.local.response_headers.set("X-RateLimit-Limit", str(total_limit))
				frappe.local.response_headers.set("X-RateLimit-Remaining", "0")
				frappe.local.response_headers.set("X-RateLimit-Reset", str(reset_epoch))
			raise frappe.RateLimitExceededError(_("Too many requests for this API key. Wait a minute and try again."))
	return {"limit": total_limit, "remaining": remaining, "reset": reset_epoch}


# ---------- Depois da chamada (after_request): headers, último uso e log de auditoria ----------


def after_request(response=None, request=None):
	info = getattr(frappe.local, "credhub_api_key", None)
	if not info:
		return
	try:
		request_id = info.get("request_id") or f"req_{secrets.token_hex(8)}"
		start_time = info.get("start_time") or time.time()
		duration_ms = max(1, int((time.time() - start_time) * 1000))
		rate_info = info.get("rate_limit") or {}
		status_code = getattr(response, "status_code", None) or 200

		# Garante os headers também no objeto response final do Werkzeug
		if response is not None and hasattr(response, "headers"):
			response.headers["X-Request-Id"] = request_id
			if rate_info:
				response.headers["X-RateLimit-Limit"] = str(rate_info.get("limit", CALLS_PER_MINUTE))
				response.headers["X-RateLimit-Remaining"] = str(rate_info.get("remaining", 0))
				response.headers["X-RateLimit-Reset"] = str(rate_info.get("reset", 0))

		frappe.db.set_value(
			KEY,
			info["name"],
			{"last_used_on": now_datetime(), "last_used_ip": frappe.local.request_ip},
			update_modified=False,
		)
		# Registra chamadas na auditoria (escritas, erros 4xx/5xx e leituras de API)
		kind, target, verb = _route()
		log_meta = frappe.get_meta(LOG)
		log_payload = {
			"doctype": LOG,
			"api_key": info["name"],
			"route": f"{verb} {frappe.request.path}"[:240],
			"reference_doctype": target if kind == "resource" else None,
			"status_code": status_code,
			"ip_address": frappe.local.request_ip,
			"called_on": now_datetime(),
		}
		if log_meta.has_field("request_id"):
			log_payload["request_id"] = request_id
		if log_meta.has_field("duration_ms"):
			log_payload["duration_ms"] = duration_ms
		frappe.get_doc(log_payload).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Cred Hub API: falha ao registrar o uso da chave")


def delete_old_logs():
	frappe.db.delete(LOG, {"called_on": ("<", add_days(now_datetime(), -LOG_DAYS))})


# ---------- Endpoints Padrão Ouro (Cursor Pagination, Lookup CPF/CNPJ, OpenAPI 3.1, Logs UI) ----------


def _encode_cursor(modified_val: str, name_val: str) -> str:
	raw = json.dumps({"m": str(modified_val), "n": str(name_val)}, separators=(",", ":"))
	return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
	try:
		padded = cursor + "=" * (-len(cursor) % 4)
		data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
		return str(data["m"]), str(data["n"])
	except Exception:
		frappe.throw(_("Invalid pagination cursor."))


@frappe.whitelist()
def list_records_cursor(
	doctype: str,
	fields=None,
	filters=None,
	updated_since: str | None = None,
	limit: int = 50,
	cursor: str | None = None,
) -> dict:
	"""Listagem determinística por cursor (modified, name) para sincronização de ERPs sem perda nem duplicidade."""
	if doctype not in RESOURCES:
		frappe.throw(_("Unsupported resource doctype: {0}").format(doctype), frappe.PermissionError)

	limit = max(1, min(cint(limit or 50), 200))
	if isinstance(fields, str):
		try:
			fields = frappe.parse_json(fields)
		except Exception:
			fields = [f.strip() for f in fields.split(",") if f.strip()]
	if not isinstance(fields, list) or not fields:
		fields = ["name", "modified", "creation"]
	for required_field in ("name", "modified"):
		if required_field not in fields:
			fields.append(required_field)

	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	base_filters = list(filters) if isinstance(filters, list) else []
	if isinstance(filters, dict):
		for k, v in filters.items():
			base_filters.append([doctype, k, "=", v])

	if updated_since:
		base_filters.append([doctype, "modified", ">=", str(updated_since).strip()])

	rows = frappe.get_list(
		doctype,
		fields=fields,
		filters=base_filters,
		order_by="modified asc, name asc",
		limit_page_length=limit + 200 if cursor else limit + 1,
	)

	if cursor:
		cursor_mod, cursor_name = _decode_cursor(cursor)
		rows = [
			r
			for r in rows
			if (str(r.get("modified")), str(r.get("name"))) > (cursor_mod, cursor_name)
		]

	has_more = len(rows) > limit
	page = rows[:limit]
	next_cursor = (
		_encode_cursor(str(page[-1]["modified"]), str(page[-1]["name"]))
		if has_more and page
		else None
	)
	return {
		"data": page,
		"next_cursor": next_cursor,
		"has_more": has_more,
		"count": len(page),
	}


# Blocos da busca por CPF/CNPJ e o escopo que cada um pede quando quem chama é uma chave (audit M8)
LOOKUP_BLOCK_SCOPES = {
	"organization": "organizations_read",
	"contact": "contacts_read",
	"cards": "cards_read",
	"digital_folder_summary": "credit_read",
}
CREDIT_FIELDS = ("credit_verdict", "credit_report")


def _limit_lookup_to_scopes(result: dict) -> dict:
	"""A key only gets the blocks its scopes cover; credit verdicts only with credit_read. Cards already follow the
	key's pipelines (get_list as its technical user). People signed in keep the whole answer."""
	info = getattr(frappe.local, "credhub_api_key", None)
	if not info:
		return result
	scopes = _scopes(info["name"])
	for block, scope in LOOKUP_BLOCK_SCOPES.items():
		if scope not in scopes:
			result[block] = [] if block == "cards" else None
	if "credit_read" not in scopes:
		for row in [result.get("organization"), *(result.get("cards") or [])]:
			for field in CREDIT_FIELDS:
				if row:
					row.pop(field, None)
	return result


@frappe.whitelist()
def lookup_by_document(document: str) -> dict:
	"""Busca unificada por CNPJ ou CPF (com ou sem máscara) retornando Organização/Contato, Cards abertos, Pasta Digital e Crédito."""
	digits = only_digits(document or "")
	if len(digits) not in (11, 14):
		frappe.throw(_("Provide a valid CPF (11 digits) or CNPJ (14 digits)."))

	result: dict = {
		"document_digits": digits,
		"document_type": "CNPJ" if len(digits) == 14 else "CPF",
		"organization": None,
		"contact": None,
		"cards": [],
		"digital_folder_summary": None,
	}

	if len(digits) == 14:
		formatted = format_cnpj(digits)
		orgs = frappe.get_list(
			"CRM Organization",
			filters={"cnpj": ("in", [digits, formatted])},
			fields=[
				"name",
				"organization_name",
				"cnpj",
				"email",
				"website",
				"industry",
				"annual_revenue",
				"credit_verdict",
				"credit_report",
				"modified",
			],
			limit_page_length=1,
		)
		if orgs:
			org = orgs[0]
			result["organization"] = org
			result["cards"] = frappe.get_list(
				"CRM Deal",
				filters={"organization": org.name},
				fields=[
					"name",
					"organization",
					"organization_name",
					"pipeline",
					"status",
					"annual_revenue",
					"deal_owner",
					"credit_verdict",
					"credit_report",
					"modified",
				],
				order_by="modified desc",
				limit_page_length=25,
			)
			try:
				from cred_hub.credit_check.onboarding import get_onboarding

				ob = get_onboarding(org.name)
				result["digital_folder_summary"] = {
					**ob["summary"],
					"portal_active": ob["portal"].get("active", False),
					"portal_url": ob["portal"].get("url", ""),
					"visit_status": ob["visit"].get("status", "not_done"),
				}
			except Exception:
				pass
	else:
		formatted = _format_cpf(digits)
		contacts = frappe.get_list(
			"Contact",
			filters={"cpf": ("in", [digits, formatted])},
			fields=["name", "first_name", "full_name", "cpf", "email_id", "mobile_no", "phone", "company_name", "modified"],
			limit_page_length=1,
		)
		if contacts:
			contact = contacts[0]
			result["contact"] = contact
			if contact.get("company_name"):
				result["cards"] = frappe.get_list(
					"CRM Deal",
					filters={"organization": contact.company_name},
					fields=["name", "organization", "organization_name", "pipeline", "status", "annual_revenue", "deal_owner", "modified"],
					order_by="modified desc",
					limit_page_length=25,
				)

	return _limit_lookup_to_scopes(result)


@frappe.whitelist(allow_guest=True)
def get_openapi_spec() -> dict:
	"""Gera a especificação OpenAPI 3.1.0 viva da API do HubTech CRM."""
	base_url = frappe.utils.get_url()
	return {
		"openapi": "3.1.0",
		"info": {
			"title": "HubTech CRM — API Padrão Ouro",
			"version": "1.2.0",
			"description": (
				"API REST & RPC do HubTech CRM (CredTech Digital) para gestão de Funis, Cards, Organizações, "
				"Contatos, Pasta Digital de Crédito PJ, Pareceres de Risco e Sincronização Incremental com ERPs."
			),
		},
		"servers": [{"url": base_url, "description": "Servidor de Produção HubTech CRM"}],
		"components": {
			"securitySchemes": {
				"BearerAuth": {
					"type": "http",
					"scheme": "bearer",
					"bearerFormat": "hub_<key_id>_<secret>",
					"description": "Chave de API gerada em Configurações > Integrações > API.",
				}
			}
		},
		"security": [{"BearerAuth": []}],
		"paths": {
			"/api/method/crm.api.pipeline.get_pipelines": {
				"get": {
					"summary": "Listar Funis e Etapas",
					"tags": ["Funis & Catálogo"],
					"responses": {"200": {"description": "Lista de funis ativos autorizados na chave."}},
				}
			},
			"/api/method/crm.api.pipeline.get_field_catalog": {
				"get": {
					"summary": "Catálogo Dinâmico de Campos (Padrão + Customizados)",
					"tags": ["Funis & Catálogo"],
					"responses": {"200": {"description": "Catálogo de campos do Card, Organização e Contato."}},
				}
			},
			"/api/method/cred_hub.api_keys.lookup_by_document": {
				"get": {
					"summary": "Busca Unificada 360° por CNPJ ou CPF",
					"tags": ["Crédito PJ & Pasta Digital", "Cards & Organizações"],
					"parameters": [
						{
							"name": "document",
							"in": "query",
							"required": True,
							"schema": {"type": "string", "example": "12.345.678/0001-90"},
							"description": "CNPJ (14 dígitos) ou CPF (11 dígitos), com ou sem pontuação.",
						}
					],
					"responses": {
						"200": {
							"description": "Organização/Contato, Cards vinculados, resumo da Pasta Digital e veredito de crédito."
						}
					},
				}
			},
			"/api/method/cred_hub.api_keys.list_records_cursor": {
				"get": {
					"summary": "Listagem Paginada por Cursor + Delta (updated_since)",
					"tags": ["Sincronização ERP"],
					"parameters": [
						{"name": "doctype", "in": "query", "required": True, "schema": {"type": "string", "enum": ["CRM Deal", "CRM Organization", "Contact", "CRM Task", "FCRM Note"]}},
						{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
						{"name": "cursor", "in": "query", "schema": {"type": "string"}},
						{"name": "updated_since", "in": "query", "schema": {"type": "string", "example": "2026-11-01 00:00:00"}},
					],
					"responses": {"200": {"description": "Página de registros com next_cursor e has_more."}},
				}
			},
			"/api/resource/CRM Deal": {
				"get": {"summary": "Listar Cards", "tags": ["Cards & Organizações"], "responses": {"200": {"description": "OK"}}},
				"post": {"summary": "Criar Card", "tags": ["Cards & Organizações"], "responses": {"200": {"description": "Card criado"}}},
			},
			"/api/method/crm.api.pipeline.move_card": {
				"post": {
					"summary": "Mover Card de Etapa (respeita Stage Gate e campos obrigatórios)",
					"tags": ["Cards & Organizações"],
					"responses": {"200": {"description": "Card atualizado"}},
				}
			},
			"/api/method/cred_hub.bulk.bulk_upsert": {
				"post": {
					"summary": "Upsert em Lote (até 50 registros por chamada)",
					"tags": ["Sincronização ERP"],
					"responses": {
						"200": {"description": "Resumo de criados, atualizados e erros."},
						"409": {"description": "Banco ocupado (deadlock): nada foi gravado; repita a mesma chamada."},
					},
				}
			},
			"/api/method/cred_hub.bulk.bulk_delete": {
				"post": {
					"summary": "Exclusão em Lote (até 50 registros por chamada, requer allow_delete)",
					"tags": ["Sincronização ERP"],
					"responses": {
						"200": {"description": "Resumo de excluídos, não encontrados e erros."},
						"409": {"description": "Banco ocupado (deadlock): nada foi excluído; repita a mesma chamada."},
					},
				}
			},
			"/api/method/cred_hub.credit_check.api.get_credit_onboarding": {
				"get": {
					"summary": "Consultar Pasta Digital de Documentos e Relatório de Visita",
					"tags": ["Crédito PJ & Pasta Digital"],
					"parameters": [
						{"name": "organization", "in": "query", "required": True, "schema": {"type": "string"}},
						{"name": "deal", "in": "query", "schema": {"type": "string"}},
					],
					"responses": {"200": {"description": "Checklist completo, link externo blindado e visita."}},
				}
			},
			"/api/method/cred_hub.credit_check.api.create_onboarding_portal_link": {
				"post": {
					"summary": "Gerar / Obter Link da Caixa de Correio Blindada (Pasta Digital)",
					"tags": ["Crédito PJ & Pasta Digital"],
					"responses": {"200": {"description": "URL do portal blindado protegida por CNPJ."}},
				}
			},
		},
	}


@frappe.whitelist()
def get_api_logs(api_key: str | None = None, limit: int = 50) -> dict:
	"""Retorna os logs recentes de auditoria das chaves de API e métricas das últimas 24h para a tela /crm/settings/api."""
	_only_admin()
	limit = max(1, min(cint(limit or 50), 200))
	filters = {}
	if api_key:
		filters["api_key"] = api_key

	log_meta = frappe.get_meta(LOG)
	fields = ["name", "api_key", "called_on", "route", "reference_doctype", "status_code", "ip_address"]
	if log_meta.has_field("request_id"):
		fields.append("request_id")
	if log_meta.has_field("duration_ms"):
		fields.append("duration_ms")

	rows = frappe.get_all(
		LOG,
		filters=filters,
		fields=fields,
		order_by="called_on desc",
		limit_page_length=limit,
	)
	key_labels = dict(frappe.get_all(KEY, fields=["name", "label"], as_list=True))
	for r in rows:
		r["key_label"] = key_labels.get(r.get("api_key")) or r.get("api_key") or "—"
		r.setdefault("request_id", "—")
		r.setdefault("duration_ms", None)

	since_24h = add_days(now_datetime(), -1)
	total_24h = frappe.db.count(LOG, {"called_on": (">=", since_24h), **(filters or {})})
	errors_24h = frappe.db.count(
		LOG,
		{"called_on": (">=", since_24h), "status_code": (">=", 400), **(filters or {})},
	)
	return {
		"logs": rows,
		"metrics": {
			"total_24h": total_24h,
			"errors_24h": errors_24h,
		},
	}


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
	user.flags.credhub_api_key_flow = True
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
	meta = frappe.get_meta(KEY)
	return {scope: int(scope in scopes) for scope in SCOPES if meta.has_field(scope)}


def _check_card_pipelines(values: dict, pipelines: list[str]):
	if (values.get("cards_read") or values.get("cards_write") or values.get("fields_admin")) and not pipelines:
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
		"custom_rate_limit": doc.get("custom_rate_limit"),
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
		"legacy_until": legacy_tokens_until(),
		"legacy_users": legacy_key_users(),
	}


@frappe.whitelist(methods=["POST"])
def create_api_key(
	label: str,
	scopes,
	pipelines=None,
	allowed_ips: str | None = None,
	expires_on: str | None = None,
	custom_rate_limit: int | None = None,
) -> dict:
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
			"custom_rate_limit": _custom_rate_limit(custom_rate_limit),
			"expires_on": expires_on or None,
			"pipelines": [{"pipeline": p} for p in pipelines],
			**values,
		}
	).insert(ignore_permissions=True)
	_sync_pipelines(user, pipelines)
	return {**_key_info(doc), "token": token}


@frappe.whitelist(methods=["POST"])
def update_api_key(
	name: str,
	label=None,
	scopes=None,
	pipelines=None,
	allowed_ips=None,
	expires_on=None,
	custom_rate_limit=None,
) -> dict:
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
	if custom_rate_limit is not None:
		doc.custom_rate_limit = _custom_rate_limit(custom_rate_limit)
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


# Papéis que o Frappe põe sozinho em todo usuário de sistema
AUTOMATIC_ROLES = ("All", "Guest", "Desk User")


def validate_user(doc, method=None):
	"""doc_event User.validate: a marca de usuário técnico só muda pelo fluxo das chaves."""
	if not doc.meta.has_field("credhub_api_key"):
		return
	if (doc.is_new() or doc.has_value_changed("credhub_api_key")) and doc.credhub_api_key and not doc.flags.credhub_api_key_flow:
		frappe.throw(_("Only an API key can create its technical user."))
	if not doc.is_new() and doc.has_value_changed("credhub_api_key") and not doc.credhub_api_key:
		frappe.throw(_("The technical user of an API key cannot become a regular user. Revoke the key instead."))
	new_secret = bool(doc.api_secret) and set(doc.api_secret) != {"*"}
	if new_secret and doc.name != "Administrator" and (doc.credhub_api_key or legacy_tokens_until()):
		frappe.throw(_("Create API keys in Settings > Integrations > API."))
	if doc.credhub_api_key:
		extra = {row.role for row in doc.roles} - set(TECHNICAL_ROLES) - set(AUTOMATIC_ROLES)
		if extra:
			frappe.throw(_("The technical user of an API key cannot get other roles: {0}").format(", ".join(sorted(extra))))


def technical_users() -> list[str]:
	"""Hook crm_hidden_users: o crm tira estes usuários das listas, dos membros dos funis e da distribuição."""
	if not frappe.get_meta("User").has_field("credhub_api_key"):
		return []
	return frappe.get_all("User", {"credhub_api_key": ("is", "set")}, pluck="name")


def is_api_user(user: str | None) -> bool:
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
	try:
		from cred_hub.credit_check.onboarding import migrate_all_to_relational

		migrate_all_to_relational()
	except Exception:
		pass
