"""API pública exemplar (credtech#375, ADR 0007 seção 11): campos da empresa (`ch_*`) na leitura, escrita, listagem por
cursor, lote, webhook e OpenAPI; corpo versionado e assinado dos webhooks; campos e formulários pela chave
(fields_admin); Pasta Digital pela API.
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_api_public`."""

import hashlib
import hmac
import io
import json
from unittest.mock import MagicMock, patch

import frappe
import frappe.api.v1 as rest
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import FileStorage, Headers

from cred_hub import api_keys, api_schema, bulk, webhooks
from cred_hub.credit_check import api as credit_api
from cred_hub.credit_check import onboarding

DEAL = "CRM Deal"
CONTACT = "Contact"
# Campos da empresa criados uma vez para a classe (criar campo é DDL e grava sozinho no banco)
DEAL_TEXT = ("API publica teste", "Data", None, DEAL)
DEAL_SELECT = ("API tipo teste", "Select", "Alfa\nBeta", DEAL)
CONTACT_TEXT = ("API contato teste", "Data", None, CONTACT)
FIELDS_ADMIN_ROUTES = (
	"crm.api.forms.create_field",
	"crm.api.forms.update_field",
	"crm.api.forms.disable_field",
	"crm.api.forms.save_form_config",
	"crm.api.pipeline.create_field",
	"crm.api.pipeline.update_field",
	"crm.api.pipeline.disable_field",
)
ALLOWED_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _catalog():
	from crm.fcrm.doctype.crm_pipeline import fields as catalog

	return catalog


def _ensure_field(label, fieldtype, options, doctype) -> str:
	catalog = _catalog()
	fieldname = catalog.fieldname_base(label)
	if not frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
		catalog.create_field(label, fieldtype, options, doctype)
	return fieldname


def _pdf() -> bytes:
	from pypdf import PdfWriter

	writer, buffer = PdfWriter(), io.BytesIO()
	writer.add_blank_page(width=72, height=72)
	writer.write(buffer)
	return buffer.getvalue()


def _authenticate(token, path, method="GET", form=None):
	"""Roda o auth_hook como numa chamada real; depois dele a sessão é a do usuário técnico."""
	request = MagicMock(path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}))
	frappe.set_user("Guest")
	frappe.local.credhub_api_key = None
	with (
		patch.object(frappe, "request", request),
		patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
		patch.dict(frappe.local.form_dict, form or {}, clear=True),
	):
		frappe.local.request_ip = "203.0.113.9"
		api_keys.authenticate()
	return frappe.session.user


class TestApiPublic(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.deal_field = _ensure_field(*DEAL_TEXT)
		cls.deal_select = _ensure_field(*DEAL_SELECT)
		cls.contact_field = _ensure_field(*CONTACT_TEXT)
		frappe.db.commit()  # nosemgrep: custom fields are DDL and must persist for the class

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		for doctype, fieldname in ((DEAL, cls.deal_field), (DEAL, cls.deal_select), (CONTACT, cls.contact_field)):
			if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
				_catalog().delete_field(fieldname, doctype)
		frappe.db.commit()  # nosemgrep: persist cleanup of committed custom fields
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		webhooks.clear_active_cache()
		api_schema.clear_openapi_cache()
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{
				"pipeline_name": "API Publica",
				"stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}],
				"fields": [self.deal_field, self.deal_select],
			}
		)["name"]
		self.other = save_pipeline({"pipeline_name": "API Outro", "stages": [{"deal_status": "Novo"}]})["name"]
		suffix = frappe.generate_hash(length=6)
		self.org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"Org API {suffix}"}).insert(
			ignore_permissions=True
		)
		self.contact = frappe.get_doc(
			{
				"doctype": CONTACT,
				"first_name": "Ana",
				"last_name": f"API {suffix}",
				"company_name": self.org.name,
				self.contact_field: "valor do contato",
			}
		).insert(ignore_permissions=True)
		self.deal = frappe.get_doc(
			{
				"doctype": DEAL,
				"pipeline": self.pipeline,
				"organization": self.org.name,
				self.deal_field: "valor do card",
				self.deal_select: "Alfa",
				"contacts": [{"contact": self.contact.name, "is_primary": 1}],
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()
		webhooks.clear_active_cache()
		api_schema.clear_openapi_cache()

	# ---------- Ajudantes ----------

	def create(self, scopes, pipelines=None):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key("Integração", list(scopes), pipelines or [self.pipeline])

	def call(self, token, path, method="GET", form=None):
		return _authenticate(token, path, method, form)

	def webhook(self, **values):
		doc = {
			"doctype": webhooks.WEBHOOK_DOCTYPE,
			"label": "Integração ERP",
			"webhook_url": "https://hooks.example.com/hubtech",
			"secret": "segredo-de-teste",
			"status": "Active",
			"deal_created": 0,
			"deal_stage_changed": 0,
			"deal_won": 0,
			"deal_lost": 0,
			"checklist_item_rejected": 0,
			"credit_limit_decided": 0,
			**values,
		}
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	# ---------- Campos da empresa em toda a API ----------

	def test_custom_fields_in_rest_read_and_write(self):
		token = self.create(("cards_read", "cards_write"))["token"]
		self.call(token, f"/api/resource/{DEAL}/{self.deal.name}")
		self.assertEqual(rest.read_doc(DEAL, self.deal.name).get(self.deal_field), "valor do card")

		self.call(token, f"/api/resource/{DEAL}", "POST")
		data = {"pipeline": self.pipeline, "organization": self.org.name, self.deal_field: "criado pela API"}
		with patch.dict(frappe.local.form_dict, {"data": frappe.as_json(data)}, clear=True):
			created = rest.create_doc(DEAL)
		self.assertEqual(frappe.db.get_value(DEAL, created.name, self.deal_field), "criado pela API")

		self.call(token, f"/api/resource/{DEAL}/{created.name}", "PUT")
		with patch.dict(frappe.local.form_dict, {"data": frappe.as_json({self.deal_select: "Beta"})}, clear=True):
			rest.update_doc(DEAL, created.name)
		self.assertEqual(frappe.db.get_value(DEAL, created.name, self.deal_select), "Beta")

	def test_custom_fields_in_cursor_list_and_batch(self):
		token = self.create(("cards_read", "cards_write"))["token"]
		self.call(token, "/api/method/cred_hub.api_keys.list_records_cursor", form={"doctype": DEAL})
		page = api_keys.list_records_cursor(DEAL, fields=["name", self.deal_field], limit=200)
		row = next(r for r in page["data"] if r["name"] == self.deal.name)
		self.assertEqual(row[self.deal_field], "valor do card")

		records = [
			{"name": self.deal.name, self.deal_field: "atualizado em lote"},
			{"pipeline": self.pipeline, "organization": self.org.name, self.deal_field: "novo em lote"},
		]
		self.call(token, "/api/method/cred_hub.bulk.bulk_upsert", "POST", form={"doctype": DEAL, "records": "[]"})
		result = bulk.bulk_upsert(DEAL, records)
		self.assertEqual(result["success"], 2, result)
		self.assertEqual(frappe.db.get_value(DEAL, self.deal.name, self.deal_field), "atualizado em lote")
		self.assertEqual(frappe.db.get_value(DEAL, result["results"][1]["name"], self.deal_field), "novo em lote")

	# ---------- Webhooks ----------

	def test_payload_policy_denies_sensitive_and_internal_fields(self):
		def df(fieldname, fieldtype="Data", hidden=0):
			return frappe._dict(fieldname=fieldname, fieldtype=fieldtype, hidden=hidden)

		self.assertTrue(api_schema.is_public(df("ch_renda"), DEAL))
		self.assertTrue(api_schema.is_public(df("full_name", hidden=1), CONTACT))
		for field in (
			df("ch_senha", "Password"),
			df("ch_api_token"),
			df("ch_client_secret"),
			df("naming_series", "Select"),
			df("erp_run_id"),
			df("products", "Table"),
			df("ch_desativado", hidden=1),
			df("details_section", "Section Break"),
		):
			with self.subTest(field=field.fieldname):
				self.assertFalse(api_schema.is_public(field, DEAL))

	def test_deal_payload_snapshot(self):
		"""Formato estável do corpo (versão 1): envelope, registro completo com campos da empresa e `related`."""
		data = api_schema.record(self.deal)
		related = api_schema.related(self.deal)
		body = webhooks.build_body("deal.created", data, "wh_fixo", 1, related=related, doctype=DEAL)
		body["timestamp"] = "<timestamp>"
		self.assertEqual(
			list(body), ["event", "version", "delivery_id", "attempt", "timestamp", "doctype", "data", "related"]
		)
		self.assertEqual(
			{k: body[k] for k in ("event", "version", "delivery_id", "attempt", "timestamp", "doctype")},
			{
				"event": "deal.created",
				"version": 1,
				"delivery_id": "wh_fixo",
				"attempt": 1,
				"timestamp": "<timestamp>",
				"doctype": DEAL,
			},
		)
		# o registro inteiro pela política, nem mais nem menos
		expected = [f for f in api_schema.public_fieldnames(DEAL) if f not in api_schema.WEBHOOK_OPT_IN_FIELDS]
		self.assertEqual(list(body["data"]), expected)
		self.assertEqual(body["data"][self.deal_field], "valor do card")
		self.assertEqual(body["data"][self.deal_select], "Alfa")
		for key in ("name", "pipeline", "status", "organization", "organization_name", "annual_revenue", "deal_owner"):
			self.assertIn(key, body["data"])  # as chaves da versão anterior continuam
		for key in ("naming_series", "erp_run_id", "contacts", "products", "probability", "email_company_cnpj"):
			self.assertNotIn(key, body["data"])
		org_summary = {k: v for k, v in body["related"]["organization"].items() if v is not None}
		self.assertEqual(org_summary["name"], self.org.name)
		self.assertEqual(org_summary["organization_name"], self.org.organization_name)
		self.assertTrue(set(body["related"]["organization"]) <= set(api_schema.ORGANIZATION_SUMMARY))
		self.assertIsNone(body["related"]["second_organization"])
		[contact] = body["related"]["contacts"]
		first_name = frappe.db.get_value(CONTACT, self.contact.name, "first_name")
		self.assertEqual((contact["name"], contact["first_name"], contact["is_primary"]), (self.contact.name, first_name, 1))
		self.assertTrue(set(contact) <= {*api_schema.CONTACT_SUMMARY, "is_primary"})
		json.loads(frappe.as_json(body))  # serializa

	def test_credit_fields_only_when_the_webhook_lists_them(self):
		frappe.db.set_value(DEAL, self.deal.name, "credit_verdict", "Warning")
		self.deal.reload()
		data = api_schema.record(self.deal)
		body = webhooks.build_body("deal.created", data, "wh", 1, related={}, doctype=DEAL)
		self.assertNotIn("credit_verdict", body["data"])
		self.assertNotIn("credit_report", body["data"])
		body = webhooks.build_body("deal.created", data, "wh", 1, related={}, doctype=DEAL, fields=["credit_verdict"])
		self.assertEqual(body["data"]["credit_verdict"], "Warning")
		with patch.object(api_schema, "CREDIT_FIELDS_IN_WEBHOOKS_BY_DEFAULT", True):  # the owner's switch
			body = webhooks.build_body("deal.created", data, "wh", 1, related={}, doctype=DEAL)
		self.assertIn("credit_verdict", body["data"])

	def test_contact_payload_has_custom_field_and_organization(self):
		body = webhooks.build_body(
			"contact.updated",
			api_schema.record(self.contact),
			"wh_x",
			1,
			related=api_schema.related(self.contact),
			doctype=CONTACT,
		)
		self.assertEqual(body["data"][self.contact_field], "valor do contato")
		for key in ("first_name", "last_name", "email_id", "mobile_no", "phone", "company_name", "full_name"):
			self.assertIn(key, body["data"])
		for key in ("user", "google_contacts_id", "sync_with_google_contacts", "email_ids", "phone_nos"):
			self.assertNotIn(key, body["data"])
		self.assertEqual(body["related"]["organization"]["name"], self.org.name)

	def test_record_events_reach_only_subscribers_and_build_payload_once(self):
		self.webhook(deal_created=1, pipelines=[{"pipeline": self.pipeline}])
		with patch.object(frappe, "enqueue") as enqueue:
			frappe.get_doc({"doctype": DEAL, "pipeline": self.other, "organization": self.org.name}).insert(
				ignore_permissions=True
			)
			enqueue.assert_not_called()  # webhook limitado a outro funil
			deal = frappe.get_doc(
				{"doctype": DEAL, "pipeline": self.pipeline, "organization": self.org.name, self.deal_field: "x"}
			).insert(ignore_permissions=True)
		[call] = enqueue.call_args_list
		kwargs = call.kwargs
		self.assertEqual((kwargs["event_name"], kwargs["doctype"], kwargs["reference_name"]), ("deal.created", DEAL, deal.name))
		self.assertEqual(kwargs["payload_data"][self.deal_field], "x")
		self.assertIn("organization", kwargs["related"])
		self.assertTrue(kwargs["enqueue_after_commit"])

		webhooks.clear_active_cache()
		frappe.db.delete(webhooks.WEBHOOK_DOCTYPE)
		with patch.object(api_schema, "record") as record, patch.object(frappe, "enqueue") as enqueue:
			self.contact.save(ignore_permissions=True)
			record.assert_not_called()  # sem webhook, nada é montado
			enqueue.assert_not_called()

	def send(self, webhook, status_code=200, **kwargs):
		"""send_webhook com a rede trocada por um dublê; devolve (headers, corpo cru)."""
		from cred_hub.email_company import safe_http

		session = MagicMock()
		session.post.return_value = MagicMock(status_code=status_code, ok=status_code < 400, text="ok")
		with (
			patch.object(safe_http, "resolve_public"),
			patch.object(safe_http, "_session", return_value=session),
		):
			webhooks.send_webhook(webhook.name, **kwargs)
		_args, call = session.post.call_args
		return call["headers"], call["data"]

	def test_signatures_v1_and_v2_with_signed_timestamp(self):
		wh = self.webhook(deal_created=1)
		headers, raw = self.send(
			wh,
			event_name="deal.created",
			payload_data=api_schema.record(self.deal),
			related=api_schema.related(self.deal),
			doctype=DEAL,
		)
		secret = b"segredo-de-teste"
		v1 = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
		timestamp = headers["X-HubTech-Timestamp"]
		v2 = hmac.new(secret, f"{timestamp}.{raw}".encode(), hashlib.sha256).hexdigest()
		self.assertEqual(headers["X-HubTech-Signature"], f"sha256={v1}")
		self.assertEqual(headers["X-HubTech-Signature-V2"], f"sha256={v2}")
		self.assertEqual(headers["X-HubTech-Webhook-Version"], "1")
		body = json.loads(raw)
		self.assertEqual((body["version"], body["delivery_id"]), (1, headers["X-HubTech-Delivery"]))
		self.assertEqual(body["data"][self.deal_field], "valor do card")
		log = frappe.get_last_doc(webhooks.LOG_DOCTYPE, filters={"webhook": wh.name})
		self.assertNotIn("segredo-de-teste", log.request_payload)

	def test_webhook_field_list_narrows_payload_and_is_validated(self):
		wh = self.webhook(deal_created=1, payload_fields=f"status, {self.deal_field}\norganization")
		self.assertEqual(wh.payload_fields, f"status\n{self.deal_field}\norganization")
		_headers, raw = self.send(
			wh,
			event_name="deal.created",
			payload_data=api_schema.record(self.deal),
			related=api_schema.related(self.deal),
			doctype=DEAL,
		)
		body = json.loads(raw)
		# "organization" traz o vínculo do card e o resumo da organização
		self.assertEqual(set(body["data"]), {"name", "status", self.deal_field, "organization"})
		self.assertEqual(set(body["related"]), {"organization"})
		for bad in ("password", "naming_series", "nao_existe"):
			with self.subTest(field=bad), self.assertRaises(frappe.ValidationError):
				self.webhook(payload_fields=bad)

	def test_retry_keeps_delivery_id(self):
		wh = self.webhook(deal_created=1)
		with patch.dict(frappe.flags, {"in_test": False}), patch.object(frappe, "enqueue") as enqueue:
			headers, _raw = self.send(wh, status_code=503, event_name="deal.created", payload_data={"name": "X"})
		retry = enqueue.call_args.kwargs
		self.assertEqual((retry["attempt"], retry["delivery_id"]), (2, headers["X-HubTech-Delivery"]))

	# ---------- OpenAPI por empresa ----------

	def assert_valid_openapi(self, spec):
		self.assertEqual(spec["openapi"], "3.1.0")
		self.assertTrue(spec["info"]["title"] and spec["info"]["version"])
		self.assertTrue(spec["servers"][0]["url"].startswith("http"))
		schemas = spec["components"]["schemas"]
		refs = []

		def walk(node):
			if isinstance(node, dict):
				if "$ref" in node:
					refs.append(node["$ref"])
				types = node.get("type")
				if isinstance(types, str | list):  # a property called "type" is a dict and is walked below
					for t in types if isinstance(types, list) else [types]:
						self.assertIn(t, ALLOWED_TYPES, node)
				for value in node.values():
					walk(value)
			elif isinstance(node, list):
				for value in node:
					walk(value)

		walk({k: v for k, v in spec.items() if k != "components"})
		walk(schemas)
		for ref in refs:
			self.assertTrue(ref.startswith("#/components/schemas/"), ref)
			self.assertIn(ref.rsplit("/", 1)[1], schemas, ref)
		self.assertTrue(spec["paths"])
		for path, item in spec["paths"].items():
			self.assertTrue(path.startswith("/api/"), path)
			operations = {k: v for k, v in item.items() if k in HTTP_VERBS}
			self.assertTrue(operations, path)
			for verb, operation in operations.items():
				self.assertTrue(operation.get("responses"), f"{verb} {path}")
				self.assertTrue(operation.get("summary"), f"{verb} {path}")
			if "{name}" in path:
				self.assertEqual(item["parameters"][0]["name"], "name")
		json.loads(json.dumps(spec))

	def test_openapi_is_per_company_and_valid(self):
		spec = api_schema.openapi_spec()
		self.assert_valid_openapi(spec)
		card = spec["components"]["schemas"]["Card"]["properties"]
		self.assertEqual(card[self.deal_field]["type"], ["string", "null"])
		self.assertTrue(card[self.deal_field]["x-custom"])
		self.assertEqual(card[self.deal_select]["enum"], ["Alfa", "Beta", "", None])
		self.assertEqual(card["organization"]["x-link"], "CRM Organization")
		self.assertIn(self.pipeline, card["pipeline"]["enum"])
		self.assertEqual(card["pipeline"]["x-enum-labels"][self.pipeline], "API Publica")
		stages = frappe.get_all("CRM Deal Status", {"pipeline": self.pipeline}, pluck="name")
		self.assertTrue(set(stages) <= set(card["status"]["enum"]))
		self.assertNotIn("naming_series", card)
		contact = spec["components"]["schemas"]["Contact"]["properties"]
		self.assertTrue(contact[self.contact_field]["x-custom"])
		self.assertNotIn("user", contact)
		for path in (
			"/api/resource/CRM Deal",
			"/api/resource/CRM Deal/{name}",
			"/api/method/crm.api.forms.create_field",
			"/api/method/cred_hub.credit_check.api.upload_checklist_document",
			"/api/method/cred_hub.credit_check.api.get_document_checklist",
		):
			self.assertIn(path, spec["paths"])

	def test_openapi_is_cached_and_rebuilt_when_fields_or_pipelines_change(self):
		with patch.object(api_schema, "build_openapi_spec", wraps=api_schema.build_openapi_spec) as build:
			api_schema.openapi_spec()
			api_schema.openapi_spec()
			self.assertEqual(build.call_count, 1)
			from crm.api.pipeline import save_pipeline

			save_pipeline({"name": self.pipeline, "pipeline_name": "API Publica renomeado"})
			spec = api_schema.openapi_spec()
			self.assertEqual(build.call_count, 2)
			self.assertEqual(
				spec["components"]["schemas"]["Card"]["properties"]["pipeline"]["x-enum-labels"][self.pipeline],
				"API Publica renomeado",
			)
			_catalog().update_field(self.deal_field, label="API publica renomeado")
			spec = api_schema.openapi_spec()
			self.assertEqual(build.call_count, 3)
			self.assertEqual(
				spec["components"]["schemas"]["Card"]["properties"][self.deal_field]["title"], "API publica renomeado"
			)
			# campo de outro doctype não derruba o cache
			api_schema.clear_openapi_cache(frappe._dict(doctype="Custom Field", dt="ToDo"))
			api_schema.openapi_spec()
			self.assertEqual(build.call_count, 3)
		_catalog().update_field(self.deal_field, label=DEAL_TEXT[0])

	def test_openapi_needs_a_key_or_a_signed_in_user(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(api_keys.get_openapi_spec)
		token = self.create(("pipelines_read",))["token"]
		self.call(token, "/api/method/cred_hub.api_keys.get_openapi_spec")
		self.assertIn("paths", api_keys.get_openapi_spec())

	def test_openapi_enums_show_only_the_key_pipelines(self):
		api_schema.openapi_spec()  # base cached with every pipeline
		token = self.create(("pipelines_read",))["token"]
		self.call(token, "/api/method/cred_hub.api_keys.get_openapi_spec")
		card = api_keys.get_openapi_spec()["components"]["schemas"]["Card"]["properties"]
		self.assertIn(self.pipeline, card["pipeline"]["enum"])
		self.assertNotIn(self.other, card["pipeline"]["enum"])
		self.assertNotIn(self.other, card["pipeline"]["x-enum-labels"])
		other_stages = set(frappe.get_all("CRM Deal Status", {"pipeline": self.other}, pluck="name"))
		self.assertFalse(other_stages & set(card["status"]["enum"]))
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		admin = api_schema.openapi_spec()["components"]["schemas"]["Card"]["properties"]
		self.assertIn(self.other, admin["pipeline"]["enum"])  # the cached base was not changed

	# ---------- Campos e formulários pela chave (fields_admin) ----------

	def test_fields_admin_routes_need_the_scope(self):
		token = self.create(("cards_read", "cards_write", "pipelines_read"))["token"]
		for route in FIELDS_ADMIN_ROUTES:
			with self.subTest(route=route), self.assertRaises(frappe.PermissionError):
				self.call(token, f"/api/method/{route}", "POST")
		admin = self.create(("cards_read", "fields_admin"))["token"]
		for route in FIELDS_ADMIN_ROUTES:
			with self.subTest(route=route):
				self.call(admin, f"/api/method/{route}", "POST")
				self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_fields_admin_key_cannot_create_forbidden_field_types(self):
		from crm.api import forms

		self.call(self.create(("cards_read", "fields_admin"))["token"], "/api/method/crm.api.forms.create_field", "POST")
		for args in (("Tabela", "Table", None), ("Usuario", "Link", "User"), ("Senha", "Password", None), ("Lista", "Select", "")):
			with self.subTest(args=args), self.assertRaises(frappe.ValidationError):
				forms.create_field(*args)
			self.assertFalse(frappe.db.exists("Custom Field", {"dt": DEAL, "label": args[0]}))

	def test_fields_admin_hook_only_for_keys_with_the_scope(self):
		self.call(self.create(("cards_read", "cards_write"))["token"], "/api/resource/CRM Deal")
		self.assertFalse(api_keys.is_fields_admin_call())
		from crm.api import forms

		with self.assertRaises(frappe.PermissionError):
			forms.update_field(self.deal_field, label="Sem escopo")
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		self.assertFalse(api_keys.is_fields_admin_call())  # sessão comum: a decisão é do is_admin do crm

	def test_fields_admin_key_updates_disables_fields_and_saves_forms_with_audit(self):
		from crm.api import forms

		created = self.create(("cards_read", "fields_admin"))
		token = created["token"]
		route = "/api/method/crm.api.forms.update_field"
		form = {"fieldname": self.deal_field, "label": "Renomeado pela chave", "doctype": DEAL}
		self.call(token, route, "POST", form)
		self.assertTrue(api_keys.is_fields_admin_call())
		self.assertEqual(forms.update_field(**form)["label"], "Renomeado pela chave")
		self.assertTrue(forms.disable_field(self.deal_select, DEAL)["disabled"])

		# formulário do funil da chave sim; de outro funil, não (o crm confere o Dono)
		rows = [{"fieldname": self.deal_field, "visible": 1, "required": 0}]
		schema = forms.save_form_config(DEAL, rows, pipeline=self.pipeline)
		self.assertIn(self.deal_field, [f["fieldname"] for f in schema["fields"]])
		with self.assertRaises(frappe.PermissionError):
			forms.save_form_config(DEAL, rows, pipeline=self.other)

		# quem mudou o quê: a chamada fica no log com os argumentos
		request = MagicMock(path=route, method="POST")
		with patch.object(frappe, "request", request), patch.dict(frappe.local.form_dict, form, clear=True):
			api_keys.after_request(MagicMock(status_code=200, headers={}))
		log = frappe.get_last_doc(api_keys.LOG, filters={"api_key": created["name"]})
		self.assertEqual(json.loads(log.details)["fieldname"], self.deal_field)
		self.assertEqual(log.owner, frappe.session.user)

		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		_catalog().enable_field(self.deal_select, DEAL)
		_catalog().update_field(self.deal_field, label=DEAL_TEXT[0])

	# ---------- Pasta Digital pela API ----------

	def test_checklist_read_by_key_with_status_and_reason(self):
		onboarding.update_checklist_item(self.org.name, "contrato_social", status="rejected", notes="Ilegível")
		token = self.create(("credit_read",))["token"]
		self.call(token, "/api/method/cred_hub.credit_check.api.get_document_checklist", form={"deal": self.deal.name})
		result = credit_api.get_document_checklist(deal=self.deal.name)
		self.assertEqual((result["organization"], result["deal"]), (self.org.name, self.deal.name))
		items = {item["key"]: item for item in result["items"]}
		self.assertEqual((items["contrato_social"]["status"], items["contrato_social"]["reason"]), ("rejected", "Ilegível"))
		other = next(item for key, item in items.items() if key != "contrato_social")
		self.assertEqual((other["status"], other["reason"]), ("pending", None))
		self.assertEqual(set(other), {*credit_api.CHECKLIST_ITEM_FIELDS, "reason"})
		self.assertIn("required_total", result["summary"])

	def test_checklist_routes_need_their_scopes(self):
		read = "/api/method/cred_hub.credit_check.api.get_document_checklist"
		upload = "/api/method/cred_hub.credit_check.api.upload_checklist_document"
		cases = (
			(("cards_read",), read, "GET"),
			(("cards_read", "files_write"), upload, "POST"),
			(("credit_read",), upload, "POST"),
			(("credit_read", "credit_write"), upload, "POST"),
			(("credit_read", "files_write"), upload, "GET"),
		)
		for scopes, path, verb in cases:
			with self.subTest(scopes=scopes, path=path, verb=verb), self.assertRaises(frappe.PermissionError):
				self.call(self.create(scopes)["token"], path, verb)
		self.call(self.create(("credit_read", "files_write"))["token"], upload, "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])

	def upload(self, content=None, filename="contrato.pdf", item_key="contrato_social", **kwargs):
		content = _pdf() if content is None else content
		upload = FileStorage(io.BytesIO(content), filename=filename) if content is not False else None
		with (
			patch.object(credit_api, "_uploaded_file", return_value=upload),
			patch("cred_hub.credit_check.committee.extract_document_insights", return_value=None),
		):
			return credit_api.upload_checklist_document(item_key=item_key, **kwargs)

	def test_upload_into_checklist_item_private_file_and_webhook(self):
		self.webhook(checklist_item_attached=1)
		token = self.create(("credit_read", "files_write"))["token"]
		self.call(token, "/api/method/cred_hub.credit_check.api.upload_checklist_document", "POST")
		with patch.object(frappe, "enqueue") as enqueue:
			item = self.upload(deal=self.deal.name, valid_until="2030-01-31")
		self.assertEqual((item["key"], item["status"], item["file_name"]), ("contrato_social", "attached", "contrato.pdf"))
		self.assertEqual(item["valid_until"], "2030-01-31")
		self.assertTrue(item["file_url"].startswith("/private/files/"))
		file_doc = frappe.get_doc("File", {"file_url": item["file_url"]})
		self.assertEqual((file_doc.is_private, file_doc.attached_to_doctype, file_doc.attached_to_name), (1, DEAL, self.deal.name))
		[call] = [c for c in enqueue.call_args_list if c.kwargs.get("event_name") == "checklist.item_attached"]
		payload = call.kwargs["payload_data"]
		self.assertEqual((payload["item_key"], payload["source"], payload["deal"]), ("contrato_social", "api", self.deal.name))

	def test_upload_follows_the_external_link_rules(self):
		token = self.create(("credit_read", "files_write"))["token"]
		self.call(token, "/api/method/cred_hub.credit_check.api.upload_checklist_document", "POST")
		cases = (
			{"filename": "virus.exe"},
			{"content": b""},
			{"content": False},
			{"content": b"%PDF-1.4 quebrado"},
			{"item_key": "nao_existe"},
		)
		for case in cases:
			with self.subTest(case=case), self.assertRaises(frappe.ValidationError):
				self.upload(organization=self.org.name, **case)
		with patch.object(onboarding, "MAX_UPLOAD_BYTES", 5), self.assertRaises(frappe.ValidationError):
			self.upload(organization=self.org.name)
		frappe.set_user("Administrator")
		onboarding.update_checklist_item(self.org.name, "contrato_social", status="verified")
		self.call(token, "/api/method/cred_hub.credit_check.api.upload_checklist_document", "POST")
		with self.assertRaises(frappe.ValidationError):  # conferido fica travado, como no link externo
			self.upload(organization=self.org.name)

	def test_upload_respects_card_visibility_and_organization(self):
		other_org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"Outra {frappe.generate_hash(length=6)}"}).insert(
			ignore_permissions=True
		)
		hidden = frappe.get_doc({"doctype": DEAL, "pipeline": self.other, "organization": other_org.name}).insert(
			ignore_permissions=True
		)
		token = self.create(("credit_read", "files_write"))["token"]
		self.call(token, "/api/method/cred_hub.credit_check.api.upload_checklist_document", "POST")
		with self.assertRaises(frappe.PermissionError):  # card de um funil que a chave não tem
			self.upload(deal=hidden.name)
		with self.assertRaises(frappe.ValidationError):  # card de outra organização
			self.upload(deal=self.deal.name, organization=other_org.name)
		with self.assertRaises(frappe.PermissionError):  # organização só com card no outro funil
			self.upload(organization=other_org.name)
		with self.assertRaises(frappe.PermissionError):  # ler o checklist também
			credit_api.get_document_checklist(organization=other_org.name)
		self.assertEqual(self.upload(organization=self.org.name)["status"], "attached")  # tem card no funil da chave


class TestFieldsAdminKeyCreatesField(IntegrationTestCase):
	"""Criar campo é DDL e grava sozinho no banco: sem rollback aqui, o teste desfaz à mão a chave, os funis e o campo."""

	def test_fields_admin_key_creates_a_field_in_its_pipeline(self):
		from crm.api import forms
		from crm.api.pipeline import get_pipeline_config, save_pipeline

		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		suffix = frappe.generate_hash(length=4)
		pipeline = save_pipeline({"pipeline_name": f"API DDL {suffix}", "stages": [{"deal_status": "Novo"}]})["name"]
		other = save_pipeline({"pipeline_name": f"API DDL outro {suffix}", "stages": [{"deal_status": "Novo"}]})["name"]
		created = api_keys.create_api_key("DDL", ["cards_read", "fields_admin"], [pipeline])
		frappe.db.commit()  # nosemgrep: the DDL below needs a clean transaction
		label = f"Criado pela chave {suffix}"
		fieldname = _catalog().fieldname_base(label)
		try:
			_authenticate(created["token"], "/api/method/crm.api.forms.create_field", "POST")
			with self.assertRaises(frappe.PermissionError):  # funil que a chave não tem: nada é criado
				forms.create_field(label, "Data", placement=[{"pipeline": other, "stages": []}])
			self.assertFalse(frappe.db.exists("Custom Field", {"dt": DEAL, "fieldname": fieldname}))
			field = forms.create_field(label, "Data", placement=[{"pipeline": pipeline, "stages": []}])
			self.assertEqual(field["fieldname"], fieldname)
			self.assertIn(fieldname, [f["fieldname"] for f in get_pipeline_config(pipeline)["fields"]])
			self.assertEqual(
				frappe.db.get_value("Custom Field", {"dt": DEAL, "fieldname": fieldname}, "owner"), frappe.session.user
			)
		finally:
			frappe.set_user("Administrator")
			frappe.local.credhub_api_key = None
			frappe.db.rollback()
			api_keys.revoke_api_key(created["name"])
			technical_user = frappe.db.get_value(api_keys.KEY, created["name"], "technical_user")
			frappe.delete_doc(api_keys.KEY, created["name"], ignore_permissions=True, force=True)
			frappe.delete_doc("User", technical_user, ignore_permissions=True, force=True)
			for name in (pipeline, other):
				for stage in frappe.get_all("CRM Deal Status", {"pipeline": name}, pluck="name"):
					frappe.delete_doc("CRM Deal Status", stage, ignore_permissions=True, force=True)
				frappe.delete_doc("CRM Pipeline", name, ignore_permissions=True, force=True)
			frappe.db.commit()  # nosemgrep: undo the committed key and pipelines
			if frappe.db.exists("Custom Field", {"dt": DEAL, "fieldname": fieldname}):
				_catalog().delete_field(fieldname, DEAL)
			frappe.db.commit()  # nosemgrep: persist cleanup
