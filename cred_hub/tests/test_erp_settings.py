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
		doc.cedente_field_map[0].source = "organization.legal_name"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.cedente_field_map[0].source, "organization.legal_name")

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

	def test_lists_of_names_for_the_screen(self):
		"""The settings screen shows the ERP's managers and sectors by name (no id is typed in the CRM)."""
		configure("Smart Securities")
		erp_settings.forget_token()

		class FakeErp:
			def lookup(self, resource):
				return {
					"gerente": [{"idGerente": "2", "descricao": "Gilberto Pilla"}, {"idGerente": "1", "descricao": "Ana Souza"}],
					"setor": [{"idSetor": "264", "descricao": "Serviços"}],
				}[resource]

		with patch.object(erp_settings, "client", return_value=FakeErp()):
			data = erp_settings.erp_lists()
		self.assertEqual(data, {"managers": ["Ana Souza", "Gilberto Pilla"], "sectors": ["Serviços"]})
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			erp_settings.erp_lists()


class TestErpFullMapping(IntegrationTestCase):
	"""Every field and column of the ERP offered in the de-para screen (credtech#367)."""

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_screen_offers_every_smart_cedente_field_and_the_crm_fields(self):
		configure("Smart Securities", erp_enabled=0)
		options = erp_settings.mapping_options()
		fields = {f["erp_field"] for f in options["cedente"]["fields"]}
		self.assertIn("inscricaoEstadual", fields)
		self.assertIn("idOperador", fields)
		self.assertIn("organization.cnpj", {s["value"] for s in options["cedente"]["sources"]})
		self.assertIn("VALOR_TOTAL", options["title"]["columns"])
		targets = {t["value"] for t in options["title"]["targets"]}
		self.assertIn("updated_amount", targets)
		self.assertNotIn("species", targets)
		self.assertIn("E-mail", options["sacado"]["columns"])
		self.assertIn("TELEFONE", options["cedente_registry"]["columns"])
		self.assertNotIn("lists", options)  # the ERP lists come apart (mapping_lists): the screen never waits for the ERP
		# every source of the default de-para has a name on the screen (e.g. Contact › Full Name)
		sources = {s["value"] for s in options["cedente"]["sources"]}
		self.assertIn("contact.full_name", sources)

	def test_a_new_field_and_an_erp_list_name_are_kept(self):
		doc = configure("Smart Securities")
		doc.append("cedente_field_map", {"erp_field": "inscricaoEstadual", "source": "organization.legal_nature"})
		doc.append("cedente_field_map", {"erp_field": "idOperador", "source": "", "default_value": "Maria Operadora"})
		doc.save(ignore_permissions=True)
		rows = {r.erp_field: r for r in doc.cedente_field_map}
		self.assertEqual((rows["inscricaoEstadual"].transform, rows["idOperador"].source), ("trim", "erp_list"))
		self.assertEqual(rows["idOperador"].default_value, "Maria Operadora")

	def test_a_required_field_cannot_be_left_out(self):
		doc = configure("Smart Securities")
		row = next(r for r in doc.cedente_field_map if r.erp_field == "emailPrincipal")
		row.source, row.default_value = "", ""
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_an_unknown_crm_field_is_refused(self):
		doc = configure("Smart Securities")
		doc.cedente_field_map[0].source = "organization.does_not_exist"
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_title_map_never_targets_the_protest_species(self):
		doc = configure("Smart Securities")
		doc.set("title_field_map", [{"erp_column": "TIPO", "crm_field": "species"}, {"erp_column": "VALOR_TOTAL", "crm_field": "updated_amount"}])
		doc.save(ignore_permissions=True)
		self.assertEqual([r.crm_field for r in doc.title_field_map], ["updated_amount"])
