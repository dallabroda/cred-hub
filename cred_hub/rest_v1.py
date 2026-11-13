"""Camada REST soberana `/api/v1/*` do HubTech CRM + envelope JSON limpo de erros.

Objetivos arquiteturais:
1. Expor rotas RESTful limpas (`/api/v1/deals`, `/api/v1/organizations`, `/api/v1/contacts`, `/api/v1/lookup`,
   `/api/v1/erp/titles`, `/api/v1/credit/checklist`, `/api/v1/openapi.json`, etc.) sem vazar nomes internos de módulos
   Python ou referências ao framework subjacente.
2. Preservar 100% de retrocompatibilidade com `/api/method/*` e `/api/resource/*` para integrações já em produção e para
   a SPA interna do CRM.
3. Compartilhar integralmente a blindagem de `api_keys.py` (HMAC-SHA256, 19 escopos, isolamento por funil, Sandbox
   `is_test`, `api_guard`, rate limit sliding-window com peso em lote, `Idempotency-Key`, `X-HubTech-API-Version` e
   auditoria em `Cred Hub API Log`).
4. Padronizar todas as respostas de erro em `/api/v1/*` em um único envelope JSON limpo (`{"error": {"code", "type",
   "message", "request_id"}}`), convertendo `HTTP 417` para `HTTP 422 Unprocessable Entity` e suprimindo `_server_messages`,
   `exc` e páginas HTML de erro mesmo quando chamado pelo navegador.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any
from urllib.parse import unquote

from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.routing import Map, Rule, Submount
from werkzeug.wrappers import Response

try:
	import frappe
	from frappe import _
	_BASE_VALIDATION_EXC = frappe.ValidationError
except (ImportError, TypeError):
	frappe = None

	def _(msg: str) -> str:
		return msg

	_BASE_VALIDATION_EXC = Exception

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
	if frappe is not None and getattr(frappe, "utils", None):
		return frappe.utils.strip_html(text)
	return _HTML_TAG_RE.sub("", text)

V1_PREFIX = "/api/v1"
LEGACY_V1_PREFIXES = ("/api/v1/method/", "/api/v1/resource/", "/api/v1/method", "/api/v1/resource")

# Slugs públicos -> Doctypes internos (o integrador nunca precisa conhecer "CRM Deal" ou "FCRM Note")
RESOURCE_SLUGS: dict[str, str] = {
	"deals": "CRM Deal",
	"deal": "CRM Deal",
	"cards": "CRM Deal",
	"card": "CRM Deal",
	"organizations": "CRM Organization",
	"organization": "CRM Organization",
	"contacts": "Contact",
	"contact": "Contact",
	"leads": "CRM Lead",
	"lead": "CRM Lead",
	"notes": "FCRM Note",
	"note": "FCRM Note",
	"tasks": "CRM Task",
	"task": "CRM Task",
	"files": "File",
	"file": "File",
}

DOCTYPE_TO_SLUG: dict[str, str] = {
	"CRM Deal": "deals",
	"CRM Organization": "organizations",
	"Contact": "contacts",
	"CRM Lead": "leads",
	"FCRM Note": "notes",
	"CRM Task": "tasks",
	"File": "files",
}

GUEST_ENDPOINTS = frozenset({"v1_openapi", "v1_postman", "v1_health"})


class MethodNotAllowedError(_BASE_VALIDATION_EXC):
	http_status_code = 405


class EndpointNotFoundError(Exception):
	http_status_code = 404


def is_sovereign_v1_path(path: str | None) -> bool:
	"""True para `/api/v1/*`, exceto os submounts legados `/api/v1/method/*` e `/api/v1/resource/*`."""
	clean = unquote(path or "").rstrip("/") or "/"
	if clean == V1_PREFIX:
		return True
	if not clean.startswith(V1_PREFIX + "/"):
		return False
	return not any(clean == p or clean.startswith(p if p.endswith("/") else p + "/") for p in LEGACY_V1_PREFIXES)


def resolve_doctype(value: str | None, default: str | None = None) -> str | None:
	if not value:
		return default
	raw = str(value).strip()
	return RESOURCE_SLUGS.get(raw.lower(), raw)


def json_response(payload: Any, status: int = 200) -> Response:
	return Response(
		json.dumps(payload, default=str, ensure_ascii=False),
		status=status,
		content_type="application/json; charset=utf-8",
	)


def _require_key_auth():
	if not getattr(frappe.local, "credhub_api_key", None):
		raise frappe.AuthenticationError(
			_("Authentication required. Send header Authorization: Bearer hub_<id>_<secret>.")
		)


def _form() -> dict:
	return frappe.local.form_dict or {}


def _public_doc(doc) -> dict:
	from cred_hub import api_schema
	from cred_hub.api_keys import CREDIT_FIELDS, _scopes

	if doc.doctype in api_schema.PUBLIC_DOCTYPES:
		data = api_schema.record(doc)
	elif doc.doctype == "File":
		data = {
			"name": doc.name,
			"file_name": doc.get("file_name"),
			"file_url": doc.get("file_url"),
			"file_size": doc.get("file_size"),
			"is_private": doc.get("is_private"),
			"attached_to_doctype": doc.get("attached_to_doctype"),
			"attached_to_name": doc.get("attached_to_name"),
			"creation": str(doc.creation) if doc.get("creation") else None,
			"modified": str(doc.modified) if doc.get("modified") else None,
		}
	else:
		data = {
			"name": doc.name,
			"doctype": doc.doctype,
			"creation": str(doc.creation) if doc.get("creation") else None,
			"modified": str(doc.modified) if doc.get("modified") else None,
		}

	info = getattr(frappe.local, "credhub_api_key", None)
	if info and "credit_read" not in _scopes(info["name"]):
		for field in CREDIT_FIELDS:
			data.pop(field, None)
	return data


# ---------- Endpoints REST `/api/v1/*` ----------


def _handle_collection(doctype: str) -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	form = _form()
	if verb in ("GET", "HEAD"):
		from cred_hub import api_schema
		from cred_hub.api_keys import CREDIT_FIELDS, _scopes, list_records_cursor

		fields = form.get("fields")
		if fields in (None, "", []):
			if doctype in api_schema.PUBLIC_DOCTYPES:
				fields = api_schema.public_fieldnames(doctype)
				info = getattr(frappe.local, "credhub_api_key", None)
				if info and "credit_read" not in _scopes(info["name"]):
					fields = [f for f in fields if f not in CREDIT_FIELDS]
			elif doctype == "File":
				fields = [
					"name",
					"file_name",
					"file_url",
					"file_size",
					"is_private",
					"attached_to_doctype",
					"attached_to_name",
					"creation",
					"modified",
				]
		res = list_records_cursor(
			doctype=doctype,
			fields=fields,
			filters=form.get("filters"),
			updated_since=form.get("updated_since"),
			limit=form.get("limit") or form.get("limit_page_length") or 50,
			cursor=form.get("cursor"),
		)
		return json_response(res, status=200)

	# POST -> Create
	from cred_hub import api_guard
	from cred_hub.bulk import sync_contact_channels

	payload = api_guard.request_payload()
	payload.pop("doctype", None)
	doc = frappe.new_doc(doctype, **payload)
	if doctype == "Contact":
		sync_contact_channels(doc, payload)
	doc.insert()
	return json_response({"data": _public_doc(doc)}, status=201)


def _handle_item(doctype: str, name: str) -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	if verb in ("GET", "HEAD"):
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		doc.apply_fieldlevel_read_permissions()
		return json_response({"data": _public_doc(doc)}, status=200)

	if verb in ("PUT", "PATCH"):
		from cred_hub import api_guard
		from cred_hub.bulk import sync_contact_channels

		payload = api_guard.request_payload()
		payload.pop("flags", None)
		payload.pop("doctype", None)
		doc = frappe.get_doc(doctype, name, for_update=True)
		doc.check_permission("write")
		doc.update(payload)
		if doctype == "Contact":
			sync_contact_channels(doc, payload)
		doc.save()
		return json_response({"data": _public_doc(doc)}, status=200)

	if verb == "DELETE":
		frappe.delete_doc(doctype, name, ignore_missing=False)
		return json_response({"ok": True, "deleted": name, "resource": DOCTYPE_TO_SLUG.get(doctype, doctype)}, status=200)

	raise MethodNotAllowedError(_("HTTP method {0} is not allowed on this endpoint.").format(verb))


def _handle_upsert(doctype: str) -> Response:
	_require_key_auth()
	from cred_hub import api_guard
	from cred_hub.api_keys import upsert_record

	payload = api_guard.request_payload()
	match_by = payload.get("match_by") if isinstance(payload.get("record"), dict) else _form().get("match_by")
	record_data = payload.get("record") if isinstance(payload.get("record"), dict) else {
		k: v for k, v in payload.items() if k not in ("doctype", "resource", "match_by")
	}
	res = upsert_record(doctype=doctype, record=record_data, match_by=match_by)
	status = 201 if res.get("action") == "created" else 200
	return json_response(res, status=status)


def _handle_comments(doctype: str, name: str) -> Response:
	_require_key_auth()
	from cred_hub import timeline

	verb = (frappe.request.method or "GET").upper()
	form = _form()
	if verb in ("GET", "HEAD"):
		rows = timeline.get_comments(reference_doctype=doctype, reference_name=name, limit=form.get("limit") or 50)
		return json_response({"data": rows, "count": len(rows)}, status=200)
	res = timeline.add_comment(reference_doctype=doctype, reference_name=name, content=form.get("content") or "")
	return json_response({"data": res}, status=201)


def v1_deals() -> Response:
	return _handle_collection("CRM Deal")


def v1_deals_upsert() -> Response:
	return _handle_upsert("CRM Deal")


def v1_deal_item(name: str) -> Response:
	return _handle_item("CRM Deal", name)


def v1_deal_move(name: str) -> Response:
	_require_key_auth()
	from crm.api.pipeline import move_card

	form = _form()
	stage = form.get("stage") or form.get("status")
	if not stage:
		from cred_hub.api_keys import InvalidParameter

		frappe.throw(_("Missing required parameter: stage."), InvalidParameter)
	res = move_card(deal=name, stage=stage, values=form.get("values"))
	return json_response({"data": res}, status=200)


def v1_deal_change_pipeline(name: str) -> Response:
	_require_key_auth()
	from crm.api.pipeline import change_pipeline

	form = _form()
	pipeline = form.get("pipeline")
	if not pipeline:
		from cred_hub.api_keys import InvalidParameter

		frappe.throw(_("Missing required parameter: pipeline."), InvalidParameter)
	kwargs = {"deal": name, "pipeline": pipeline}
	if form.get("stage"):
		kwargs["stage"] = form.get("stage")
	if form.get("values") is not None:
		kwargs["values"] = form.get("values")
	res = frappe.call(change_pipeline, **kwargs)
	return json_response({"data": res}, status=200)


def v1_deal_contacts(name: str) -> Response:
	_require_key_auth()
	from crm.api.card_contacts import get_card_contacts

	res = frappe.call(get_card_contacts, deal=name)
	return json_response({"data": res}, status=200)


def v1_deal_comments(name: str) -> Response:
	return _handle_comments("CRM Deal", name)


def v1_organizations() -> Response:
	return _handle_collection("CRM Organization")


def v1_organizations_upsert() -> Response:
	return _handle_upsert("CRM Organization")


def v1_organization_item(name: str) -> Response:
	return _handle_item("CRM Organization", name)


def v1_organization_comments(name: str) -> Response:
	return _handle_comments("CRM Organization", name)


def v1_contacts() -> Response:
	return _handle_collection("Contact")


def v1_contacts_upsert() -> Response:
	return _handle_upsert("Contact")


def v1_contact_item(name: str) -> Response:
	return _handle_item("Contact", name)


def v1_contact_comments(name: str) -> Response:
	return _handle_comments("Contact", name)


def v1_leads() -> Response:
	return _handle_collection("CRM Lead")


def v1_notes() -> Response:
	return _handle_collection("FCRM Note")


def v1_note_item(name: str) -> Response:
	return _handle_item("FCRM Note", name)


def v1_tasks() -> Response:
	return _handle_collection("CRM Task")


def v1_task_item(name: str) -> Response:
	return _handle_item("CRM Task", name)


def v1_files() -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	if verb in ("GET", "HEAD"):
		return _handle_collection("File")
	from frappe.handler import upload_file

	doc = upload_file()
	return json_response({"data": _public_doc(doc)}, status=201)


def v1_file_item(name: str) -> Response:
	return _handle_item("File", name)


def v1_pipelines() -> Response:
	_require_key_auth()
	from crm.api.pipeline import get_pipelines

	return json_response({"data": get_pipelines()}, status=200)


def v1_pipeline_item(name: str) -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	if verb in ("GET", "HEAD"):
		from crm.api.pipeline import get_pipeline_config

		return json_response({"data": get_pipeline_config(name)}, status=200)
	from crm.api.pipeline import save_pipeline

	doc_arg = _form().get("doc")
	return json_response({"data": save_pipeline(doc_arg)}, status=200)


def v1_fields() -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	form = _form()
	if verb in ("GET", "HEAD"):
		from crm.api.pipeline import get_field_catalog

		kwargs = {}
		if form.get("doctype") or form.get("resource"):
			kwargs["doctype"] = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")
		return json_response({"data": frappe.call(get_field_catalog, **kwargs)}, status=200)

	from crm.api.forms import create_field

	doctype = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")
	res = create_field(
		label=form.get("label"),
		fieldtype=form.get("fieldtype"),
		options=form.get("options"),
		doctype=doctype,
		placement=form.get("placement"),
		required=form.get("required"),
	)
	return json_response({"data": res}, status=201)


def v1_field_item(name: str) -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "PATCH").upper()
	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")
	if verb == "DELETE":
		from crm.api.forms import disable_field

		return json_response({"data": disable_field(fieldname=name, doctype=doctype)}, status=200)
	from crm.api.forms import update_field

	return json_response(
		{
			"data": update_field(
				fieldname=name,
				label=form.get("label"),
				options=form.get("options"),
				doctype=doctype,
			)
		},
		status=200,
	)


def v1_forms_schema() -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "GET").upper()
	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")
	if verb in ("GET", "HEAD"):
		from crm.api.forms import get_form_schema

		res = get_form_schema(
			doctype=doctype,
			pipeline=form.get("pipeline"),
			stage=form.get("stage"),
			block=form.get("block"),
		)
		return json_response({"data": res}, status=200)
	from crm.api.forms import save_form_config

	res = save_form_config(
		doctype=doctype,
		fields=form.get("fields"),
		pipeline=form.get("pipeline"),
		stage=form.get("stage"),
		block=form.get("block"),
	)
	return json_response({"data": res}, status=200)


def v1_lookup() -> Response:
	_require_key_auth()
	from cred_hub.api_keys import lookup_by_document

	return json_response({"data": lookup_by_document(document=_form().get("document") or "")}, status=200)


def v1_sync_records() -> Response:
	_require_key_auth()
	from cred_hub.api_keys import InvalidParameter, list_records_cursor

	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"))
	if not doctype:
		frappe.throw(_("Provide 'resource' (e.g. deals, organizations, contacts) or 'doctype'."), InvalidParameter)
	res = list_records_cursor(
		doctype=doctype,
		fields=form.get("fields"),
		filters=form.get("filters"),
		updated_since=form.get("updated_since"),
		limit=form.get("limit") or 50,
		cursor=form.get("cursor"),
	)
	return json_response(res, status=200)


def v1_sync_deleted() -> Response:
	_require_key_auth()
	from cred_hub.api_keys import InvalidParameter
	from cred_hub.sync import get_deleted_records

	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"))
	if not doctype:
		frappe.throw(_("Provide 'resource' (e.g. deals, organizations, contacts) or 'doctype'."), InvalidParameter)
	rows = get_deleted_records(doctype=doctype, since=form.get("since") or form.get("deleted_since"))
	return json_response({"data": rows, "count": len(rows)}, status=200)


def v1_bulk_upsert() -> Response:
	_require_key_auth()
	from cred_hub.bulk import bulk_upsert

	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"))
	return json_response(bulk_upsert(doctype=doctype, records=form.get("records")), status=200)


def v1_bulk_delete() -> Response:
	_require_key_auth()
	from cred_hub.bulk import bulk_delete

	form = _form()
	doctype = resolve_doctype(form.get("resource") or form.get("doctype"))
	return json_response(bulk_delete(doctype=doctype, names=form.get("names")), status=200)


def v1_credit_onboarding() -> Response:
	_require_key_auth()
	from cred_hub.credit_check.api import get_credit_onboarding

	form = _form()
	res = frappe.call(get_credit_onboarding, organization=form.get("organization"), deal=form.get("deal"))
	return json_response({"data": res}, status=200)


def v1_credit_checklist() -> Response:
	_require_key_auth()
	from cred_hub.credit_check.api import get_document_checklist

	form = _form()
	return json_response({"data": get_document_checklist(organization=form.get("organization"), deal=form.get("deal"))}, status=200)


def v1_credit_checklist_upload() -> Response:
	_require_key_auth()
	from cred_hub.credit_check.api import upload_checklist_document

	form = _form()
	res = upload_checklist_document(
		item_key=form.get("item_key"),
		organization=form.get("organization"),
		deal=form.get("deal"),
		valid_until=form.get("valid_until"),
	)
	return json_response({"data": res}, status=201)


def v1_credit_checklist_items() -> Response:
	_require_key_auth()
	from cred_hub.credit_check.api import update_checklist_item

	form = _form()
	res = frappe.call(
		update_checklist_item,
		organization=form.get("organization"),
		item_key=form.get("item_key"),
		status=form.get("status"),
		notes=form.get("notes"),
		reason=form.get("reason"),
		valid_until=form.get("valid_until"),
		deal=form.get("deal"),
	)
	return json_response({"data": res}, status=200)


def v1_credit_portal_link() -> Response:
	_require_key_auth()
	verb = (frappe.request.method or "POST").upper()
	form = _form()
	if verb == "DELETE":
		from cred_hub.credit_check.api import revoke_portal_link

		res = frappe.call(revoke_portal_link, organization=form.get("organization"), deal=form.get("deal"))
		return json_response({"data": res}, status=200)
	from cred_hub.credit_check.api import create_onboarding_portal_link

	res = frappe.call(
		create_onboarding_portal_link,
		organization=form.get("organization"),
		deal=form.get("deal"),
		regenerate=form.get("regenerate", False),
	)
	return json_response({"data": res}, status=200)


def v1_credit_checks() -> Response:
	_require_key_auth()
	from cred_hub.credit_check.api import check_deal

	form = _form()
	res = frappe.call(check_deal, deal_name=form.get("deal") or form.get("deal_name"))
	return json_response({"data": res}, status=200)


def v1_erp_titles() -> Response:
	_require_key_auth()
	from cred_hub.erp.push import upsert_titles

	form = _form()
	res = upsert_titles(
		titles=form.get("titles"),
		erp_source=form.get("erp_source") or "External ERP",
		run_id=form.get("run_id"),
	)
	return json_response(res, status=200)


def v1_erp_settlements() -> Response:
	_require_key_auth()
	from cred_hub.erp.push import settle_titles

	form = _form()
	res = settle_titles(
		settlements=form.get("settlements"),
		erp_source=form.get("erp_source") or "External ERP",
	)
	return json_response(res, status=200)


def v1_erp_registrations() -> Response:
	_require_key_auth()
	from cred_hub.erp.push import register_result

	form = _form()
	res = frappe.call(
		register_result,
		deal=form.get("deal"),
		status=form.get("status"),
		erp_source=form.get("erp_source") or "External ERP",
		external_id=form.get("external_id") or form.get("erp_code"),
		error_message=form.get("error_message") or form.get("message"),
	)
	return json_response(res, status=200)


def v1_openapi() -> Response:
	from cred_hub.api_keys import get_openapi_spec

	form = _form()
	include_legacy = frappe.utils.sbool(form.get("include_legacy"))
	spec = get_openapi_spec(
		openapi_version=form.get("openapi_version"),
		format=form.get("format"),
		include_legacy=include_legacy,
	)
	return json_response(spec, status=200)


def v1_postman() -> Response:
	from cred_hub.api_keys import get_postman_collection

	include_legacy = frappe.utils.sbool(_form().get("include_legacy"))
	return json_response(get_postman_collection(include_legacy=include_legacy), status=200)


def v1_health() -> Response:
	from cred_hub.health import check

	res = check()
	status = cint_or(getattr(frappe.response, "http_status_code", 200), 200)
	return json_response(res, status=status)


def cint_or(val: Any, fallback: int) -> int:
	try:
		return int(val)
	except Exception:
		return fallback


# ---------- Tabela de Roteamento Werkzeug + Resolução de Escopos ----------

ENDPOINT_HANDLERS = {
	"v1_openapi": v1_openapi,
	"v1_postman": v1_postman,
	"v1_health": v1_health,
	"v1_deals": v1_deals,
	"v1_deals_upsert": v1_deals_upsert,
	"v1_deal_item": v1_deal_item,
	"v1_deal_move": v1_deal_move,
	"v1_deal_change_pipeline": v1_deal_change_pipeline,
	"v1_deal_contacts": v1_deal_contacts,
	"v1_deal_comments": v1_deal_comments,
	"v1_organizations": v1_organizations,
	"v1_organizations_upsert": v1_organizations_upsert,
	"v1_organization_item": v1_organization_item,
	"v1_organization_comments": v1_organization_comments,
	"v1_contacts": v1_contacts,
	"v1_contacts_upsert": v1_contacts_upsert,
	"v1_contact_item": v1_contact_item,
	"v1_contact_comments": v1_contact_comments,
	"v1_leads": v1_leads,
	"v1_notes": v1_notes,
	"v1_note_item": v1_note_item,
	"v1_tasks": v1_tasks,
	"v1_task_item": v1_task_item,
	"v1_files": v1_files,
	"v1_file_item": v1_file_item,
	"v1_pipelines": v1_pipelines,
	"v1_pipeline_item": v1_pipeline_item,
	"v1_fields": v1_fields,
	"v1_field_item": v1_field_item,
	"v1_forms_schema": v1_forms_schema,
	"v1_lookup": v1_lookup,
	"v1_sync_records": v1_sync_records,
	"v1_sync_deleted": v1_sync_deleted,
	"v1_bulk_upsert": v1_bulk_upsert,
	"v1_bulk_delete": v1_bulk_delete,
	"v1_credit_onboarding": v1_credit_onboarding,
	"v1_credit_checklist": v1_credit_checklist,
	"v1_credit_checklist_upload": v1_credit_checklist_upload,
	"v1_credit_checklist_items": v1_credit_checklist_items,
	"v1_credit_portal_link": v1_credit_portal_link,
	"v1_credit_checks": v1_credit_checks,
	"v1_erp_titles": v1_erp_titles,
	"v1_erp_settlements": v1_erp_settlements,
	"v1_erp_registrations": v1_erp_registrations,
}

URL_RULES = [
	Rule("/openapi.json", methods=["GET", "HEAD"], endpoint=v1_openapi),
	Rule("/postman.json", methods=["GET", "HEAD"], endpoint=v1_postman),
	Rule("/health", methods=["GET", "HEAD"], endpoint=v1_health),
	# Deals
	Rule("/deals", methods=["GET", "HEAD", "POST"], endpoint=v1_deals),
	Rule("/deals/upsert", methods=["POST"], endpoint=v1_deals_upsert),
	Rule("/deals/<path:name>/move", methods=["POST"], endpoint=v1_deal_move),
	Rule("/deals/<path:name>/pipeline", methods=["POST"], endpoint=v1_deal_change_pipeline),
	Rule("/deals/<path:name>/contacts", methods=["GET", "HEAD"], endpoint=v1_deal_contacts),
	Rule("/deals/<path:name>/comments", methods=["GET", "HEAD", "POST"], endpoint=v1_deal_comments),
	Rule("/deals/<path:name>", methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"], endpoint=v1_deal_item),
	# Organizations
	Rule("/organizations", methods=["GET", "HEAD", "POST"], endpoint=v1_organizations),
	Rule("/organizations/upsert", methods=["POST"], endpoint=v1_organizations_upsert),
	Rule("/organizations/<path:name>/comments", methods=["GET", "HEAD", "POST"], endpoint=v1_organization_comments),
	Rule("/organizations/<path:name>", methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"], endpoint=v1_organization_item),
	# Contacts
	Rule("/contacts", methods=["GET", "HEAD", "POST"], endpoint=v1_contacts),
	Rule("/contacts/upsert", methods=["POST"], endpoint=v1_contacts_upsert),
	Rule("/contacts/<path:name>/comments", methods=["GET", "HEAD", "POST"], endpoint=v1_contact_comments),
	Rule("/contacts/<path:name>", methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"], endpoint=v1_contact_item),
	# Leads, Notes, Tasks, Files
	Rule("/leads", methods=["POST"], endpoint=v1_leads),
	Rule("/notes", methods=["GET", "HEAD", "POST"], endpoint=v1_notes),
	Rule("/notes/<path:name>", methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"], endpoint=v1_note_item),
	Rule("/tasks", methods=["GET", "HEAD", "POST"], endpoint=v1_tasks),
	Rule("/tasks/<path:name>", methods=["GET", "HEAD", "PUT", "PATCH", "DELETE"], endpoint=v1_task_item),
	Rule("/files", methods=["GET", "HEAD", "POST"], endpoint=v1_files),
	Rule("/files/<path:name>", methods=["GET", "HEAD", "DELETE"], endpoint=v1_file_item),
	# Pipelines, Fields, Forms
	Rule("/pipelines", methods=["GET", "HEAD"], endpoint=v1_pipelines),
	Rule("/pipelines/<path:name>", methods=["GET", "HEAD", "PUT"], endpoint=v1_pipeline_item),
	Rule("/fields", methods=["GET", "HEAD", "POST"], endpoint=v1_fields),
	Rule("/fields/<string:name>", methods=["PATCH", "DELETE"], endpoint=v1_field_item),
	Rule("/forms/schema", methods=["GET", "HEAD", "PUT"], endpoint=v1_forms_schema),
	# Search, Sync, Bulk
	Rule("/lookup", methods=["GET", "HEAD"], endpoint=v1_lookup),
	Rule("/sync/records", methods=["GET", "HEAD"], endpoint=v1_sync_records),
	Rule("/sync/deleted", methods=["GET", "HEAD"], endpoint=v1_sync_deleted),
	Rule("/bulk/upsert", methods=["POST"], endpoint=v1_bulk_upsert),
	Rule("/bulk/delete", methods=["POST"], endpoint=v1_bulk_delete),
	# Credit & Digital Folder
	Rule("/credit/onboarding", methods=["GET", "HEAD"], endpoint=v1_credit_onboarding),
	Rule("/credit/checklist", methods=["GET", "HEAD"], endpoint=v1_credit_checklist),
	Rule("/credit/checklist/upload", methods=["POST"], endpoint=v1_credit_checklist_upload),
	Rule("/credit/checklist/items", methods=["PATCH"], endpoint=v1_credit_checklist_items),
	Rule("/credit/portal-link", methods=["POST", "DELETE"], endpoint=v1_credit_portal_link),
	Rule("/credit/checks", methods=["POST"], endpoint=v1_credit_checks),
	# ERP Push (Porta B)
	Rule("/erp/titles", methods=["POST"], endpoint=v1_erp_titles),
	Rule("/erp/settlements", methods=["POST"], endpoint=v1_erp_settlements),
	Rule("/erp/registrations", methods=["POST"], endpoint=v1_erp_registrations),
]

_AUTH_MAP = Map(
	[Submount(V1_PREFIX, [Rule(r.rule, methods=r.methods, endpoint=r.endpoint.__name__) for r in URL_RULES])],
	strict_slashes=False,
	merge_slashes=False,
)
_REGISTERED_IN_FRAPPE = False


def ensure_registered():
	"""Registra as rotas `/api/v1/*` no `frappe.api.API_URL_MAP` uma única vez por processo worker."""
	global _REGISTERED_IN_FRAPPE
	if _REGISTERED_IN_FRAPPE:
		return
	import frappe.api

	frappe.api.API_URL_MAP.add(Submount(V1_PREFIX, URL_RULES))
	_REGISTERED_IN_FRAPPE = True


def match_v1(path: str, verb: str) -> tuple[str, dict[str, Any]]:
	"""Resolve `(endpoint_name, kwargs)` para uma chamada `/api/v1/*`, levantando 404 ou 405 se inválida."""
	adapter = _AUTH_MAP.bind("hubtech.local")
	clean_path = unquote(path or "")
	try:
		return adapter.match(clean_path, method=verb.upper())
	except MethodNotAllowed as exc:
		raise MethodNotAllowedError(_("HTTP method {0} is not allowed for {1}.").format(verb.upper(), clean_path)) from exc
	except NotFound as exc:
		not_found_cls = frappe.DoesNotExistError if frappe is not None and hasattr(frappe, "DoesNotExistError") else EndpointNotFoundError
		raise not_found_cls(_("API v1 endpoint not found: {0}").format(clean_path)) from exc


def normalize_v1_form(path: str, verb: str):
	"""Normaliza aliases amigáveis (`resource` -> `doctype`, `record_id` -> `docname`, etc.) no `form_dict` antes dos
	checks de escopo, guarda de escrita, rate limit e idempotência."""
	if not is_sovereign_v1_path(path):
		return
	form = frappe.local.form_dict
	if form is None:
		return
	try:
		endpoint, kwargs = match_v1(path, verb)
	except Exception:
		return

	if form.get("resource") and not form.get("doctype"):
		form["doctype"] = resolve_doctype(form.get("resource"))
	if endpoint == "v1_deals_upsert":
		form["doctype"] = "CRM Deal"
	elif endpoint == "v1_organizations_upsert":
		form["doctype"] = "CRM Organization"
	elif endpoint == "v1_contacts_upsert":
		form["doctype"] = "Contact"
	elif endpoint == "v1_files" and verb.upper() == "POST":
		if form.get("resource") and not form.get("doctype"):
			form["doctype"] = resolve_doctype(form.get("resource"))
		if form.get("record_id") and not form.get("docname"):
			form["docname"] = form.get("record_id")
	elif endpoint == "v1_pipeline_item" and verb.upper() == "PUT":
		if not form.get("doc"):
			doc_payload = {k: v for k, v in form.items() if k in ("fields", "field_rules")}
			doc_payload["name"] = kwargs["name"]
			form["doc"] = doc_payload
	elif endpoint in ("v1_field_item",):
		form.setdefault("fieldname", kwargs["name"])
		form["doctype"] = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")
	elif endpoint in ("v1_fields", "v1_forms_schema") and verb.upper() in ("POST", "PUT", "PATCH"):
		form["doctype"] = resolve_doctype(form.get("resource") or form.get("doctype"), "CRM Deal")


def resolve_for_auth(path: str, verb: str) -> tuple[str | None, str | None, str, str | None]:
	"""Traduz uma rota `/api/v1/*` para `(kind, target, effective_verb, record_name)` entendido por `api_keys.py`."""
	verb = (verb or "GET").upper()
	endpoint, kwargs = match_v1(path, verb)
	name = kwargs.get("name")

	resource_collections = {
		"v1_deals": "CRM Deal",
		"v1_organizations": "CRM Organization",
		"v1_contacts": "Contact",
		"v1_leads": "CRM Lead",
		"v1_notes": "FCRM Note",
		"v1_tasks": "CRM Task",
	}
	if endpoint in resource_collections:
		return "resource", resource_collections[endpoint], verb, None

	if endpoint == "v1_files":
		if verb in ("GET", "HEAD"):
			return "resource", "File", verb, None
		return "method", "upload_file", "POST", None

	resource_items = {
		"v1_deal_item": "CRM Deal",
		"v1_organization_item": "CRM Organization",
		"v1_contact_item": "Contact",
		"v1_note_item": "FCRM Note",
		"v1_task_item": "CRM Task",
		"v1_file_item": "File",
	}
	if endpoint in resource_items:
		return "resource", resource_items[endpoint], verb, name

	if endpoint in ("v1_deals_upsert", "v1_organizations_upsert", "v1_contacts_upsert"):
		return "method", "cred_hub.api_keys.upsert_record", "POST", None

	if endpoint in ("v1_deal_comments", "v1_organization_comments", "v1_contact_comments"):
		target = "cred_hub.timeline.get_comments" if verb in ("GET", "HEAD") else "cred_hub.timeline.add_comment"
		return "method", target, verb, name

	if endpoint == "v1_pipeline_item":
		target = "crm.api.pipeline.get_pipeline_config" if verb in ("GET", "HEAD") else "crm.api.pipeline.save_pipeline"
		return "method", target, "POST" if verb == "PUT" else verb, name

	if endpoint == "v1_fields":
		target = "crm.api.pipeline.get_field_catalog" if verb in ("GET", "HEAD") else "crm.api.forms.create_field"
		return "method", target, verb, None

	if endpoint == "v1_field_item":
		target = "crm.api.forms.disable_field" if verb == "DELETE" else "crm.api.forms.update_field"
		return "method", target, "POST", name

	if endpoint == "v1_forms_schema":
		target = "crm.api.forms.get_form_schema" if verb in ("GET", "HEAD") else "crm.api.forms.save_form_config"
		return "method", target, "POST" if verb == "PUT" else verb, None

	if endpoint == "v1_credit_portal_link":
		target = (
			"cred_hub.credit_check.api.revoke_portal_link"
			if verb == "DELETE"
			else "cred_hub.credit_check.api.create_onboarding_portal_link"
		)
		return "method", target, "POST", None

	method_map = {
		"v1_deal_move": ("crm.api.pipeline.move_card", "POST"),
		"v1_deal_change_pipeline": ("crm.api.pipeline.change_pipeline", "POST"),
		"v1_deal_contacts": ("crm.api.card_contacts.get_card_contacts", "GET"),
		"v1_pipelines": ("crm.api.pipeline.get_pipelines", "GET"),
		"v1_lookup": ("cred_hub.api_keys.lookup_by_document", "GET"),
		"v1_sync_records": ("cred_hub.api_keys.list_records_cursor", "GET"),
		"v1_sync_deleted": ("cred_hub.sync.get_deleted_records", "GET"),
		"v1_bulk_upsert": ("cred_hub.bulk.bulk_upsert", "POST"),
		"v1_bulk_delete": ("cred_hub.bulk.bulk_delete", "POST"),
		"v1_credit_onboarding": ("cred_hub.credit_check.api.get_credit_onboarding", "GET"),
		"v1_credit_checklist": ("cred_hub.credit_check.api.get_document_checklist", "GET"),
		"v1_credit_checklist_upload": ("cred_hub.credit_check.api.upload_checklist_document", "POST"),
		"v1_credit_checklist_items": ("cred_hub.credit_check.api.update_checklist_item", "POST"),
		"v1_credit_checks": ("cred_hub.credit_check.api.check_deal", "POST"),
		"v1_erp_titles": ("cred_hub.erp.push.upsert_titles", "POST"),
		"v1_erp_settlements": ("cred_hub.erp.push.settle_titles", "POST"),
		"v1_erp_registrations": ("cred_hub.erp.push.register_result", "POST"),
		"v1_openapi": ("cred_hub.api_keys.get_openapi_spec", "GET"),
		"v1_postman": ("cred_hub.api_keys.get_postman_collection", "GET"),
		"v1_health": ("cred_hub.health.check", "GET"),
	}
	if endpoint in method_map:
		m_target, m_verb = method_map[endpoint]
		return "method", m_target, m_verb, name
	return "other", None, verb, None


# ---------- Envelope JSON Único e Soberano de Erros para `/api/v1/*` ----------

STATUS_CODE_DEFAULTS = {
	400: ("bad_request", "BadRequestError", "Invalid request parameters."),
	401: ("authentication_error", "AuthenticationError", "Invalid, expired or missing API key."),
	403: ("permission_denied", "PermissionError", "This API key is not allowed to perform this operation."),
	404: ("not_found", "NotFoundError", "The requested resource or endpoint was not found."),
	405: ("method_not_allowed", "MethodNotAllowedError", "HTTP method not allowed on this endpoint."),
	409: ("conflict", "ConflictError", "Request conflicted with current state or concurrent operation."),
	413: ("payload_too_large", "PayloadTooLargeError", "Uploaded payload exceeds the maximum allowed size."),
	422: ("validation_error", "ValidationError", "Request validation failed."),
	429: ("rate_limit_exceeded", "RateLimitExceededError", "Rate limit exceeded. Retry after Retry-After seconds."),
}

EXC_TYPE_TO_CODE = {
	"InvalidParameter": "invalid_parameter",
	"UnsupportedApiVersion": "unsupported_api_version",
	"InvalidIdempotencyKey": "invalid_idempotency_key",
	"DataError": "bad_request",
	"AuthenticationError": "authentication_error",
	"PermissionError": "permission_denied",
	"KeyNotAllowed": "insufficient_scope",
	"PlanFeatureNotIncluded": "plan_feature_not_included",
	"DoesNotExistError": "not_found",
	"NotFound": "not_found",
	"MethodNotAllowed": "method_not_allowed",
	"MethodNotAllowedError": "method_not_allowed",
	"IdempotencyKeyInUse": "idempotency_key_in_use",
	"IdempotencyKeyInterrupted": "idempotency_key_interrupted",
	"IdempotencyKeyReused": "idempotency_key_reused",
	"DuplicateEntryError": "duplicate_record",
	"BatchRetryable": "database_busy_retry",
	"WriteRefused": "write_refused",
	"MandatoryError": "missing_required_field",
	"LinkValidationError": "invalid_reference",
	"ValidationError": "validation_error",
	"RateLimitExceededError": "rate_limit_exceeded",
}


def _extract_server_message(payload: dict) -> str | None:
	raw_list = payload.get("_server_messages")
	if not raw_list:
		return None
	try:
		parsed_list = json.loads(raw_list) if isinstance(raw_list, str) else raw_list
	except Exception:
		return None
	messages = []
	for item in parsed_list or []:
		try:
			msg = json.loads(item).get("message") if isinstance(item, str) else item.get("message")
		except Exception:
			msg = item
		if msg:
			clean = _strip_html(str(msg)).strip()
			if clean and clean not in messages:
				messages.append(clean)
	return "; ".join(messages) if messages else None


def clean_v1_error_response(response: Response, request_id: str | None = None) -> int:
	"""Reescreve qualquer erro (`status >= 400`) em `/api/v1/*` para JSON puro e soberano:
	- Normaliza `417 Expectation Failed` -> `422 Unprocessable Entity`
	- Converte páginas HTML de erro (`401`/`403`/`404`/`405`/`500`) em `application/json`
	- Remove `_server_messages`, `exc`, `exception`, `_error_message` e qualquer nome interno de módulo
	Devolve o `status_code` final normalizado.
	"""
	req_id = request_id or response.headers.get("X-Request-Id") or f"req_{secrets.token_hex(8)}"
	raw_status = getattr(response, "status_code", 500) or 500
	status = 422 if raw_status == 417 else raw_status

	payload: dict = {}
	if "json" in (getattr(response, "mimetype", "") or ""):
		try:
			loaded = json.loads(response.get_data(as_text=True) or "{}")
			if isinstance(loaded, dict):
				payload = loaded
		except Exception:
			payload = {}

	# Se já estiver exclusivamente no envelope limpo sem chaves legadas, apenas garante headers e status
	if set(payload.keys()) == {"error"} and isinstance(payload.get("error"), dict):
		payload["error"].setdefault("request_id", req_id)
		response.status_code = status
		response.mimetype = "application/json"
		response.headers["Content-Type"] = "application/json; charset=utf-8"
		response.headers["X-Request-Id"] = req_id
		response.set_data(json.dumps(payload, default=str, ensure_ascii=False))
		return status

	default_code, default_type, default_msg = STATUS_CODE_DEFAULTS.get(
		status, ("internal_error", "InternalServerError", "An unexpected internal error occurred.")
	)
	exc_type = str(payload.get("exc_type") or "").strip()
	if status >= 500:
		err_type = "InternalServerError"
		err_code = "internal_error"
		message = default_msg
	else:
		err_type = exc_type or default_type
		err_code = EXC_TYPE_TO_CODE.get(exc_type, default_code)
		message = _extract_server_message(payload)
		if not message:
			existing_err = payload.get("error")
			if isinstance(existing_err, dict) and existing_err.get("message"):
				message = str(existing_err["message"])
		if not message:
			exc_text = str(payload.get("exception") or "").strip()
			if exc_text and ":" in exc_text:
				candidate = _strip_html(exc_text.split(":", 1)[1]).strip()
				if candidate:
					message = candidate
		if not message and isinstance(payload.get("message"), str) and payload["message"].strip():
			message = _strip_html(payload["message"]).strip()
		if not message:
			message = default_msg

	# Sanitização soberana: nunca expor "frappe" na mensagem ou tipo público
	if "frappe" in err_type.lower():
		err_type = default_type
	if "frappe" in message.lower():
		message = default_msg

	clean_body = {
		"error": {
			"code": err_code,
			"type": err_type,
			"message": message,
			"request_id": req_id,
		}
	}
	response.status_code = status
	response.mimetype = "application/json"
	response.headers["Content-Type"] = "application/json; charset=utf-8"
	response.headers["X-Request-Id"] = req_id
	response.headers.setdefault("X-HubTech-API-Version", "1")
	response.set_data(json.dumps(clean_body, default=str, ensure_ascii=False))
	return status
