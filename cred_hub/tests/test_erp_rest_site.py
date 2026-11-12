"""Generic REST connector on a site (ADR 0007, credtech#375): settings, secrets, templates and the shared collection
sync driven only through the contract. HTTP is faked.
Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_erp_rest_site`."""

import copy
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.erp import rest_templates, sync
from cred_hub.erp import settings as erp_settings
from cred_hub.erp.connectors.rest import GENERIC
from cred_hub.tests.test_erp_clients import FakeResponse, FakeSession
from cred_hub.tests.test_erp_rest import ITEM, SAMPLE, SECRETS, TOKEN, page


def configure(config=None, secrets=None, **values):
	doc = frappe.get_doc(erp_settings.SETTINGS)
	doc.update({
		"erp_enabled": 1, "erp_provider": GENERIC, "rest_config": json.dumps(config or SAMPLE),
		"overdue_days": 1, "paid_lookback_days": 7, "last_paid_sync": None, "sync_dry_run": 0, **values,
	})
	doc.set("rest_secrets", [{"secret_name": k, "secret_value": v} for k, v in (secrets or SECRETS).items()])
	doc.save(ignore_permissions=True)
	frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
	return doc


class TestGenericRestSite(IntegrationTestCase):
	def setUp(self):
		self.patches = [patch("frappe.enqueue"), patch.object(frappe.db, "commit"), patch("time.sleep")]
		for p in self.patches:
			p.start()
		frappe.db.set_single_value("Cred Hub Provider Settings", "collection_pipeline", None)
		self.reset_cache()

	def tearDown(self):
		for p in self.patches:
			p.stop()
		frappe.db.rollback()
		frappe.flags.pop("cred_hub_erp_rest_config", None)
		self.reset_cache()
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		frappe.clear_document_cache("Cred Hub Provider Settings", "Cred Hub Provider Settings")

	def reset_cache(self):
		for key in (f"cred_hub_erp_report:{GENERIC}:overdue", f"cred_hub_erp_report:{GENERIC}:settled", erp_settings.TOKEN_KEY):
			frappe.cache.delete_value(key)

	def test_saving_normalizes_and_loads_the_suggested_de_para_of_the_config(self):
		doc = configure()
		saved = json.loads(doc.rest_config)
		self.assertEqual(saved["limits"]["max_redirects"], 0)
		self.assertEqual([r.erp_field for r in doc.cedente_field_map], ["documento", "razaoSocial"])
		self.assertEqual(doc.cedente_field_map[0].source, "organization.cnpj")

	def test_invalid_config_is_refused_with_the_reason(self):
		bad = copy.deepcopy(SAMPLE)
		bad["base_url"] = "http://10.0.0.1/api"
		with self.assertRaisesRegex(frappe.ValidationError, "API URL"):
			configure(bad)

	def test_secrets_are_encrypted_and_read_only_through_the_contract(self):
		doc = configure()
		row = doc.rest_secrets[1]
		raw = frappe.db.get_value("Cred Hub ERP Secret", row.name, "secret_value")
		self.assertNotEqual(raw, row.get_password("secret_value"))
		self.assertNotIn("s3cr3t", doc.rest_config)
		cls = erp_settings.connector_class(doc)
		values = erp_settings.credential_values(frappe.get_doc(erp_settings.SETTINGS), cls)
		self.assertEqual(values["rest_secrets"], SECRETS)
		self.assertEqual(cls.missing_credentials(values), [])

	def test_capabilities_come_from_the_site_config(self):
		configure()
		cls = erp_settings.connector_class()
		self.assertTrue(cls.capabilities.overdue_titles and cls.capabilities.register_cedente)
		self.assertEqual(cls.pace.report_cooldown, 300)
		options = erp_settings.mapping_options()
		self.assertIn("documento", [f["erp_field"] for f in options["cedente"]["fields"]])
		self.assertIn("sacado.documento", options["title"]["columns"])
		self.assertEqual(options["title"]["fixed"]["id"], "erp_title_id")

	def test_collection_sync_runs_through_the_contract(self):
		configure()
		session = FakeSession({
			("POST", "/oauth/token"): [TOKEN],
			("GET", "/v2/titulos"): [page([ITEM], last=True)],
			("GET", "/v2/titulos/liquidados"): [FakeResponse(200, [])],
		})
		with patch.object(frappe.db, "commit"):
			stats = sync.run(session=session)
		self.assertEqual((stats["overdue_read"], stats["titles_new"], stats["failed"]), (1, 1, 0), stats)
		self.assertTrue(sync.find_row(GENERIC, "501"))
		log = frappe.get_last_doc("Cred Hub Bureau Query", filters={"provider": GENERIC})
		self.assertNotIn("s3cr3t", (log.response or "") + (log.error or ""))

	def test_connection_test_logs_without_secrets(self):
		configure()
		session = FakeSession({("POST", "/oauth/token"): [TOKEN], ("GET", "/v2/health"): [FakeResponse(200, {"ok": 1})]})
		with patch("cred_hub.erp.safe_api.SafeApiSession", return_value=session):
			result = erp_settings.test_connection()
		self.assertTrue(result["ok"], result)
		log = frappe.get_last_doc("Cred Hub Bureau Query", filters={"provider": GENERIC})
		self.assertEqual(log.status, "Success")
		self.assertNotIn("s3cr3t", log.response or "")

	def test_template_export_and_import(self):
		configure()
		template = rest_templates.export_template()
		self.assertNotIn("base_url", template)
		self.assertNotIn("s3cr3t", json.dumps(template))
		frappe.db.set_single_value(erp_settings.SETTINGS, "rest_config", None)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		# The sample's token comes from auth.acme-erp.com.br: nothing is saved until the admin confirms that host
		result = rest_templates.import_template(json.dumps(template), "https://erp.outro-cliente.com.br/api")
		self.assertEqual((result["ok"], result["confirm_token_url"]), (False, "auth.acme-erp.com.br"))
		self.assertFalse(frappe.db.get_single_value(erp_settings.SETTINGS, "rest_config"))
		result = rest_templates.import_template(json.dumps(template), "https://erp.outro-cliente.com.br/api",
			confirm_token_url="auth.acme-erp.com.br")
		self.assertEqual(result["system"], "Acme ERP")
		doc = frappe.get_doc(erp_settings.SETTINGS)
		self.assertEqual(json.loads(doc.rest_config)["base_url"], "https://erp.outro-cliente.com.br/api")
		with self.assertRaises(frappe.ValidationError):
			rest_templates.import_template(json.dumps(template), "http://erp.outro-cliente.com.br")

	def test_changing_the_config_or_a_secret_forgets_the_token(self):
		configure()
		cls = erp_settings.connector_class()
		values = erp_settings.credential_values(frappe.get_doc(erp_settings.SETTINGS), cls)
		scope = cls.cache_scope(values)
		erp_settings._store_token(erp_settings.Token("old-token", 10**10), scope)
		self.assertEqual(erp_settings._cached_token(scope).value, "old-token")
		configure(secrets={**SECRETS, "client_secret": "rotated-secret-999"})
		self.assertEqual(erp_settings._cached_token(scope).value, "")
		values = erp_settings.credential_values(frappe.get_doc(erp_settings.SETTINGS), cls)
		self.assertNotEqual(cls.cache_scope(values), scope)

	def test_sandbox_collection_funnel_is_refused(self):
		configure()
		session = FakeSession({
			("POST", "/oauth/token"): [TOKEN],
			("GET", "/v2/titulos"): [page([ITEM], last=True)],
			("GET", "/v2/titulos/liquidados"): [FakeResponse(200, [])],
		})
		with patch("cred_hub.erp.settings.is_sandbox_pipeline", return_value=True):
			stats = sync.run(session=session)
		self.assertIn("Sandbox", stats["error"])
		self.assertFalse(sync.find_row(GENERIC, "501"))

	def test_templates_are_admin_only(self):
		configure()
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				rest_templates.export_template()
		finally:
			frappe.set_user("Administrator")
