"""CNPJ no card (épico #66). Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import frappe
from crm.tests import default_test_pipeline
from frappe.tests import IntegrationTestCase

CNPJ = "11.222.333/0001-81"
OTHER = "11.444.777/0001-61"


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
			{"doctype": "CRM Lead", "pipeline": default_test_pipeline(), "first_name": "Ana", "cnpj": CNPJ, "trade_name": "Exemplo"}
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
			{"doctype": "CRM Lead", "pipeline": default_test_pipeline(), "first_name": "Bia", "organization": "Org Sem CNPJ", "cnpj": CNPJ}
		)
		lead.insert(ignore_permissions=True)
		lead.flags.ignore_permissions = True
		card = frappe.get_doc("CRM Deal", convert_to_deal(lead.name, doc=lead))
		self.assertEqual(frappe.db.get_value("CRM Organization", card.organization, "cnpj"), CNPJ)

	def _convert(self, **lead_fields):
		from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

		lead = frappe.get_doc({"doctype": "CRM Lead", "pipeline": default_test_pipeline(), "first_name": "Teste", **lead_fields})
		lead.insert(ignore_permissions=True)
		lead.flags.ignore_permissions = True
		return frappe.get_doc("CRM Deal", convert_to_deal(lead.name, doc=lead))

	def test_organization_with_other_cnpj_is_not_mixed(self):
		frappe.get_doc(
			{
				"doctype": "CRM Organization",
				"organization_name": "Org Outra",
				"cnpj": OTHER,
				"city": "JOINVILLE",
			}
		).insert()
		card = self._convert(organization="Org Outra", cnpj=CNPJ, city="BLUMENAU", legal_name="OUTRA LTDA")
		org = frappe.get_doc("CRM Organization", card.organization)
		self.assertEqual((org.cnpj, org.city), (OTHER, "JOINVILLE"))
		self.assertFalse(org.legal_name)
		comments = frappe.get_all(
			"Comment", {"reference_doctype": "CRM Deal", "reference_name": card.name}, pluck="content"
		)
		self.assertTrue(any(CNPJ in c for c in comments))

	def test_homonym_with_other_cnpj_gets_its_own_organization(self):
		frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Padaria Central", "cnpj": OTHER}
		).insert()
		card = self._convert(cnpj=CNPJ, trade_name="Padaria Central")
		self.assertEqual(card.organization, f"Padaria Central ({CNPJ})")
		self.assertEqual(frappe.db.get_value("CRM Organization", card.organization, "cnpj"), CNPJ)

	def test_homonym_without_cnpj_is_reused(self):
		frappe.get_doc({"doctype": "CRM Organization", "organization_name": "Padaria Sem CNPJ"}).insert()
		card = self._convert(cnpj=CNPJ, trade_name="Padaria Sem CNPJ")
		self.assertEqual(card.organization, "Padaria Sem CNPJ")
		self.assertEqual(frappe.db.get_value("CRM Organization", card.organization, "cnpj"), CNPJ)
