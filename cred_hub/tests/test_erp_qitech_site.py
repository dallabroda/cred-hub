"""QI Tech connector on a site (ADR 0007 §10, credtech#375): settings, encrypted private key, connection test and the
honest capabilities (no collection sync). HTTP is faked.
Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_erp_qitech_site`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.erp import settings as erp_settings
from cred_hub.erp import sync
from cred_hub.erp.connectors.qitech import QITECH
from cred_hub.tests.test_erp_clients import FakeSession
from cred_hub.tests.test_erp_qitech import API_KEY, PING_OK, PRIVATE_PEM


def configure(**values):
	doc = frappe.get_doc(erp_settings.SETTINGS)
	doc.update({
		"erp_enabled": 1, "erp_provider": QITECH, "qitech_base_url": "https://assignor-api.sandbox.qidtvm.com.br/",
		"qitech_api_key": API_KEY, "qitech_private_key": PRIVATE_PEM, **values,
	})
	doc.save(ignore_permissions=True)
	frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
	return doc


class TestQiTechSite(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		erp_settings.forget_token()
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def test_saving_loads_the_qitech_de_para_and_keeps_the_key_encrypted(self):
		doc = configure()
		self.assertEqual(doc.qitech_base_url, "https://assignor-api.sandbox.qidtvm.com.br")
		self.assertIn("document_number", [r.erp_field for r in doc.cedente_field_map])
		values = erp_settings.credential_values(frappe.get_doc(erp_settings.SETTINGS), erp_settings.connector_class())
		self.assertEqual(values["qitech_private_key"].strip(), PRIVATE_PEM.strip())
		self.assertEqual(values["qitech_api_key"], API_KEY)
		self.assertNotIn("PRIVATE KEY", str(frappe.db.get_single_value(erp_settings.SETTINGS, "qitech_private_key")))

	def test_http_url_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			configure(qitech_base_url="http://assignor-api.qidtvm.com.br")

	def test_connection_test_signs_and_logs_without_secrets(self):
		configure()
		session = FakeSession({("GET", "/authentication_test"): [PING_OK]})
		with patch("cred_hub.erp.base.requests.Session", return_value=session):
			result = erp_settings.test_connection()
		self.assertTrue(result["ok"], result)
		self.assertEqual(session.sent[0]["headers"]["API-CLIENT-KEY"], API_KEY)
		log = frappe.get_last_doc("Cred Hub Bureau Query", filters={"provider": QITECH})
		self.assertEqual(log.status, "Success")
		self.assertNotIn(API_KEY, (log.response or "") + (log.error or ""))

	def test_collection_sync_stays_off(self):
		configure()
		with patch.object(frappe.db, "commit"):
			stats = sync.run(session=FakeSession({}))
		self.assertIn("QI Tech", stats["message"])
		self.assertEqual(stats["overdue_read"], 0)
