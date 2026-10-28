"""Organização com CNPJ passa a se chamar pela razão social (decisão do dono, 28/10/2026). Nada sai do servidor."""

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.cnpj import enrich


class TestLegalName(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def organization(self, name):
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name}).insert(ignore_permissions=True)

	def test_the_organization_takes_the_legal_name_and_its_cards_follow(self):
		from crm.api.pipeline import save_pipeline

		org = self.organization("Magalu digitado")
		pipeline = save_pipeline({"pipeline_name": "Razão social", "stages": [{"deal_status": "Novo"}]})
		deal = frappe.get_doc(
			{
				"doctype": "CRM Deal",
				"pipeline": pipeline["name"],
				"status": pipeline["stages"][0]["name"],
				"organization": org.name,
			}
		).insert(ignore_permissions=True)
		enrich.use_legal_name(org, "MAGAZINE LUIZA S/A")
		self.assertFalse(frappe.db.exists("CRM Organization", "Magalu digitado"))
		self.assertEqual(frappe.db.get_value("CRM Organization", "MAGAZINE LUIZA S/A", "organization_name"), "MAGAZINE LUIZA S/A")
		self.assertEqual(frappe.db.get_value("CRM Deal", deal.name, "organization"), "MAGAZINE LUIZA S/A")

	def test_the_contacts_of_the_organization_follow_the_new_name(self):
		org = self.organization("Contatos digitado")
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Ana", "company_name": org.name}).insert(
			ignore_permissions=True
		)
		enrich.use_legal_name(org, "CONTATOS LTDA")
		self.assertEqual(frappe.db.get_value("Contact", contact.name, "company_name"), "CONTATOS LTDA")

	def test_a_name_already_taken_keeps_what_the_user_typed(self):
		self.organization("ACME SEGUROS LTDA")
		org = self.organization("Acme digitado")
		enrich.use_legal_name(org, "ACME SEGUROS LTDA")
		self.assertTrue(frappe.db.exists("CRM Organization", "Acme digitado"))

	def test_without_legal_name_nothing_changes(self):
		org = self.organization("Sem razão")
		enrich.use_legal_name(org, "")
		self.assertTrue(frappe.db.exists("CRM Organization", "Sem razão"))
