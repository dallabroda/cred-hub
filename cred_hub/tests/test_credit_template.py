"""Funil "Crédito PJ" (credtech#316): mínimo de propósito, nada obrigatório por etapa e instala uma vez só.
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_template`."""

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.credit_check import api, template

SETTINGS = "Cred Hub Credit Settings"


class TestCreditTemplate(IntegrationTestCase):
	def setUp(self):
		# site de teste descartável: sem funil "Credit PJ" de antes
		for name in frappe.get_all("CRM Pipeline", {"pipeline_name": template.PIPELINE}, pluck="name"):
			frappe.delete_doc("CRM Pipeline", name, force=True, ignore_permissions=True)
		frappe.db.set_single_value(SETTINGS, "credit_pipeline", "")

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_status_before_install_lists_the_stages(self):
		status = api.get_credit_template_status()
		self.assertFalse(status["installed"])
		self.assertEqual(len(status["stages"]), 8)

	def test_install_creates_ordered_stages_and_fields_without_required_rules(self):
		result = api.install_credit_template()

		pipeline = frappe.get_doc("CRM Pipeline", result["pipeline"])
		stages = frappe.get_all(
			"CRM Deal Status", {"pipeline": pipeline.name}, ["deal_status", "type", "position"], order_by="position asc"
		)
		self.assertEqual([stage.deal_status for stage in stages], [stage[0] for stage in template.STAGES])
		self.assertEqual(stages[-2].type, "Won")
		self.assertEqual(stages[-1].type, "Lost")

		card = [row.fieldname for row in pipeline.fields if (row.block or "Card") == "Card"]
		for fieldname in ("deal_value", "credit_purpose", "credit_term_months", "credit_collateral", "credit_decision"):
			self.assertIn(fieldname, card)
		self.assertIn(("Organization", "annual_revenue"), [(row.block, row.fieldname) for row in pipeline.fields])
		# o sistema avisa, o operador decide: nenhuma etapa trava o card por campo faltando
		self.assertEqual([row for row in pipeline.field_rules if row.required], [])

		self.assertEqual(frappe.db.get_single_value(SETTINGS, "credit_pipeline"), pipeline.name)
		self.assertTrue(api.get_credit_template_status()["installed"])

	def test_second_install_is_refused(self):
		api.install_credit_template()
		with self.assertRaises(frappe.ValidationError):
			api.install_credit_template()

	def test_credit_fields_exist_on_the_card(self):
		meta = frappe.get_meta("CRM Deal")
		for fieldname in ("credit_purpose", "credit_term_months", "credit_collateral", "credit_decision"):
			self.assertTrue(meta.has_field(fieldname), fieldname)
		decision = meta.get_field("credit_decision")
		self.assertIn("Approved with conditions", decision.options)
