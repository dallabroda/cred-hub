"""CNPJ no card (épico #66). Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import frappe
from frappe.tests import IntegrationTestCase

CNPJ = "11.222.333/0001-81"


class TestCardRegistry(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_card_has_no_copy_of_registry_fields(self):
		# Os dados da Receita ficam só na Organização (pedido do dono: nada de campo redundante)
		meta = frappe.get_meta("CRM Deal")
		for field in ("cnpj", "legal_name", "org_cnpj"):
			self.assertFalse(meta.has_field(field), field)

	def test_lead_cnpj_goes_to_card_organization(self):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "Ana", "cnpj": CNPJ, "trade_name": "Exemplo"}
		)
		lead.insert(ignore_permissions=True)
		lead.flags.ignore_permissions = True
		card = frappe.get_doc("CRM Deal", convert_to_deal(lead.name, doc=lead))
		self.assertTrue(card.organization)
		self.assertEqual(frappe.db.get_value("CRM Organization", card.organization, "cnpj"), CNPJ)

	def test_lead_cnpj_fills_empty_cnpj_of_existing_organization(self):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		frappe.get_doc({"doctype": "CRM Organization", "organization_name": "Org Sem CNPJ"}).insert()
		lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "Bia", "organization": "Org Sem CNPJ", "cnpj": CNPJ}
		)
		lead.insert(ignore_permissions=True)
		lead.flags.ignore_permissions = True
		card = frappe.get_doc("CRM Deal", convert_to_deal(lead.name, doc=lead))
		self.assertEqual(frappe.db.get_value("CRM Organization", card.organization, "cnpj"), CNPJ)
