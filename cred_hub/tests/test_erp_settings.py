"""ERP integration settings (credtech#367): default de-para, token cache, connection test. HTTP is faked.
Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_erp_settings`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.erp import settings as erp_settings
from cred_hub.erp.mapper import DEFAULT_RULES
from cred_hub.tests.test_erp_clients import SMART_TOKEN, FakeResponse, FakeSession, hal


def configure(provider="Smart Securities", **values):
	doc = frappe.get_doc(erp_settings.SETTINGS)
	doc.update({
		"erp_enabled": 1, "erp_provider": provider, "smart_client_id": "cid", "smart_client_secret": "csecret",
		"wba_tenant_id": "tenant", "wba_username": "user", "wba_password": "pass", **values,
	})
	doc.save(ignore_permissions=True)
	return doc


class TestErpSettings(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		erp_settings.forget_token()
		frappe.db.rollback()

	def test_choosing_an_erp_loads_its_default_mapping(self):
		doc = configure("Smart Securities")
		self.assertEqual(
			[row.erp_field for row in doc.cedente_field_map],
			[rule["erp_field"] for rule in DEFAULT_RULES["Smart Securities"]],
		)
		doc = configure("WBA")
		self.assertEqual(doc.cedente_field_map[0].erp_field, "documento")

	def test_edited_mapping_is_kept_on_save(self):
		doc = configure("WBA")
		doc.cedente_field_map[0].source = "organization.custom_cnpj"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.cedente_field_map[0].source, "organization.custom_cnpj")

	def test_secret_is_stored_encrypted(self):
		configure("Smart Securities")
		raw = frappe.db.get_single_value(erp_settings.SETTINGS, "smart_client_secret")
		self.assertNotEqual(raw, "csecret")
		self.assertEqual(frappe.get_doc(erp_settings.SETTINGS).get_password("smart_client_secret"), "csecret")

	def test_connection_ok_is_logged(self):
		configure("Smart Securities")
		session = FakeSession({("POST", "/oauth"): [SMART_TOKEN], ("GET", "/smartsecurities/setor"): [hal("setor", [{"idSetor": "1"}])]})
		with patch("cred_hub.erp.base.requests.Session", return_value=session):
			result = erp_settings.test_connection()
		self.assertTrue(result["ok"], result)
		log = frappe.get_last_doc("Cred Hub Bureau Query", filters={"provider": "Smart Securities"})
		self.assertEqual(log.status, "Success")
		self.assertNotIn("csecret", log.response or "")
		# The new token is shared through Redis for the next job
		self.assertEqual(erp_settings._cached_token().value, "sm-1")

	def test_connection_refused_reports_and_logs(self):
		configure("WBA")
		session = FakeSession({("POST", "/oauth/token"): [FakeResponse(401, {"error_description": "Bad credentials"})]})
		with patch("cred_hub.erp.base.requests.Session", return_value=session):
			result = erp_settings.test_connection()
		self.assertFalse(result["ok"])
		self.assertIn("Bad credentials", result["message"])
		log = frappe.get_last_doc("Cred Hub Bureau Query", filters={"provider": "WBA"})
		self.assertEqual(log.status, "Error")

	def test_only_admins_can_test(self):
		configure("WBA")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			erp_settings.test_connection()
