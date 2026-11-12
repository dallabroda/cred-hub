"""Public shape of HubTech records (credtech#375, ADR 0007 section 11): which fields leave the system and how.

One policy feeds the webhook payloads and the OpenAPI document, so what the spec promises is what the webhooks send:
- every value field of the record, including the company's own fields (`ch_*`), plus name, creation and modified;
- never a Password field, a table, a layout field, a disabled (hidden) field, an internal flag listed in
  DENIED_FIELDNAMES or a field whose name looks like a credential (SENSITIVE). The deny list wins over any request.

The OpenAPI document is built per site (the company's fields, pipelines and stages), cached, and dropped from the cache
by doc_events when a field, a pipeline or a stage changes (hooks.py).
"""

import copy
import re

try:
	import frappe
	from frappe.model import no_value_fields, table_fields
except (ImportError, TypeError):
	frappe = None
	no_value_fields, table_fields = (), ()

# Records the API documents and the webhooks describe
DEAL = "CRM Deal"
ORGANIZATION = "CRM Organization"
CONTACT = "Contact"
TASK = "CRM Task"
NOTE = "FCRM Note"
PUBLIC_DOCTYPES = (DEAL, ORGANIZATION, CONTACT, TASK, NOTE)
SCHEMA_NAMES = {DEAL: "Card", ORGANIZATION: "Organization", CONTACT: "Contact", TASK: "Task", NOTE: "Note"}
CUSTOM_PREFIX = "ch_"

# Standard columns that are part of the public record
STANDARD_FIELDS = ("name", "creation", "modified")
# Field types that never leave the system as record values
DENIED_FIELDTYPES = frozenset({"Password", *no_value_fields, *table_fields})
# Internal flags and integration bookkeeping, never sent
DENIED_FIELDNAMES = frozenset(
	{
		"naming_series",
		"erp_run_id",
		"user",
		"google_contacts",
		"google_contacts_id",
		"sync_with_google_contacts",
		"pulled_from_google_contacts",
		"google_calendar_event_id",
		"assertiva_protocol",
	}
)
# Anything named like a credential is left out, even a field the company created
SENSITIVE = re.compile(r"password|passwd|secret|token|api_?key|hash", re.IGNORECASE)
# Hidden on the form but part of the record
HIDDEN_ALLOWED = {CONTACT: frozenset({"full_name"})}

# Short views of the records linked to a card or a contact (webhook `related`)
ORGANIZATION_SUMMARY = ("name", "organization_name", "cnpj", "legal_name", "trade_name", "website", "industry")
CONTACT_SUMMARY = ("name", "full_name", "first_name", "last_name", "email_id", "mobile_no", "phone", "cpf")
# Credit verdict fields are not sent by a webhook unless its field list names them (owner decision pending,
# credtech#375): flip this switch to send them by default
CREDIT_FIELDS_IN_WEBHOOKS_BY_DEFAULT = False
WEBHOOK_OPT_IN_FIELDS = frozenset({"credit_verdict", "credit_report"})
# Keys of `related` for each record
RELATED_KEYS = {DEAL: ("organization", "second_organization", "contacts"), CONTACT: ("organization",)}

OPENAPI_CACHE_KEY = "credhub_openapi_spec"
OPENAPI_CACHE_SECONDS = 24 * 60 * 60
# Pipelines and stages listed as enums; past this the enum is left out (the spec stays small)
MAX_ENUM = 500


# ---------- Field policy ----------


def is_public(df, doctype: str) -> bool:
	fieldname = df.fieldname or ""
	if df.fieldtype in DENIED_FIELDTYPES or fieldname in DENIED_FIELDNAMES or SENSITIVE.search(fieldname):
		return False
	return not df.hidden or fieldname in HIDDEN_ALLOWED.get(doctype, ())


def public_fields(doctype: str) -> list:
	"""DocFields of `doctype` that the API shows, in form order. O(fields); the meta itself is cached by Frappe."""
	return [df for df in frappe.get_meta(doctype).fields if df.fieldname and is_public(df, doctype)]


def public_fieldnames(doctype: str) -> list[str]:
	return [*STANDARD_FIELDS, *(df.fieldname for df in public_fields(doctype))]


def record(doc) -> dict:
	"""The public view of a record: every public field, its value as stored (dates go out as text in JSON)."""
	return {fieldname: doc.get(fieldname) for fieldname in public_fieldnames(doc.doctype)}


def _summaries(doctype: str, names: list[str], fields: tuple) -> dict:
	"""{name: summary} with one query, only the summary fields the site has."""
	names = [n for n in dict.fromkeys(names) if n]
	if not names:
		return {}
	meta = frappe.get_meta(doctype)
	columns = [f for f in fields if f == "name" or meta.has_field(f)]
	rows = frappe.get_all(doctype, filters={"name": ("in", names)}, fields=columns, ignore_permissions=True)
	return {row.name: dict(row) for row in rows}


def related(doc) -> dict:
	"""Records linked to a card (its organizations and contacts) or to a contact (its organization). Two queries at
	most, whatever the number of contacts."""
	if doc.doctype == DEAL:
		org_names = [doc.get("organization"), doc.get("second_organization")]
		orgs = _summaries(ORGANIZATION, org_names, ORGANIZATION_SUMMARY)
		links = [row for row in doc.get("contacts") or [] if row.get("contact")]
		contacts = _summaries(CONTACT, [row.contact for row in links], CONTACT_SUMMARY)
		return {
			"organization": orgs.get(doc.get("organization")),
			"second_organization": orgs.get(doc.get("second_organization")),
			"contacts": [
				{**contacts[row.contact], "is_primary": int(bool(row.get("is_primary")))}
				for row in links
				if row.contact in contacts
			],
		}
	if doc.doctype == CONTACT:
		company = doc.get("company_name")
		return {"organization": _summaries(ORGANIZATION, [company], ORGANIZATION_SUMMARY).get(company)}
	return {}


def allowed_payload_keys() -> set[str]:
	"""Names a webhook may list in its field list: record fields of cards and contacts and the related keys."""
	keys = set()
	for doctype in (DEAL, CONTACT):
		keys.update(public_fieldnames(doctype))
		keys.update(RELATED_KEYS[doctype])
	return keys


# ---------- OpenAPI ----------

_STRING_TYPES = {
	"Data",
	"Small Text",
	"Text",
	"Long Text",
	"Text Editor",
	"Markdown Editor",
	"HTML Editor",
	"Code",
	"Phone",
	"Read Only",
	"Attach",
	"Attach Image",
	"Color",
	"Barcode",
	"Signature",
	"Autocomplete",
	"Icon",
}
_NUMBER_TYPES = {"Float", "Currency", "Percent", "Rating", "Duration"}
_INTEGER_TYPES = {"Int", "Long Int"}
_DATA_FORMATS = {"Email": "email", "URL": "uri"}


def field_schema(df) -> dict:
	"""JSON Schema of a DocField. Not required fields may also be null."""
	fieldtype = df.fieldtype
	schema: dict = {}
	if fieldtype in _INTEGER_TYPES:
		schema["type"] = "integer"
	elif fieldtype in _NUMBER_TYPES:
		schema["type"] = "number"
	elif fieldtype == "Check":
		schema.update(type="integer", enum=[0, 1])
	elif fieldtype == "Date":
		schema.update(type="string", format="date")
	elif fieldtype == "Datetime":
		schema.update(type="string", examples=["2026-11-11 09:30:00"])
	elif fieldtype == "Time":
		schema.update(type="string", examples=["09:30:00"])
	elif fieldtype == "Select":
		options = [o for o in (df.options or "").split("\n") if o != ""]
		schema.update(type="string", enum=options)
		if not df.reqd:
			schema["enum"] = [*options, ""]
	elif fieldtype == "Link":
		schema.update(type="string", **{"x-link": df.options})
	elif fieldtype == "Dynamic Link":
		schema.update(type="string", **{"x-dynamic-link": df.options})
	elif fieldtype in ("JSON", "Geolocation"):
		schema["type"] = "object"
	else:
		schema["type"] = "string"
		if fieldtype == "Data" and df.options in _DATA_FORMATS:
			schema["format"] = _DATA_FORMATS[df.options]
	if not df.reqd and "type" in schema:
		schema["type"] = [schema["type"], "null"]
		if "enum" in schema:
			schema["enum"] = [*schema["enum"], None]
	schema["title"] = df.label or df.fieldname
	schema["x-fieldtype"] = fieldtype
	if df.fieldname.startswith(CUSTOM_PREFIX):
		schema["x-custom"] = True
	if df.get("read_only"):
		schema["readOnly"] = True
	return schema


def _rows_for_enum(doctype: str, fields: list[str]) -> list:
	"""Rows of pipelines or stages for an enum; empty past MAX_ENUM (the enum is then left out)."""
	rows = frappe.get_all(doctype, fields=fields, order_by="creation asc", limit_page_length=MAX_ENUM + 1)
	return [] if len(rows) > MAX_ENUM else rows


def _pipeline_enum() -> tuple[list, dict]:
	rows = _rows_for_enum("CRM Pipeline", ["name", "pipeline_name"])
	return [r.name for r in rows], {r.name: r.pipeline_name or r.name for r in rows}


def _stage_enum(pipeline_labels: dict) -> tuple[list, dict]:
	"""Stage names, each labelled "Pipeline / Stage" (stage ids are not readable on their own)."""
	rows = _rows_for_enum("CRM Deal Status", ["name", "deal_status", "pipeline"])
	labels = {
		r.name: f"{pipeline_labels.get(r.pipeline) or r.pipeline or '-'} / {r.deal_status or r.name}" for r in rows
	}
	return [r.name for r in rows], labels


def record_schema(doctype: str, pipelines=None, stages=None) -> dict:
	properties = {
		"name": {"type": "string", "title": "ID", "readOnly": True},
		"creation": {"type": "string", "title": "Created on", "readOnly": True},
		"modified": {"type": "string", "title": "Last modified on", "readOnly": True},
	}
	required = []
	for df in public_fields(doctype):
		schema = field_schema(df)
		if doctype == DEAL and df.fieldname == "pipeline" and pipelines and pipelines[0]:
			schema.update(enum=pipelines[0], **{"x-enum-labels": pipelines[1]})
		if doctype == DEAL and df.fieldname == "status" and stages and stages[0]:
			schema.update(enum=stages[0], **{"x-enum-labels": stages[1]})
		properties[df.fieldname] = schema
		if df.reqd:
			required.append(df.fieldname)
	return {
		"type": "object",
		"title": SCHEMA_NAMES[doctype],
		"x-doctype": doctype,
		"properties": properties,
		"required": required,
	}


def _ref(name: str) -> dict:
	return {"$ref": f"#/components/schemas/{name}"}


def _json_response(description: str, schema: dict | None = None) -> dict:
	response = {"description": description}
	if schema:
		response["content"] = {"application/json": {"schema": {"type": "object", "properties": {"message": schema}}}}
	return response


def _resource_paths(doctype: str) -> dict:
	schema = SCHEMA_NAMES[doctype]
	tag = schema + "s"
	wrapped = {"type": "object", "properties": {"data": _ref(schema)}}
	collection = {
		"get": {
			"summary": f"List {tag.lower()}",
			"tags": [tag],
			"parameters": [
				{"name": "fields", "in": "query", "schema": {"type": "string"}, "example": '["name","modified"]'},
				{"name": "filters", "in": "query", "schema": {"type": "string"}},
				{"name": "limit_start", "in": "query", "schema": {"type": "integer"}},
				{"name": "limit_page_length", "in": "query", "schema": {"type": "integer"}},
			],
			"responses": {
				"200": {
					"description": "OK",
					"content": {
						"application/json": {
							"schema": {"type": "object", "properties": {"data": {"type": "array", "items": _ref(schema)}}}
						}
					},
				}
			},
		},
		"post": {
			"summary": f"Create a {schema.lower()}",
			"tags": [tag],
			"requestBody": {"required": True, "content": {"application/json": {"schema": _ref(schema)}}},
			"responses": {"200": {"description": "Created", "content": {"application/json": {"schema": wrapped}}}},
		},
	}
	item = {
		"parameters": [{"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}],
		"get": {
			"summary": f"Read a {schema.lower()}",
			"tags": [tag],
			"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": wrapped}}}},
		},
		"put": {
			"summary": f"Update a {schema.lower()} (only the fields sent change)",
			"tags": [tag],
			"requestBody": {"required": True, "content": {"application/json": {"schema": _ref(schema)}}},
			"responses": {"200": {"description": "Updated", "content": {"application/json": {"schema": wrapped}}}},
		},
		"delete": {
			"summary": f"Delete a {schema.lower()} (needs the allow_delete scope)",
			"tags": [tag],
			"responses": {"202": {"description": "Deleted"}},
		},
	}
	return {f"/api/resource/{doctype}": collection, f"/api/resource/{doctype}/{{name}}": item}


def _method(summary: str, tag: str, verb: str = "get", params=None, body=None, response=None, extra=None) -> dict:
	operation = {"summary": summary, "tags": [tag], "responses": {"200": response or {"description": "OK"}}}
	if params:
		operation["parameters"] = [
			{"name": n, "in": "query", "required": r, "schema": {"type": "string"}} for n, r in params
		]
	if body:
		operation["requestBody"] = body
	operation["responses"].update(extra or {})
	return {verb: operation}


def _form_body(properties: dict, required: list[str]) -> dict:
	schema = {"type": "object", "properties": properties, "required": required}
	return {
		"required": True,
		"content": {"application/json": {"schema": schema}, "application/x-www-form-urlencoded": {"schema": schema}},
	}


def _shared_schemas() -> dict:
	text = {"type": "string"}
	nullable_text = {"type": ["string", "null"]}
	item = {
		"type": "object",
		"properties": {
			"key": text,
			"label": text,
			"category": text,
			"required": {"type": "boolean"},
			"is_custom": {"type": "boolean"},
			"status": {
				"type": "string",
				"enum": ["pending", "attached", "verified", "rejected"],
				"description": "pending: waiting; attached: sent, waiting review; verified: approved; rejected: send again",
			},
			"reason": {**nullable_text, "description": "Why it was rejected (only when status is rejected)"},
			"notes": text,
			"file_name": text,
			"file_url": {**text, "description": "Private file: download it with a key that has files_read"},
			"valid_until": {**text, "description": "YYYY-MM-DD or empty"},
			"is_expired": {"type": "boolean"},
			"expiring_soon": {"type": "boolean"},
			"updated_by": text,
			"updated_on": text,
		},
	}
	return {
		"ChecklistItem": item,
		"Checklist": {
			"type": "object",
			"properties": {
				"organization": text,
				"deal": nullable_text,
				"summary": {"type": "object"},
				"items": {"type": "array", "items": _ref("ChecklistItem")},
			},
		},
		"CatalogField": {
			"type": "object",
			"properties": {
				"fieldname": text,
				"label": text,
				"fieldtype": text,
				"options": text,
				"is_standard": {"type": "integer"},
				"locked": {"type": "integer"},
				"disabled": {"type": "integer"},
			},
		},
		"WebhookEnvelope": {
			"type": "object",
			"description": "Body of every webhook (version 1). Sign check: X-HubTech-Signature-V2 = "
			"sha256=HMAC_SHA256(secret, X-HubTech-Timestamp + '.' + raw body).",
			"properties": {
				"event": text,
				"version": {"type": "integer", "const": 1},
				"delivery_id": {**text, "description": "Same on every retry of one delivery"},
				"attempt": {"type": "integer"},
				"timestamp": text,
				"doctype": {**nullable_text, "description": "Record type for deal.* and contact.* events"},
				"data": {"type": "object", "description": "The record (Card or Contact schema) or the event data"},
				"related": {"type": "object", "description": "Organizations and contacts linked to the record"},
			},
			"required": ["event", "version", "delivery_id", "attempt", "timestamp", "data"],
		},
	}


def _method_paths() -> dict:
	from crm.fcrm.doctype.crm_pipeline.fields import FIELD_TYPES

	m = "/api/method/"
	field_props = {
		"label": {"type": "string"},
		"fieldtype": {"type": "string", "enum": list(FIELD_TYPES)},
		"options": {"type": "string", "description": "Select: one option per line; Link: Contact or CRM Organization"},
		"doctype": {"type": "string", "enum": [DEAL, CONTACT, ORGANIZATION], "default": DEAL},
		"placement": {"type": "array", "items": {"type": "object"}, "description": "[{pipeline, stages}] (cards)"},
		"required": {"type": "boolean"},
	}
	catalog_field = {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": {"message": _ref("CatalogField")}}}}}
	checklist = {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": {"message": _ref("Checklist")}}}}}
	item = {"description": "OK", "content": {"application/json": {"schema": {"type": "object", "properties": {"message": _ref("ChecklistItem")}}}}}
	return {
		f"{m}crm.api.pipeline.get_pipelines": _method("List pipelines and stages", "Pipelines"),
		f"{m}crm.api.pipeline.get_field_catalog": _method("Field catalog (standard and company fields)", "Fields"),
		f"{m}crm.api.forms.get_form_schema": _method(
			"Form of a context (fields in order, visible, required, read only)",
			"Fields",
			params=[("doctype", True), ("pipeline", False), ("stage", False), ("block", False)],
		),
		f"{m}crm.api.forms.create_field": _method(
			"Create a company field (fields_admin)",
			"Fields",
			"post",
			body=_form_body(field_props, ["label", "fieldtype"]),
			response=catalog_field,
			extra={"403": {"description": "The key has no fields_admin scope"}},
		),
		f"{m}crm.api.forms.update_field": _method(
			"Rename a company field or change its options (fields_admin)",
			"Fields",
			"post",
			body=_form_body(
				{"fieldname": {"type": "string"}, "label": {"type": "string"}, "options": {"type": "string"}, "doctype": field_props["doctype"]},
				["fieldname"],
			),
			response=catalog_field,
		),
		f"{m}crm.api.forms.disable_field": _method(
			"Disable a company field; values are kept (fields_admin)",
			"Fields",
			"post",
			body=_form_body({"fieldname": {"type": "string"}, "doctype": field_props["doctype"]}, ["fieldname"]),
			response=catalog_field,
		),
		f"{m}crm.api.forms.save_form_config": _method(
			"Save the form of a pipeline/stage or of contacts/organizations (fields_admin)",
			"Fields",
			"post",
			body=_form_body(
				{
					"doctype": field_props["doctype"],
					"pipeline": {"type": "string"},
					"stage": {"type": "string"},
					"block": {"type": "string"},
					"fields": {"type": "array", "items": {"type": "object"}},
				},
				["doctype", "fields"],
			),
		),
		f"{m}crm.api.pipeline.move_card": _method(
			"Move a card to a stage (stage rules apply)", "Cards", "post", params=[("deal", True), ("stage", True)]
		),
		f"{m}cred_hub.api_keys.lookup_by_document": _method(
			"Find organization/contact, cards and Digital Folder by CNPJ or CPF", "Search", params=[("document", True)]
		),
		f"{m}cred_hub.api_keys.list_records_cursor": _method(
			"Cursor listing with updated_since (ERP sync)",
			"Sync",
			params=[("doctype", True), ("fields", False), ("updated_since", False), ("limit", False), ("cursor", False)],
		),
		f"{m}cred_hub.bulk.bulk_upsert": _method(
			"Create or update up to 50 records",
			"Sync",
			"post",
			extra={"409": {"description": "Database busy: nothing saved, send the same call again"}},
		),
		f"{m}cred_hub.bulk.bulk_delete": _method(
			"Delete up to 50 records (allow_delete)",
			"Sync",
			"post",
			extra={"409": {"description": "Database busy: nothing deleted, send the same call again"}},
		),
		f"{m}cred_hub.sync.get_deleted_records": _method("Deleted records since a date", "Sync"),
		f"{m}cred_hub.credit_check.api.get_document_checklist": _method(
			"Digital Folder checklist of an organization or card, item by item",
			"Digital Folder",
			params=[("organization", False), ("deal", False)],
			response=checklist,
		),
		f"{m}cred_hub.credit_check.api.upload_checklist_document": {
			"post": {
				"summary": "Upload a document into a checklist item (files_write + credit_read)",
				"tags": ["Digital Folder"],
				"requestBody": {
					"required": True,
					"content": {
						"multipart/form-data": {
							"schema": {
								"type": "object",
								"properties": {
									"item_key": {"type": "string"},
									"organization": {"type": "string"},
									"deal": {"type": "string"},
									"valid_until": {"type": "string", "format": "date"},
									"file": {"type": "string", "format": "binary"},
								},
								"required": ["item_key", "file"],
							}
						}
					},
				},
				"responses": {
					"200": item,
					"403": {"description": "The key lacks files_write or credit_read, or cannot see the record"},
					"417": {"description": "File refused (type, size, empty) or item already verified"},
				},
			}
		},
		f"{m}cred_hub.credit_check.api.create_onboarding_portal_link": _method(
			"Create or get the client's external upload link", "Digital Folder", "post"
		),
		f"{m}cred_hub.api_keys.get_openapi_spec": _method(
			"OpenAPI specification for this site (3.1 or 3.0.3, or format=postman)",
			"Schema",
			params=[("openapi_version", False), ("format", False)],
		),
		f"{m}cred_hub.api_keys.get_postman_collection": _method(
			"Ready-to-import Postman Collection v2.1 for this site",
			"Schema",
		),
	}


IDEMPOTENCY_PARAMETER = {
	"name": "Idempotency-Key",
	"in": "header",
	"required": False,
	"schema": {"type": "string", "minLength": 1, "maxLength": 255},
	"description": "Any unique value (a UUID). Resending the same request with the same key within 24 hours replays "
	"the first answer (header Idempotent-Replayed: true) instead of writing again.",
}
IDEMPOTENCY_RESPONSES = {
	"409": {"description": "A request with this Idempotency-Key is still running: wait and send again"},
	"422": {"description": "This Idempotency-Key was already used for a different request"},
}


def _add_idempotency(paths: dict):
	"""Every write accepts Idempotency-Key (credtech#375)."""
	for operations in paths.values():
		for verb in ("post", "put", "patch", "delete"):
			operation = operations.get(verb)
			if not operation:
				continue
			operation["parameters"] = [*operation.get("parameters", []), IDEMPOTENCY_PARAMETER]
			for code, response in IDEMPOTENCY_RESPONSES.items():
				operation.setdefault("responses", {}).setdefault(code, response)


def _webhook_specs() -> dict:
	"""Webhooks of HubTech CRM in OpenAPI 3.1 format (credtech#375)."""
	envelope_properties = {
		"event": {"type": "string", "example": "deal.stage_changed"},
		"version": {"type": "integer", "example": 1},
		"event_id": {"type": "string", "example": "evt_01931f82b2c47a98"},
		"delivery_id": {"type": "string", "example": "del_01931f82b2e58c12"},
		"attempt": {"type": "integer", "example": 1},
		"replay": {"type": "boolean", "example": False},
		"test": {"type": "boolean", "description": "true for test webhooks on the Sandbox pipeline", "example": False},
		"timestamp": {"type": "string", "format": "date-time", "example": "2026-11-12T18:00:00Z"},
		"doctype": {"type": "string", "example": "CRM Deal"},
		"data": {"type": "object", "description": "Full record payload with ch_* fields"},
		"related": {"type": "object", "description": "Linked organizations and contacts"},
	}

	events = {
		"deal.created": "A new card was created in a pipeline",
		"deal.stage_changed": "A card was moved to another stage",
		"deal.won": "A card reached a Won stage (outcome Won)",
		"deal.lost": "A card reached a Lost stage (outcome Lost)",
		"deal.deleted": "A card was deleted",
		"contact.created": "A new contact was registered",
		"contact.updated": "A contact phone, email or details changed",
		"checklist.item_rejected": "A document in the Digital Folder was rejected",
		"checklist.item_attached": "A document was attached to a Digital Folder checklist item",
		"credit.limit_decided": "A credit limit decision was recorded by the credit committee",
	}

	webhooks = {}
	for event_name, summary in events.items():
		webhooks[event_name] = {
			"post": {
				"summary": summary,
				"tags": ["Webhooks"],
				"requestBody": {
					"required": True,
					"content": {
						"application/json": {
							"schema": {
								"type": "object",
								"properties": {
									**envelope_properties,
									"event": {"type": "string", "example": event_name},
								},
								"required": ["event", "version", "event_id", "delivery_id", "attempt", "timestamp", "data"],
							}
						}
					},
				},
				"responses": {
					"200": {"description": "Webhook received successfully"},
				},
			}
		}
	return webhooks


def build_openapi_spec() -> dict:
	pipelines = _pipeline_enum()
	stages = _stage_enum(pipelines[1])
	schemas = {SCHEMA_NAMES[dt]: record_schema(dt, pipelines, stages) for dt in PUBLIC_DOCTYPES}
	schemas.update(_shared_schemas())
	paths = {}
	for doctype in PUBLIC_DOCTYPES:
		paths.update(_resource_paths(doctype))
	paths.update(_method_paths())
	_add_idempotency(paths)
	return {
		"openapi": "3.1.0",
		"info": {
			"title": "HubTech CRM API",
			"version": "2.0.0",
			"description": (
				"REST and RPC API of HubTech CRM for this company: cards, organizations, contacts, tasks, notes, "
				"pipelines, fields and the Digital Folder. Record schemas include the company's own fields (x-custom). "
				"API version 1: every answer carries X-HubTech-API-Version; send the same header to pin it (an unknown "
				"version answers 400). Breaking changes only in a new version, announced at least 90 days ahead with "
				"Deprecation and Sunset headers."
			),
			"x-hubtech-api-version": "1",
		},
		"components": {
			"securitySchemes": {
				"BearerAuth": {
					"type": "http",
					"scheme": "bearer",
					"bearerFormat": "hub_<key_id>_<secret>",
					"description": "Key created in Settings > Integrations > API.",
				}
			},
			"schemas": schemas,
		},
		"security": [{"BearerAuth": []}],
		"paths": paths,
		"webhooks": _webhook_specs(),
	}


def to_openapi_3_0(spec_3_1: dict) -> dict:
	"""Converts OpenAPI 3.1 to OpenAPI 3.0.3 for legacy ERP tooling (ADR 0007, credtech#375)."""
	spec = copy.deepcopy(spec_3_1)
	spec["openapi"] = "3.0.3"
	if "webhooks" in spec:
		spec["x-webhooks"] = spec.pop("webhooks")

	def _transform(obj):
		if isinstance(obj, dict):
			t = obj.get("type")
			if isinstance(t, list):
				types = [x for x in t if x != "null"]
				if "null" in t:
					obj["nullable"] = True
				obj["type"] = types[0] if len(types) == 1 else (types or "string")
			if "enum" in obj and isinstance(obj["enum"], list):
				if None in obj["enum"]:
					obj["enum"] = [x for x in obj["enum"] if x is not None]
					obj["nullable"] = True
			for v in obj.values():
				_transform(v)
		elif isinstance(obj, list):
			for item in obj:
				_transform(item)

	_transform(spec)
	return spec


def to_postman_collection(spec: dict) -> dict:
	"""Generates a ready Postman Collection v2.1 from the OpenAPI specification (credtech#375)."""
	base_url = (spec.get("servers") or [{}])[0].get("url") or "{{baseUrl}}"
	collection = {
		"info": {
			"name": spec.get("info", {}).get("title") or "HubTech CRM API",
			"description": spec.get("info", {}).get("description") or "",
			"schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
			"version": spec.get("info", {}).get("version") or "2.0.0",
		},
		"auth": {
			"type": "bearer",
			"bearer": [{"key": "token", "value": "{{hubtech_token}}", "type": "string"}],
		},
		"variable": [
			{"key": "baseUrl", "value": base_url, "type": "string"},
			{"key": "hubtech_token", "value": "hub_id_segredo", "type": "string"},
		],
		"item": [],
	}

	folders = {}
	for path, path_item in (spec.get("paths") or {}).items():
		for verb in ("get", "post", "put", "patch", "delete"):
			op = path_item.get(verb)
			if not op:
				continue
			tags = op.get("tags") or ["General"]
			tag = tags[0]
			if tag not in folders:
				folders[tag] = {"name": tag, "item": []}

			headers = [
				{"key": "Accept", "value": "application/json", "type": "text"},
				{"key": "X-HubTech-API-Version", "value": "1", "type": "text"},
			]
			if verb.upper() in ("POST", "PUT", "PATCH", "DELETE"):
				headers.append({"key": "Idempotency-Key", "value": "{{$guid}}", "type": "text"})
				headers.append({"key": "Content-Type", "value": "application/json", "type": "text"})

			query_params = []
			for param in op.get("parameters") or []:
				if param.get("in") == "query":
					query_params.append({
						"key": param.get("name"),
						"value": str(param.get("example") or ""),
						"disabled": not param.get("required", False),
						"description": param.get("description", ""),
					})

			req_item = {
				"name": op.get("summary") or f"{verb.upper()} {path}",
				"request": {
					"method": verb.upper(),
					"header": headers,
					"url": {
						"raw": "{{baseUrl}}" + path,
						"host": ["{{baseUrl}}"],
						"path": [p for p in path.strip("/").split("/") if p],
						"query": query_params,
					},
					"description": op.get("description") or op.get("summary") or "",
				},
			}
			folders[tag]["item"].append(req_item)

	collection["item"] = list(folders.values())
	return collection


def openapi_spec(openapi_version: str | None = None) -> dict:
	"""Cached per site; the server URL is filled in per call (the same site answers on more than one host)."""
	spec = frappe.cache.get_value(OPENAPI_CACHE_KEY)
	if not spec:
		spec = build_openapi_spec()
		frappe.cache.set_value(OPENAPI_CACHE_KEY, spec, expires_in_sec=OPENAPI_CACHE_SECONDS)
	spec = _visible_enums(copy.copy(spec))
	spec["servers"] = [{"url": frappe.utils.get_url()}]
	if openapi_version in ("3.0", "3.0.3"):
		return to_openapi_3_0(spec)
	return spec


def _visible_enums(spec: dict) -> dict:
	"""Pipelines and stages in the enums only as far as the caller sees them (a key: its pipelines), from the cached
	base, with two permission-checked queries. Field names stay company-wide: pipelines_read shows the field
	structure of the company (documented)."""
	from crm.permissions.pipeline import is_admin

	if is_admin():
		return spec
	if getattr(frappe.session, "user", None) == "Guest":
		schemas = dict(spec["components"]["schemas"])
		card = dict(schemas["Card"])
		properties = dict(card["properties"])
		canonical_enums = {
			"pipeline": (["vendas", "credito"], {"vendas": "Vendas", "credito": "Crédito"}),
			"status": (
				["novo", "qualificacao", "proposta", "ganho", "perdido"],
				{
					"novo": "Novo",
					"qualificacao": "Qualificação",
					"proposta": "Proposta",
					"ganho": "Ganho",
					"perdido": "Perdido",
				},
			),
		}
		for field, (names, labels) in canonical_enums.items():
			prop = properties.get(field)
			if prop and "enum" in prop:
				prop = dict(prop)
				prop["enum"] = names
				prop["x-enum-labels"] = labels
				properties[field] = prop
		card["properties"] = properties
		schemas["Card"] = card
		spec["components"] = {**spec["components"], "schemas": schemas}
		return spec
	pipelines = set(frappe.get_list("CRM Pipeline", pluck="name"))
	stages = set(frappe.get_all("CRM Deal Status", {"pipeline": ("in", list(pipelines) or [""])}, pluck="name"))
	schemas = dict(spec["components"]["schemas"])
	card = dict(schemas["Card"])
	properties = dict(card["properties"])
	for field, visible in (("pipeline", pipelines), ("status", stages)):
		prop = properties.get(field)
		if prop and "enum" in prop:
			prop = dict(prop)
			prop["enum"] = [v for v in prop["enum"] if v in visible]
			prop["x-enum-labels"] = {k: v for k, v in (prop.get("x-enum-labels") or {}).items() if k in visible}
			properties[field] = prop
	card["properties"] = properties
	schemas["Card"] = card
	spec["components"] = {**spec["components"], "schemas": schemas}
	return spec


def clear_openapi_cache(doc=None, method=None):
	"""doc_events of Custom Field, Property Setter, CRM Pipeline and CRM Deal Status: the spec is built again on the
	next call. Fields of other doctypes leave the cache alone."""
	is_field_change = doc is not None and doc.doctype in ("Custom Field", "Property Setter")
	if is_field_change and (doc.get("dt") or doc.get("doc_type")) not in PUBLIC_DOCTYPES:
		return
	frappe.cache.delete_value(OPENAPI_CACHE_KEY)
