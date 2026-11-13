"""Testes de integração reais para Identidade Universal (external_id), Upsert Inteligente, Porta B de ERPs,
Templates Curados e Webhooks de Crédito/Cobrança (credtech#375).
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_api_external_id_and_push`."""

from datetime import date
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today
from werkzeug.datastructures import Headers

from cred_hub import api_keys, api_schema, sandbox, webhooks
from cred_hub.collection import importer
from cred_hub.erp import connectors
from cred_hub.erp import settings as erp_settings
from cred_hub.erp import sync
from cred_hub.erp.connectors.rest import GENERIC
from cred_hub.erp.fields import erp_custom_fields
from cred_hub.erp.push import register_result, settle_titles, upsert_titles
from cred_hub.erp.rest import RestConfig
from cred_hub.erp.rest_templates import CURATED_TEMPLATES, list_curated_templates
from cred_hub.integrations import SETTINGS as PROVIDER_SETTINGS

DEBTOR_CNPJ = "11222333000181"
CEDENTE_CNPJ = "45723174000110"
CONTACT_CPF = "11144477735"


def _authenticate(token: str, path: str, method: str = "POST", form: dict | None = None):
	request = MagicMock(path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}))
	frappe.set_user("Guest")
	frappe.local.credhub_api_key = None
	with (
		patch.object(frappe, "request", request),
		patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
		patch.dict(frappe.local.form_dict, form or {}, clear=True),
	):
		frappe.local.request_ip = "203.0.113.42"
		api_keys.authenticate()
	return frappe.session.user


class TestUniversalIdentityAndPush(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		self.patches = [patch("frappe.enqueue"), patch.object(frappe.db, "commit")]
		for p in self.patches:
			p.start()
		webhooks.clear_active_cache()
		api_schema.clear_openapi_cache()
		sandbox.clear_test_users_cache()

		from crm.api.pipeline import save_pipeline

		self.collection_pipeline = save_pipeline(
			{
				"pipeline_name": "Cobrança Push Teste",
				"stages": [
					{"deal_status": "Em Cobrança", "type": "Open"},
					{"deal_status": "Liquidado", "type": "Won"},
					{"deal_status": "Baixado", "type": "Lost"},
				],
			}
		)["name"]
		self.credit_pipeline = save_pipeline(
			{
				"pipeline_name": "Crédito Push Teste",
				"stages": [
					{"deal_status": "Análise", "type": "Open"},
					{"deal_status": "Pronto para Cadastro ERP", "type": "Ongoing"},
					{"deal_status": "Ativo", "type": "Won"},
				],
			}
		)["name"]
		self.credit_stages = frappe.get_all(
			"CRM Deal Status",
			{"pipeline": self.credit_pipeline},
			["name", "deal_status"],
		)
		self.register_stage = next(s.name for s in self.credit_stages if s.deal_status == "Pronto para Cadastro ERP")

		provider = frappe.get_single(PROVIDER_SETTINGS)
		provider.update(
			{
				"collection_pipeline": self.collection_pipeline,
				"collection_grouping": importer.BY_PAIR,
			}
		)
		provider.save()
		frappe.clear_document_cache(PROVIDER_SETTINGS, PROVIDER_SETTINGS)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		for p in self.patches:
			p.stop()
		frappe.db.rollback()
		webhooks.clear_active_cache()
		api_schema.clear_openapi_cache()
		sandbox.clear_test_users_cache()
		frappe.clear_document_cache(PROVIDER_SETTINGS, PROVIDER_SETTINGS)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def create_key(self, scopes, pipelines=None, is_test: int = 0):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key(
			f"Chave Push {'Test' if is_test else 'Prod'}",
			list(scopes),
			pipelines or [self.collection_pipeline, self.credit_pipeline],
			is_test=is_test,
		)

	def test_curated_templates_all_validate_against_rest_config_and_require_admin(self):
		fields = erp_custom_fields()
		for dt in ("CRM Organization", "Contact", "CRM Deal"):
			ext = next((f for f in fields[dt] if f.get("fieldname") == "external_id"), None)
			self.assertIsNotNone(ext, dt)
			self.assertEqual(ext["search_index"], 1)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			list_curated_templates()

		frappe.set_user("Administrator")
		listed = list_curated_templates()
		self.assertEqual(len(listed), len(CURATED_TEMPLATES))
		self.assertGreaterEqual(len(listed), 4)
		for item in listed:
			with self.subTest(template_id=item["id"]):
				cfg = RestConfig.from_template(item["template"], item["default_base_url"])
				self.assertEqual(cfg.secret_names(), item["secrets_required"])
				conn_cls = connectors.get(GENERIC).with_config(cfg)
				self.assertTrue(conn_cls.capabilities.overdue_titles)
				self.assertTrue(conn_cls.capabilities.settled_titles)

	def test_upsert_record_sanitizes_output_handles_contact_channels_and_isolates_sandbox(self):
		prod_token = self.create_key(("organizations_write", "contacts_write", "cards_write"))["token"]
		test_token = self.create_key(("organizations_write", "contacts_write", "cards_write"), is_test=1)["token"]
		suffix = frappe.generate_hash(length=6)
		ext_org = f"erp_org_{suffix}"
		ext_cont = f"erp_cont_{suffix}"

		# 1. Upsert de Organização real com alias tax_id -> não vaza campos internos
		_authenticate(prod_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "CRM Organization"})
		created_org = api_keys.upsert_record(
			"CRM Organization",
			{"external_id": ext_org, "tax_id": DEBTOR_CNPJ, "organization_name": f"Acme Prod {suffix}"},
		)
		self.assertEqual(created_org["action"], "created")
		self.assertEqual(created_org["external_id"], ext_org)
		self.assertNotIn("naming_series", created_org["data"])
		self.assertNotIn("credit_verdict", created_org["data"])

		# 2. Chave Sandbox com MESMO external_id e MESMO CNPJ cria registro [sandbox] isolado e atualiza sem duplicar
		_authenticate(test_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "CRM Organization"})
		sb_first = api_keys.upsert_record(
			"CRM Organization",
			{"external_id": ext_org, "cnpj": DEBTOR_CNPJ, "organization_name": f"Acme Sandbox {suffix}"},
		)
		self.assertEqual(sb_first["action"], "created")
		self.assertNotEqual(sb_first["name"], created_org["name"])

		_authenticate(test_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "CRM Organization"})
		sb_second = api_keys.upsert_record(
			"CRM Organization",
			{"external_id": ext_org, "website": "https://sandbox.acme.test"},
		)
		self.assertEqual(sb_second["action"], "updated")
		self.assertEqual(sb_second["name"], sb_first["name"])

		# 3. Chave real fazendo update no mesmo external_id atinge apenas o registro real (sem colisão com Sandbox)
		_authenticate(prod_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "CRM Organization"})
		prod_updated = api_keys.upsert_record(
			"CRM Organization",
			{"external_id": ext_org, "website": "https://prod.acme.com.br"},
			match_by="external_id",
		)
		self.assertEqual(prod_updated["action"], "updated")
		self.assertEqual(prod_updated["name"], created_org["name"])
		self.assertEqual(prod_updated["data"]["website"], "https://prod.acme.com.br")

		# 4. Contact: alias email/tax_id e sincronização real da tabela filha email_ids em updates subsequentes
		_authenticate(prod_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "Contact"})
		c1 = api_keys.upsert_record(
			"Contact",
			{"external_id": ext_cont, "first_name": "Carlos", "tax_id": CONTACT_CPF, "email": f"v1.{suffix}@acme.com.br"},
		)
		self.assertEqual(c1["action"], "created")
		self.assertEqual(c1["data"]["email_id"], f"v1.{suffix}@acme.com.br")

		_authenticate(prod_token, "/api/method/cred_hub.api_keys.upsert_record", "POST", {"doctype": "Contact"})
		c2 = api_keys.upsert_record(
			"Contact",
			{"external_id": ext_cont, "email_id": f"v2.{suffix}@acme.com.br"},
		)
		self.assertEqual(c2["action"], "updated")
		self.assertEqual(c2["name"], c1["name"])
		self.assertEqual(frappe.db.get_value("Contact", c1["name"], "email_id"), f"v2.{suffix}@acme.com.br")

	def test_porta_b_push_settles_with_exact_paid_date_and_blocks_sandbox_or_wrong_pipeline(self):
		# 1. Chave Sandbox é bloqueada de empurrar títulos para o funil real de Cobrança
		test_token = self.create_key(("cards_write",), is_test=1)["token"]
		_authenticate(test_token, "/api/method/cred_hub.erp.push.upsert_titles", "POST")
		with self.assertRaises(sandbox.SandboxViolation):
			upsert_titles(
				[{"erp_id": "SB-1", "sacado_document": DEBTOR_CNPJ, "due_date": "2026-10-10", "amount": 500}],
				erp_source="Asaas",
			)

		# 2. Chave sem acesso ao funil de Cobrança (restrita apenas ao funil de Crédito) recebe 403 PermissionError
		credit_only_token = self.create_key(("cards_write",), pipelines=[self.credit_pipeline])["token"]
		_authenticate(credit_only_token, "/api/method/cred_hub.erp.push.upsert_titles", "POST")
		with self.assertRaises(frappe.PermissionError):
			upsert_titles(
				[{"erp_id": "UNAUTH-1", "sacado_document": DEBTOR_CNPJ, "due_date": "2026-10-10", "amount": 500}],
				erp_source="Asaas",
			)

		# 3. Chave real autorizada no funil de Cobrança insere título vencido e liquida preservando paid_on retroativo
		coll_token = self.create_key(("cards_write",), pipelines=[self.collection_pipeline])["token"]
		erp_title_id = f"TIT-{frappe.generate_hash(length=6)}"
		due_str = str(add_days(today(), -10))
		paid_backdated = getdate(add_days(today(), -3))

		_authenticate(coll_token, "/api/method/cred_hub.erp.push.upsert_titles", "POST")
		pushed = upsert_titles(
			[
				{
					"erp_id": erp_title_id,
					"sacado_document": DEBTOR_CNPJ,
					"sacado_name": "Sacado Real Ltda",
					"cedente_document": CEDENTE_CNPJ,
					"cedente_name": "Cedente Real S/A",
					"due_date": due_str,
					"amount": 1250.00,
					"number": "NF-7788",
				}
			],
			erp_source="Asaas",
		)
		self.assertTrue(pushed["ok"])
		self.assertEqual(pushed["titles_new"], 1)
		self.assertEqual(pushed["cards_new"], 1)

		found = sync.find_row("Asaas", erp_title_id)
		self.assertIsNotNone(found)
		card_name, row_name = found

		_authenticate(coll_token, "/api/method/cred_hub.erp.push.settle_titles", "POST")
		settled = settle_titles(
			[
				{
					"erp_id": erp_title_id,
					"outcome": "paid",
					"paid_amount": 1250.00,
					"paid_on": str(paid_backdated),
					"note": "Pix recebido Asaas",
				}
			],
			erp_source="Asaas",
		)
		self.assertTrue(settled["ok"])
		self.assertEqual(settled["titles_closed"], 1)
		self.assertEqual(settled["cards_won"], 1)

		frappe.set_user("Administrator")
		deal = frappe.get_doc("CRM Deal", card_name)
		row = next(r for r in deal.titles if r.name == row_name)
		self.assertEqual(getdate(row.closed_on), paid_backdated)
		self.assertEqual(deal.outcome, "Won")

	def test_webhooks_cedente_ready_promise_agreement_and_register_result_scopes(self):
		frappe.set_user("Administrator")
		wh = frappe.get_doc(
			{
				"doctype": "Cred Hub Webhook",
				"label": "ERP Listener Completo",
				"webhook_url": "https://hooks.example.com/hubtech",
				"secret": "segredo-de-teste",
				"status": "Active",
				"deal_stage_changed": 1,
				"deal_updated": 1,
				"cedente_ready_to_register": 1,
				"title_promise_recorded": 1,
				"title_agreement_recorded": 1,
			}
		).insert(ignore_permissions=True)
		self.assertTrue(wh.name)
		webhooks.clear_active_cache()

		erp_cfg = frappe.get_single(erp_settings.SETTINGS)
		erp_cfg.update(
			{
				"erp_enabled": 0,
				"cedente_send_enabled": 1,
				"cedente_trigger": "Card enters a stage",
				"cedente_pipeline": self.credit_pipeline,
				"cedente_stage": self.register_stage,
			}
		)
		erp_cfg.save()
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

		suffix = frappe.generate_hash(length=6)
		org = frappe.get_doc(
			{
				"doctype": "CRM Organization",
				"organization_name": f"Cedente Novo {suffix}",
				"cnpj": CEDENTE_CNPJ,
			}
		).insert(ignore_permissions=True)
		credit_deal = frappe.get_doc(
			{
				"doctype": "CRM Deal",
				"pipeline": self.credit_pipeline,
				"organization": org.name,
				"deal_value": 50000,
			}
		).insert(ignore_permissions=True)

		# Move etapa para cedente_stage E altera deal_value simultaneamente -> ambos stage_changed, updated e cedente.ready_to_register!
		credit_deal.status = self.register_stage
		credit_deal.deal_value = 80000
		credit_deal.save()

		outbox_events = frappe.get_all(
			"Cred Hub Webhook Outbox",
			{"webhook": wh.name, "reference_name": credit_deal.name},
			pluck="event",
		)
		self.assertIn("deal.stage_changed", outbox_events)
		self.assertIn("deal.updated", outbox_events)
		self.assertIn("cedente.ready_to_register", outbox_events)

		# Promessa e acordo no card de cobrança disparam title.promise_recorded e title.agreement_recorded
		coll_token = self.create_key(("cards_write",), pipelines=[self.collection_pipeline])["token"]
		erp_tid = f"PROM-{suffix}"
		_authenticate(coll_token, "/api/method/cred_hub.erp.push.upsert_titles", "POST")
		upsert_titles(
			[
				{
					"erp_id": erp_tid,
					"sacado_document": DEBTOR_CNPJ,
					"sacado_name": "Sacado Acordo",
					"due_date": str(add_days(today(), -15)),
					"amount": 1000.00,
				}
			],
			erp_source="Bling",
		)
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		coll_card_name, title_row_name = sync.find_row("Bling", erp_tid)

		from crm.api import collection as collection_api

		promise_day = str(add_days(today(), 5))
		collection_api.set_promise(coll_card_name, title_row_name, promise_date=promise_day, promise_amount=1000.00)
		collection_api.create_agreement(
			coll_card_name,
			title_row_name,
			installments=[
				{"due_date": str(add_days(today(), 10)), "amount": 500.00},
				{"due_date": str(add_days(today(), 40)), "amount": 500.00},
			],
		)

		coll_events = frappe.get_all(
			"Cred Hub Webhook Outbox",
			{"webhook": wh.name, "reference_name": coll_card_name},
			pluck="event",
		)
		self.assertIn("title.promise_recorded", coll_events)
		self.assertIn("title.agreement_recorded", coll_events)

		# register_result exige organizations_write ao vincular erp_customer_id e funciona quando presente
		cards_only_token = self.create_key(("cards_write",), pipelines=[self.credit_pipeline])["token"]
		_authenticate(cards_only_token, "/api/method/cred_hub.erp.push.register_result", "POST")
		with self.assertRaises(frappe.PermissionError):
			register_result(
				deal=credit_deal.name,
				status="success",
				erp_source="TOTVS",
				erp_customer_id=f"CLI-{suffix}",
			)

		full_token = self.create_key(("cards_write", "organizations_write"), pipelines=[self.credit_pipeline])["token"]
		_authenticate(full_token, "/api/method/cred_hub.erp.push.register_result", "POST")
		res = register_result(
			deal=credit_deal.name,
			status="success",
			erp_source="TOTVS",
			erp_customer_id=f"CLI-{suffix}",
			message="Cedente integrado com sucesso",
		)
		self.assertTrue(res["ok"])
		self.assertEqual(res["external_id_linked"], f"CLI-{suffix}")
		self.assertEqual(frappe.db.get_value("CRM Organization", org.name, "external_id"), f"CLI-{suffix}")
