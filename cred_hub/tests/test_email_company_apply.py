"""Empresa pelo e-mail do lead aplicada no card (credtech#187). Nada sai do servidor: o resultado da descoberta é
montado à mão. Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.email_company import apply, finder, score

CNPJ = "11222333000181"
COMPANY = {"legal_name": "ACME SEGUROS LTDA", "trade_name": "ACME SEGUROS", "registration_status": "ATIVA"}


class TestEmailCompanyApply(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline({"pipeline_name": "Empresa pelo e-mail", "stages": [{"deal_status": "Novo"}]})
		self.contact = frappe.get_doc({"doctype": "Contact", "first_name": "Ana", "last_name": "Acme"}).insert(
			ignore_permissions=True
		).name

	def tearDown(self):
		frappe.db.rollback()

	def card(self, organization=None):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Ana", "email": "ana@acmeseguros.com.br"}).insert(
			ignore_permissions=True
		)
		deal = frappe.get_doc(
			{
				"doctype": "CRM Deal",
				"pipeline": self.pipeline["name"],
				"status": self.pipeline["stages"][0]["name"],
				"lead": lead.name,
				"organization": organization,
				"contacts": [{"contact": self.contact, "is_primary": 1}],
			}
		).insert(ignore_permissions=True)
		return lead.name, deal.name

	def organization(self, name, cnpj=None):
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, "cnpj": cnpj}).insert(
			ignore_permissions=True
		).name

	def result(self, confidence=score.HIGH, organization=None):
		return {
			"domain": "acmeseguros.com.br",
			"final_domain": "acmeseguros.com.br",
			"cnpj": CNPJ,
			"source": "rdap",
			"rdap_domain": "acmeseguros.com.br",
			"organization": organization,
			"company": COMPANY,
			"confidence": confidence,
		}

	def deal(self, name):
		return frappe.get_doc("CRM Deal", name)

	def comments(self, deal):
		return frappe.get_all("Comment", filters={"reference_doctype": "CRM Deal", "reference_name": deal}, pluck="content")

	def contact_organizations(self):
		return [l.link_name for l in frappe.get_doc("Contact", self.contact).links if l.link_doctype == "CRM Organization"]

	def test_high_confidence_creates_the_organization_with_the_cnpj(self):
		lead, deal = self.card()
		apply.apply(lead, self.result())
		organization = self.deal(deal).organization
		self.assertEqual(organization, "ACME SEGUROS")
		self.assertEqual(frappe.db.get_value("CRM Organization", organization, "cnpj"), "11.222.333/0001-81")
		self.assertEqual(frappe.db.get_value("CRM Organization", organization, "website"), "https://acmeseguros.com.br")
		self.assertIn(organization, self.contact_organizations())
		self.assertTrue(any("acmeseguros.com.br" in c and "Registro.br" in c for c in self.comments(deal)))

	def test_high_confidence_fills_the_organization_made_for_this_lead(self):
		typed = self.organization("Acme digitado")
		lead, deal = self.card(organization=typed)
		apply.apply(lead, self.result())
		self.assertEqual(self.deal(deal).organization, typed)
		self.assertEqual(frappe.db.get_value("CRM Organization", typed, "cnpj"), "11.222.333/0001-81")
		self.assertFalse(frappe.db.exists("CRM Organization", "ACME SEGUROS"))

	def test_high_confidence_links_an_organization_that_already_exists(self):
		existing = self.organization("Acme já cadastrada", "11.222.333/0001-81")
		lead, deal = self.card()
		apply.apply(lead, self.result(organization=existing))
		self.assertEqual(self.deal(deal).organization, existing)
		self.assertIn(existing, self.contact_organizations())

	def test_organization_someone_chose_is_never_swapped(self):
		shared = self.organization("Usada em outro card")
		self.card(organization=shared)
		lead, deal = self.card(organization=shared)
		apply.apply(lead, self.result())
		doc = self.deal(deal)
		self.assertEqual(doc.organization, shared)
		self.assertFalse(frappe.db.get_value("CRM Organization", shared, "cnpj"))
		self.assertEqual((doc.email_company_status, doc.email_company_cnpj), (apply.SUGGESTED, "11.222.333/0001-81"))

	def test_medium_confidence_is_a_suggestion_confirmed_with_one_click(self):
		lead, deal = self.card()
		apply.apply(lead, self.result(confidence=score.MEDIUM))
		doc = self.deal(deal)
		self.assertFalse(doc.organization)
		self.assertEqual((doc.email_company_status, doc.email_company_name), (apply.SUGGESTED, "ACME SEGUROS"))
		with patch.object(finder, "receita", return_value=COMPANY):
			apply.confirm(deal)
		doc = self.deal(deal)
		self.assertEqual((doc.organization, doc.email_company_status), ("ACME SEGUROS", apply.CONFIRMED))

	def test_dismissed_suggestion_changes_nothing(self):
		lead, deal = self.card()
		apply.apply(lead, self.result(confidence=score.MEDIUM))
		apply.dismiss(deal)
		doc = self.deal(deal)
		self.assertEqual((doc.organization, doc.email_company_status), (None, apply.DISMISSED))
		with self.assertRaises(frappe.ValidationError):
			apply.confirm(deal)

	def test_low_confidence_does_nothing(self):
		lead, deal = self.card()
		apply.apply(lead, self.result(confidence=score.LOW))
		doc = self.deal(deal)
		self.assertEqual((doc.organization, doc.email_company_status), (None, None))
		self.assertEqual(self.comments(deal), [])

	def test_card_with_the_same_cnpj_stays_as_it_is(self):
		same = self.organization("Acme com CNPJ", "11.222.333/0001-81")
		lead, deal = self.card(organization=same)
		apply.apply(lead, self.result())
		self.assertEqual(self.deal(deal).organization, same)
		self.assertFalse(frappe.db.exists("CRM Organization", "ACME SEGUROS"))

	def test_only_managers_change_the_switch(self):
		frappe.set_user("crm.user1@example.com")
		with self.assertRaises(frappe.PermissionError):
			apply.save_setting(0)
