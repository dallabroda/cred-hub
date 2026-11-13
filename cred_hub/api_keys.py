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
from cred_hub.api_version import deprecation_headers, requested_version, set_version_headers
from cred_hub.billing import CREDIT_AND_COLLECTION, require_feature
from cred_hub.cnpj.mapping import format_cnpj, only_digits
from cred_hub.sandbox import (
	TEST_FORBIDDEN_SCOPES,
	TEST_HEADER,
	TEST_KEY_ID_PREFIX,
	check_not_sandbox,
	check_test_scopes,
	clear_test_users_cache,
	ensure_sandbox_pipeline,
	install_sandbox,
)


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
	# Catálogo de campos e formulários pela chave (decisão do dono 11/11/2026, credtech#375): só com "Campos e regras";
	# o crm confere o funil (Dono) e os limites, e o hook crm_field_admin deixa o usuário técnico passar como admin
	"crm.api.forms.create_field": "fields_admin",
	"crm.api.forms.update_field": "fields_admin",
	"crm.api.forms.disable_field": "fields_admin",
	"crm.api.forms.save_form_config": "fields_admin",
	"crm.api.pipeline.create_field": "fields_admin",
	"crm.api.pipeline.update_field": "fields_admin",
	"crm.api.pipeline.disable_field": "fields_admin",
	# Campos da organização, segunda organização e contato que a etapa mostra no card (Fase 5, credtech#182)
	"crm.api.forms.get_card_blocks": "cards_read",
	# Vínculos de contatos ao card
	"crm.api.card_contacts.get_card_contacts": "cards_read",
	# Upload de arquivos
	"upload_file": "files_write",
	# Comentários / Timeline
	"frappe.desk.form.utils.add_comment": "comments_write",
	"cred_hub.timeline.add_comment": "comments_write",
	"cred_hub.timeline.get_comments": "comments_read",
	"cred_hub.health.check": "pipelines_read",
	# Sincronização incremental com exclusões
	"cred_hub.sync.get_deleted_records": "sync_deletes",
	# Operações em lote e Upsert Inteligente (Batch & Single Upsert API)
	"cred_hub.bulk.bulk_upsert": "cards_write",
	"cred_hub.bulk.bulk_delete": "cards_write",
	"cred_hub.api_keys.upsert_record": "cards_write",
	# Porta B de ERPs (ADR 0007 §1, §4, §5)
	"cred_hub.erp.push.upsert_titles": "cards_write",
	"cred_hub.erp.push.settle_titles": "cards_write",
	"cred_hub.erp.push.register_result": "cards_write",
	# Busca unificada por CPF/CNPJ e especificação OpenAPI 3.1
	"cred_hub.api_keys.lookup_by_document": "organizations_read",
	"cred_hub.api_keys.get_openapi_spec": "pipelines_read",
	"cred_hub.api_keys.get_postman_collection": "pipelines_read",
	# Domínio de Crédito PJ & Pasta Digital
	"cred_hub.credit_check.api.get_credit_onboarding": "credit_read",
	# Pasta Digital pela API (credtech#375): ler o checklist item a item e enviar um documento a um item
	"cred_hub.credit_check.api.get_document_checklist": "credit_read",
	"cred_hub.credit_check.api.upload_checklist_document": "files_write",
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


def _new_secret(prefix: str = ""):
	key_id = prefix + secrets.token_hex(6)
	secret = secrets.token_urlsafe(32)
	return key_id, secret, f"{PREFIX}{key_id}_{secret}"


def _split_token(token: str):
	if not token.startswith(PREFIX):
		return None, None
	key_id, sep, secret = token[len(PREFIX) :].partition("_")
	return (key_id, secret) if sep and key_id and secret else (None, None)


# ---------- Autenticação (auth_hooks) ----------


def authenticate():
	from cred_hub import rest_v1

	rest_v1.ensure_registered()
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
	req_verb = (frappe.request.method or "GET").upper()
	req_path = unquote(frappe.request.path or "")
	# Version that serves the call (and Deprecation/Sunset when the route is on its way out), on every answer
	set_version_headers(_set_header, req_verb, req_path)
	# An IP that keeps failing authentication is stopped before any lookup or hashing
	_check_auth_failures()
	key_id, secret = _split_token(token.strip())
	key = frappe.db.get_value(
		KEY,
		{"key_id": key_id} if key_id else {"name": ""},
		["name", "secret_hash", "status", "expires_on", "technical_user", "allowed_ips", "custom_rate_limit", "is_test"],
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
	# Test key (sandbox, credtech#375): every answer says so, and the route check refuses what costs or is global
	frappe.local.credhub_api_is_test = bool(cint(key.is_test))
	if frappe.local.credhub_api_is_test:
		_set_header(TEST_HEADER, "1")
	rest_v1.normalize_v1_form(req_path, req_verb)
	try:
		write = _check_route(key.name)
	except (KeyNotAllowed, frappe.DoesNotExistError, rest_v1.MethodNotAllowedError) as route_exc:
		_refuse(key, request_id, start_time, getattr(route_exc, "http_status_code", 403), route_exc)
	rate_info = _rate_limit(key.name, write, key.custom_rate_limit, _write_weight() if write else 1)
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
	# A pinned version that does not exist is refused (400) before anything runs, logged as any other error
	frappe.local.credhub_api_key["api_version"] = requested_version()
	# Plan features (credtech#398): a credit write or paid query answers 403 PlanFeatureNotIncluded when the company's
	# plan lacks Credit & Collection; after credhub_api_key is set, so the refusal is logged and gets the error envelope
	_check_plan_feature()
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
	if write:
		# Idempotency-Key (credtech#375): last, after scopes, rate limit and the record checks; a resent call answers
		# what the first one answered (or 409/422) without running again
		from cred_hub import api_idempotency

		frappe.local.credhub_api_key["idempotency"] = api_idempotency.begin(key.name, request_id)


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


def _safe_route():
	try:
		return _route()
	except Exception:
		return None, None, (getattr(frappe.request, "method", None) or "GET").upper()


def _log_refusal(key_name: str, status: int, request_id: str | None = None, start_time: float | None = None):
	kind, target, verb = _safe_route()
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
	"""403/404/405 of a key whose secret was verified: logged and counted in its rate limit, so probing the scopes is visible
	and throttled (audit M13). The log is committed: the request fails."""
	try:
		_rate_limit(key.name, False, key.custom_rate_limit)
	finally:
		try:
			_log_refusal(key.name, status, request_id, start_time)
		except Exception:
			frappe.log_error(title="Cred Hub API: falha ao registrar chamada recusada")
	if exc is KeyNotAllowed or isinstance(exc, KeyNotAllowed):
		_deny()
	raise exc


# ---------- Chaves antigas (token api_key:api_secret de um usuário) ----------


# Old keys were announced as deprecated this many days before they stop (owner decision: 30-day transition)
LEGACY_NOTICE_DAYS = 30


def legacy_tokens_until():
	"""Último dia das chaves antigas: `credhub_legacy_token_until` (AAAA-MM-DD), posto no deploy da API nova com
	30 dias de transição (decisão do dono). Sem a config, as chaves antigas seguem como hoje."""
	value = frappe.conf.get("credhub_legacy_token_until")
	return getdate(value) if value else None


def _check_legacy_token():
	"""O Frappe já autenticou o token do usuário; depois do prazo, só o Administrator do site segue usando."""
	until = legacy_tokens_until()
	user = frappe.session.user
	if until and getdate(nowdate()) <= until and user not in ("Guest", "Administrator"):
		# the transition window is announced on every call made with an old key (RFC 9745 / RFC 8594)
		for name, value in deprecation_headers(add_days(until, -LEGACY_NOTICE_DAYS), until).items():
			_set_header(name, value)
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
	from cred_hub import rest_v1

	path = unquote(frappe.request.path or "")
	if rest_v1.is_sovereign_v1_path(path):
		verb = (frappe.request.method or "GET").upper()
		return bool(rest_v1.resolve_for_auth(path, verb)[3])
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if path.startswith(prefix):
			return len([p for p in path[len(prefix) :].split("/") if p]) >= 2
	return False


def _resource_name() -> str | None:
	from cred_hub import rest_v1

	path = unquote(frappe.request.path or "")
	if rest_v1.is_sovereign_v1_path(path):
		verb = (frappe.request.method or "GET").upper()
		return rest_v1.resolve_for_auth(path, verb)[3]
	for prefix in ("/api/resource/", "/api/v2/document/"):
		if path.startswith(prefix):
			parts = [p for p in path[len(prefix) :].split("/") if p]
			return parts[1] if len(parts) >= 2 else None
	return None


def _route():
	"""(kind, target, verbo): kind é resource, method, file_download, other (barrado) ou None fora da API."""
	from cred_hub import rest_v1

	path = unquote(frappe.request.path or "")
	verb = (frappe.request.method or "GET").upper()
	if rest_v1.is_sovereign_v1_path(path):
		kind, target, eff_verb, _rec_name = rest_v1.resolve_for_auth(path, verb)
		return kind, target, eff_verb
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
	if getattr(frappe.local, "credhub_api_is_test", False):
		# a test key never runs paid queries nor changes company-wide fields, even if a scope slipped in
		scopes -= TEST_FORBIDDEN_SCOPES
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
		if target == "cred_hub.credit_check.api.upload_checklist_document":
			# o arquivo (files_write) e a leitura da Pasta Digital que a resposta devolve (credit_read)
			if {"files_write", "credit_read"} <= scopes and verb == "POST":
				return True
			_deny()
		if target in ("cred_hub.bulk.bulk_upsert", "cred_hub.bulk.bulk_delete", "cred_hub.api_keys.upsert_record"):
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


# Credit routes the plan gates (credtech#398). Reads of what already exists stay open (nothing disappears on a
# downgrade, as in cred-hub#116); every gated method is also gated inside the function (billing.requires_feature).
PLAN_GATED_SCOPES = {"credit_write", "credit_paid_queries"}
PLAN_GATED_METHODS = {
	"cred_hub.credit_check.api.upload_checklist_document",
	"cred_hub.erp.push.upsert_titles",
	"cred_hub.erp.push.settle_titles",
	"cred_hub.erp.push.register_result",
}


def _check_plan_feature():
	kind, target, verb = _route()
	scope = None
	if kind == "resource":
		read, write = RESOURCES.get(target, (None, None))
		scope = read if verb in ("GET", "HEAD") else write
	elif kind == "method":
		scope = METHODS.get(target)
	if scope in PLAN_GATED_SCOPES or (kind == "method" and target in PLAN_GATED_METHODS):
		require_feature(CREDIT_AND_COLLECTION)


# save_pipeline pela chave: só campos e regras de um funil da chave, nunca criar funil, membros ou distribuição
SAVE_PIPELINE_KEYS = {"name", "fields", "field_rules"}


def is_fields_admin_call() -> bool:
	"""Hook crm_field_admin: this request comes from an API key with the fields admin scope, running as its technical
	user. The route was already checked against the key in authenticate; this checks it again where the crm decides."""
	info = getattr(frappe.local, "credhub_api_key", None)
	if not info or not info.get("name"):
		return False
	technical_user = frappe.db.get_value(KEY, info["name"], "technical_user")
	return technical_user == frappe.session.user and "fields_admin" in _scopes(info["name"])


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


def _write_weight() -> int:
	"""A batch call counts one write per record (audit L4), up to the batch maximum."""
	kind, target, _verb = _route()
	batch_arg = {
		"cred_hub.bulk.bulk_upsert": "records",
		"cred_hub.bulk.bulk_delete": "names",
		"cred_hub.erp.push.upsert_titles": "titles",
		"cred_hub.erp.push.settle_titles": "settlements",
	}.get(target if kind == "method" else None)
	if not batch_arg:
		return 1
	form = frappe.local.form_dict or {}
	items = form.get(batch_arg)
	try:
		items = frappe.parse_json(items) if isinstance(items, str) else items
	except Exception:
		return 1
	from cred_hub.bulk import MAX_BATCH_SIZE

	return max(1, min(len(items), MAX_BATCH_SIZE)) if isinstance(items, list) else 1


def _rate_limit(key_name: str, write: bool, custom_limit: int | None = None, weight: int = 1) -> dict:
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
		# a batch weighs one write per record, but never more than the whole limit: with a small custom limit a full
		# batch still fits an empty minute (and otherwise answers 429 with Retry-After)
		step = min(weight, limit) if counter == "write" else 1
		curr_count = frappe.cache.incrby(cache_key_curr, step)
		if curr_count == step:
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
	from cred_hub import rest_v1

	req_path = getattr(request, "path", None) or (
		getattr(frappe.request, "path", None) if getattr(frappe, "request", None) else ""
	)
	is_v1 = rest_v1.is_sovereign_v1_path(req_path)
	info = getattr(frappe.local, "credhub_api_key", None)
	if not info:
		if is_v1 and response is not None and (getattr(response, "status_code", 200) or 200) >= 400:
			hdr_req_id = None
			if hasattr(frappe.local, "response_headers"):
				hdr_req_id = frappe.local.response_headers.get("X-Request-Id")
			rest_v1.clean_v1_error_response(response, hdr_req_id)
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

		if status_code >= 400 and response is not None:
			if is_v1:
				status_code = rest_v1.clean_v1_error_response(response, request_id)
			else:
				_error_envelope(response, request_id)
		if info.get("idempotency"):
			_finish_idempotency(info["idempotency"], response)
		write = bool(info.get("write"))
		# Último uso: no máximo uma gravação por minuto por chave em leituras (audit M12)
		touch_key = f"credhub_api_last_used:{info['name']}"
		touched = False
		if write or status_code >= 400 or not frappe.cache.get_value(touch_key):
			frappe.db.set_value(
				KEY,
				info["name"],
				{"last_used_on": now_datetime(), "last_used_ip": frappe.local.request_ip},
				update_modified=False,
			)
			frappe.cache.set_value(touch_key, 1, expires_in_sec=LAST_USED_THROTTLE_SECONDS)
			touched = True
		# Auditoria: escritas e erros; leituras com sucesso não viram linha (audit M12)
		kind, target, verb = _safe_route()
		if not write and status_code < 400:
			if touched:
				frappe.db.commit()
			return
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
		details = _audit_details(kind, target)
		if details and log_meta.has_field("details"):
			log_payload["details"] = details
		frappe.get_doc(log_payload).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Cred Hub API: falha ao registrar o uso da chave")


def _finish_idempotency(claim: dict, response):
	"""Store the answer of an Idempotency-Key call (or free the key); a failure here leaves the key in flight (409)
	rather than ever running the write twice."""
	from cred_hub import api_idempotency

	try:
		api_idempotency.finish(claim, response)
	except Exception:
		frappe.log_error(title="Cred Hub API: falha ao guardar a resposta do Idempotency-Key")


# Argumentos de uma chamada de "Campos e regras" guardados no log (quem mudou o quê); nunca segredos
AUDITED_ARGS = (
	"doctype",
	"fieldname",
	"label",
	"fieldtype",
	"options",
	"placement",
	"required",
	"pipeline",
	"stage",
	"block",
	"fields",
	"doc",
)
AUDIT_DETAILS_LIMIT = 4000


def _audit_details(kind: str | None, target: str | None) -> str | None:
	if kind != "method" or METHODS.get(target) != "fields_admin":
		return None
	form = frappe.local.form_dict or {}
	details = {arg: form.get(arg) for arg in AUDITED_ARGS if form.get(arg) not in (None, "")}
	return json.dumps(details, default=str, ensure_ascii=False)[:AUDIT_DETAILS_LIMIT]


LAST_USED_THROTTLE_SECONDS = 60
CLEANUP_BATCH = 5000


def _error_text(payload: dict, response) -> str:
	for raw in frappe.parse_json(payload.get("_server_messages") or "[]") or []:
		try:
			message = frappe.parse_json(raw).get("message")
		except Exception:
			message = raw
		if message:
			return frappe.utils.strip_html(str(message))
	exception = str(payload.get("exception") or "")
	if exception:
		return exception.split(":", 1)[-1].strip()
	return getattr(response, "status", "") or "Error"


def _error_envelope(response, request_id: str):
	"""Errors to a key also carry {"error": {"code", "message", "request_id"}} (audit L2); Frappe's own keys stay for
	clients that already read them."""
	try:
		if getattr(response, "mimetype", None) != "application/json":
			return
		payload = json.loads(response.get_data(as_text=True) or "{}")
		if not isinstance(payload, dict) or "error" in payload:
			return
		payload["error"] = {
			"code": payload.get("exc_type") or str(response.status_code),
			"message": _error_text(payload, response),
			"request_id": request_id,
		}
		response.set_data(json.dumps(payload, default=str))
	except Exception:
		pass


def delete_old_logs():
	"""Daily, in batches (a big log never holds one long delete)."""
	cutoff = add_days(now_datetime(), -LOG_DAYS)
	while True:
		names = frappe.get_all(LOG, {"called_on": ("<", cutoff)}, pluck="name", limit_page_length=CLEANUP_BATCH)
		if not names:
			break
		frappe.db.delete(LOG, {"name": ("in", names)})
		frappe.db.commit()
		if len(names) < CLEANUP_BATCH:
			break


# Índices das sincronizações e da auditoria (audit H3, M15): criados no migrate, idempotentes
SYNC_INDEXES = (
	("CRM Deal", ("modified", "name")),
	("CRM Organization", ("modified", "name")),
	("Contact", ("modified", "name")),
	("CRM Task", ("modified", "name")),
	("FCRM Note", ("modified", "name")),
	("CRM Lead", ("modified", "name")),
	("Deleted Document", ("deleted_doctype", "creation")),
)


def ensure_indexes():
	for doctype, columns in SYNC_INDEXES:
		try:
			frappe.db.add_index(doctype, list(columns), index_name="credhub_" + "_".join(columns))
		except Exception:
			frappe.log_error(title=f"Cred Hub API: índice de {doctype} não criado")


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
		frappe.throw(_("Invalid pagination cursor."), InvalidParameter)


class InvalidParameter(frappe.ValidationError):
	"""A call argument the API cannot use (answered with 400, audit L2)."""

	http_status_code = 400


# Standard columns a listing may ask for besides the doctype's fields
LISTABLE_STANDARD = ("name", "creation", "modified", "modified_by", "owner")


def _cursor_fields(doctype: str, fields) -> list[str]:
	if fields in (None, "", []):
		return ["name", "modified", "creation"]
	if isinstance(fields, str):
		try:
			fields = frappe.parse_json(fields)
		except Exception:
			fields = [f.strip() for f in fields.split(",") if f.strip()]
	if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
		frappe.throw(_("fields must be a list of field names."), InvalidParameter)
	unknown = [f for f in fields if not _listable(doctype, f)]
	if unknown:
		frappe.throw(_("Unknown fields: {0}").format(", ".join(unknown)), InvalidParameter)
	for required_field in ("name", "modified"):
		if required_field not in fields:
			fields.append(required_field)
	return fields


# Filter operators a listing may use, and how many filters one call may send
FILTER_OPERATORS = frozenset(
	{"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "is", "between", "timespan"}
)
MAX_FILTERS = 20


def _listable(doctype: str, fieldname) -> bool:
	"""A column a key may read or filter on: standard columns, or fields of the doctype that are not passwords and
	have no field-level permission (permlevel > 0). Anything else could be read or probed through filters."""
	if not isinstance(fieldname, str):
		return False
	if fieldname in LISTABLE_STANDARD:
		return True
	df = frappe.get_meta(doctype).get_field(fieldname)
	return bool(df) and df.fieldtype != "Password" and not frappe.utils.cint(df.permlevel)


def _cursor_filters(doctype: str, filters) -> list:
	"""[[doctype, field, operator, value], ...] checked one by one (audit review of #105): same doctype, listable
	field, known operator, at most MAX_FILTERS. Otherwise 400."""
	if filters in (None, "", [], {}):
		return []
	if isinstance(filters, str):
		try:
			filters = frappe.parse_json(filters)
		except Exception:
			frappe.throw(_("filters must be JSON."), InvalidParameter)
	if isinstance(filters, dict):
		items = []
		for field, value in filters.items():
			if isinstance(value, list | tuple) and len(value) == 2 and isinstance(value[0], str):
				items.append([doctype, field, value[0], value[1]])
			else:
				items.append([doctype, field, "=", value])
	elif isinstance(filters, list):
		items = []
		for item in filters:
			if not isinstance(item, list | tuple) or len(item) not in (3, 4):
				frappe.throw(_("Each filter must be [field, operator, value]."), InvalidParameter)
			items.append(list(item) if len(item) == 4 else [doctype, *item])
	else:
		frappe.throw(_("filters must be JSON."), InvalidParameter)
	if len(items) > MAX_FILTERS:
		frappe.throw(_("At most {0} filters per call.").format(MAX_FILTERS), InvalidParameter)
	for dt, field, operator, _value in items:
		if dt != doctype or not _listable(doctype, field):
			frappe.throw(_("Filter on a field that cannot be used: {0}").format(field), InvalidParameter)
		if str(operator).lower() not in FILTER_OPERATORS:
			frappe.throw(_("Filter operator not allowed: {0}").format(operator), InvalidParameter)
	return items


def _since(value: str | None) -> str | None:
	if not value:
		return None
	try:
		return str(frappe.utils.get_datetime(str(value).strip()))
	except Exception:
		frappe.throw(_("updated_since must be a date and time like 2026-11-01 00:00:00."), InvalidParameter)


@frappe.whitelist()
def list_records_cursor(
	doctype: str,
	fields=None,
	filters=None,
	updated_since: str | None = None,
	limit: int = 50,
	cursor: str | None = None,
) -> dict:
	"""Listagem determinística por cursor (modified, name) para sincronização de ERPs sem perda nem duplicidade.

	O cursor vai para o SQL: (modified > m) OR (modified = m AND name > n), escrito como
	modified >= m AND (modified > m OR name > n), com limit + 1 linhas e índice (modified, name) — nenhuma página
	depende do tamanho da tabela nem perde registros (audit H3)."""
	if doctype not in RESOURCES:
		frappe.throw(_("Unsupported resource doctype: {0}").format(doctype), frappe.PermissionError)

	limit = max(1, min(cint(limit or 50), 200))
	fields = _cursor_fields(doctype, fields)

	base_filters = _cursor_filters(doctype, filters)

	since = _since(updated_since)
	if since:
		base_filters.append([doctype, "modified", ">=", since])

	or_filters = None
	if cursor:
		cursor_mod, cursor_name = _decode_cursor(cursor)
		base_filters.append([doctype, "modified", ">=", cursor_mod])
		or_filters = [[doctype, "modified", ">", cursor_mod], [doctype, "name", ">", cursor_name]]

	rows = frappe.get_list(
		doctype,
		fields=fields,
		filters=base_filters,
		or_filters=or_filters,
		order_by="modified asc, name asc",
		limit_page_length=limit + 1,
	)

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
					"outcome",
					"deal_value",
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
					fields=[
						"name",
						"organization",
						"organization_name",
						"pipeline",
						"status",
						"outcome",
						"deal_value",
						"annual_revenue",
						"deal_owner",
						"credit_verdict",
						"credit_report",
						"modified",
					],
					order_by="modified desc",
					limit_page_length=25,
				)

	return _limit_lookup_to_scopes(result)


@frappe.whitelist(methods=["POST"])
def upsert_record(doctype: str, record: dict | str, match_by: str | None = None) -> dict:
	"""Cria ou atualiza um único registro com resolução inteligente de identidade (credtech#375).

	- Requer chave de API com escopo de escrita correspondente (cards_write, contacts_write, organizations_write).
	- Resolve o registro automaticamente no mesmo espaço (produção vs sandbox) por name, external_id, cnpj/tax_id ou cpf/tax_id/email_id (ou restrito a `match_by`).
	- Devolve o status ('created' ou 'updated'), o ID interno do Frappe (name), o external_id e o registro público sanitizado (`api_schema.record`).
	"""
	from cred_hub import api_guard, api_schema
	from cred_hub.bulk import (
		SUPPORTED_DOCTYPES,
		prepare_upsert_payload,
		resolve_record_name,
		sync_contact_channels,
	)

	key_info = getattr(frappe.local, "credhub_api_key", None)
	if not key_info:
		frappe.throw(_("This method is only accessible via API Key."), frappe.PermissionError)

	if doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("Upsert operation is not supported for {0}.").format(doctype), frappe.ValidationError)

	if isinstance(record, str):
		try:
			record = frappe.parse_json(record)
		except Exception:
			frappe.throw(_("Invalid JSON in record parameter."), frappe.ValidationError)

	if not isinstance(record, dict) or not record:
		frappe.throw(_("The 'record' parameter must be a non-empty JSON object."), frappe.ValidationError)

	if record.get("doctype") not in (None, "", doctype):
		frappe.throw(_("Record doctype must be {0}.").format(doctype), frappe.ValidationError)

	matched_name = resolve_record_name(doctype, record, match_by=match_by)
	exists = bool(matched_name)
	if exists and getattr(frappe.local, "credhub_api_is_test", False):
		if not frappe.has_permission(doctype, "read", doc=matched_name):
			exists = False
			matched_name = None

	data = prepare_upsert_payload(doctype, record)
	api_guard.check_write(doctype, data, matched_name if exists else None)

	if exists:
		doc = frappe.get_doc(doctype, matched_name)
		doc.check_permission("write")
		doc.update(data)
		sync_contact_channels(doc, data)
		doc.save()
		action = "updated"
	else:
		doc_data = {k: v for k, v in data.items() if k != "name"}
		doc = frappe.get_doc({"doctype": doctype, **doc_data})
		sync_contact_channels(doc, doc_data)
		doc.check_permission("create")
		doc.insert()
		action = "created"

	public_data = api_schema.record(doc)
	if "credit_read" not in _scopes(key_info["name"]):
		for field in CREDIT_FIELDS:
			public_data.pop(field, None)

	return {
		"ok": True,
		"action": action,
		"name": doc.name,
		"doctype": doc.doctype,
		"external_id": doc.get("external_id") or None,
		"data": public_data,
	}


@frappe.whitelist(allow_guest=True)
def get_openapi_spec(
	openapi_version: str | None = None,
	format: str | None = None,
	include_legacy: int | bool = False,
) -> dict:
	"""OpenAPI 3.1 (or 3.0.3) of this company's API (v1 sovereign surface by default).

	- Authenticated users/keys get custom fields and visible pipelines.
	- Guests get the public canonical schema with generic pipeline names.
	- format="postman" exports a ready Postman Collection v2.1.
	- openapi_version="3.0" converts to OpenAPI 3.0.3 for legacy ERP tooling.
	- include_legacy=1 includes legacy /api/resource/* and /api/method/* aliases alongside /api/v1/*.
	"""
	from cred_hub.api_schema import openapi_spec, to_openapi_3_0, to_postman_collection

	surface = "all" if frappe.utils.sbool(include_legacy) else "v1"
	spec = openapi_spec(surface=surface)
	if format == "postman":
		return to_postman_collection(spec)
	if openapi_version in ("3.0", "3.0.3"):
		return to_openapi_3_0(spec)
	return spec


@frappe.whitelist(allow_guest=True)
def get_postman_collection(include_legacy: int | bool = False) -> dict:
	"""Downloads a ready Postman Collection v2.1 (sovereign /api/v1/* surface by default)."""
	from cred_hub.api_schema import openapi_spec, to_postman_collection

	surface = "all" if frappe.utils.sbool(include_legacy) else "v1"
	return to_postman_collection(openapi_spec(surface=surface))


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
			**{field: 0 for field in QUIET_USER_FIELDS},
			"roles": [{"role": role} for role in TECHNICAL_ROLES],
		}
	)
	user.flags.ignore_permissions = True
	user.flags.no_welcome_mail = True
	user.flags.credhub_api_key_flow = True
	user.insert()
	mute_technical_user(user.name)
	return user.name


# A technical user never gets e-mail nor notifications: its address is a placeholder (`.invalid`), but an assignment,
# mention, follow or thread e-mail would still queue (and try to send) a message to it
QUIET_USER_FIELDS = (
	"thread_notify",
	"send_me_a_copy",
	"document_follow_notify",
	"follow_created_documents",
	"follow_commented_documents",
	"follow_liked_documents",
	"follow_shared_documents",
	"follow_assigned_documents",
)
QUIET_NOTIFICATION_FIELDS = ("enabled", "enable_email_notifications")


def mute_technical_user(user: str):
	"""No e-mail and no notification for a technical user, whatever the path: preferences off, notifications off and
	a global unsubscribe of its address (Frappe drops it before creating the Email Queue). Idempotent."""
	values = {field: 0 for field in QUIET_USER_FIELDS if frappe.get_meta("User").has_field(field)}
	if values:
		frappe.db.set_value("User", user, values, update_modified=False)
	settings = frappe.db.exists("Notification Settings", user)
	if not settings:
		frappe.get_doc({"doctype": "Notification Settings", "name": user}).insert(ignore_permissions=True)
	frappe.db.set_value(
		"Notification Settings", user, {field: 0 for field in QUIET_NOTIFICATION_FIELDS}, update_modified=False
	)
	email = frappe.db.get_value("User", user, "email") or user
	if not frappe.db.exists("Email Unsubscribe", {"email": email, "global_unsubscribe": 1}):
		frappe.get_doc({"doctype": "Email Unsubscribe", "email": email, "global_unsubscribe": 1}).insert(
			ignore_permissions=True
		)


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
		"is_test": cint(doc.get("is_test")),
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
	is_test: int | None = None,
) -> dict:
	"""Cria a chave e devolve o token completo UMA vez (só o hash fica gravado). `is_test`: chave de teste, presa ao
	funil Sandbox (criado na primeira), sem consultas pagas nem campos da empresa (credtech#375)."""
	_only_admin()
	label = (label or "").strip()
	if not label:
		frappe.throw(_("Give the key a name."))
	if frappe.db.count(KEY, {"status": "Active"}) >= MAX_KEYS:
		frappe.throw(_("This company already has {0} active API keys. Revoke one to create another.").format(MAX_KEYS))
	is_test = cint(is_test)
	values = _clean_scopes(scopes)
	if is_test:
		check_test_scopes(values)
		pipelines = [ensure_sandbox_pipeline()]
	else:
		pipelines = _clean_pipelines(pipelines)
		check_not_sandbox(pipelines)
	_check_card_pipelines(values, pipelines)
	_check_ips(allowed_ips)
	key_id, secret, token = _new_secret(TEST_KEY_ID_PREFIX if is_test else "")
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
			"is_test": is_test,
			**values,
		}
	).insert(ignore_permissions=True)
	_sync_pipelines(user, pipelines)
	clear_test_users_cache()
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
		values = _clean_scopes(scopes)
		if cint(doc.get("is_test")):
			check_test_scopes(values)
		doc.update(values)
	if pipelines is not None:
		# a test key stays on the Sandbox pipeline; a real key never gets it
		if cint(doc.get("is_test")):
			pipelines = [ensure_sandbox_pipeline()]
		else:
			pipelines = _clean_pipelines(pipelines)
			check_not_sandbox(pipelines)
		doc.set("pipelines", [{"pipeline": p} for p in pipelines])
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
	clear_test_users_cache()
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
	install_sandbox()
	# technical users created before they were muted (no e-mail, no notification)
	for user in technical_users():
		mute_technical_user(user)
	if not frappe.db.exists("Role", API_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": API_ROLE, "desk_access": 0}).insert(ignore_permissions=True)
	# Migração pode mudar campos e eventos: a especificação OpenAPI e a lista de webhooks ativos são lidas de novo
	from cred_hub.api_schema import clear_openapi_cache
	from cred_hub.webhooks import clear_active_cache

	clear_openapi_cache()
	clear_active_cache()
	ensure_indexes()
	try:
		from cred_hub.credit_check.onboarding import migrate_all_to_relational

		migrate_all_to_relational()
	except Exception:
		pass
