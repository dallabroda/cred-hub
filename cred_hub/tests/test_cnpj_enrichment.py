"""Testes de integração (rodam no bench: `bench --site <site> run-tests --app cred_hub`)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.cnpj import client, enrich
from cred_hub.cnpj.mapping import from_opencnpj
from cred_hub.tests.test_cnpj_mapping import OPENCNPJ

CNPJ = "11.222.333/0001-81"


class TestCnpjEnrichment(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _org(self, name="Exemplo Teste Cred Hub", cnpj=CNPJ):
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, "cnpj": cnpj}).insert()

	def test_custom_fields_installed(self):
		for dt in ("CRM Organization", "CRM Lead"):
			self.assertTrue(frappe.get_meta(dt).has_field("cnpj"), dt)
			for field in ("legal_name", "company_size", "is_mei", "neighborhood", "years_in_business"):
				self.assertTrue(frappe.get_meta(dt).has_field(field), (dt, field))
			self.assertFalse(frappe.get_meta(dt).has_field("razao_social"), dt)

	def test_cnpj_is_formatted(self):
		org = self._org(cnpj="11222333000181")
		self.assertEqual(org.cnpj, "11.222.333/0001-81")

	@patch.object(client, "fetch", return_value=from_opencnpj(OPENCNPJ))
	def test_enrich_organization_and_partners(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		# com CNPJ, a organização passa a se chamar pela razão social (decisão do dono, 28/10/2026)
		self.assertFalse(frappe.db.exists("CRM Organization", "Exemplo Teste Cred Hub"))
		org = frappe.get_doc("CRM Organization", "EMPRESA EXEMPLO LTDA")
		self.assertEqual((org.legal_name, org.organization_name), ("EMPRESA EXEMPLO LTDA", "EMPRESA EXEMPLO LTDA"))
		self.assertEqual(org.share_capital, 205431960490.52)
		self.assertEqual((org.state, org.city, org.neighborhood), ("SC", "FLORIANOPOLIS", "CENTRO"))
		self.assertEqual((org.company_size, org.is_mei), ("Other size", "No"))
		self.assertEqual(org.years_in_business, enrich.years_between("2010-05-20"))
		self.assertEqual(org.industry, "Software Development")
		self.assertTrue(org.cnpj_checked_on)
		address = frappe.get_doc("Address", org.address)
		self.assertEqual((address.address_line1, address.address_line2), ("RUA DAS FLORES, 100", "SALA 2, CENTRO"))
		self.assertEqual((address.city, address.state, address.pincode, address.country), ("FLORIANOPOLIS", "SC", "88010-000", "Brazil"))
		self.assertTrue(any(link.link_name == org.name for link in address.links))
		contacts = frappe.get_all("Contact", filters={"company_name": org.name}, pluck="full_name")
		self.assertEqual(contacts, ["Maria da Silva Exemplo"])
		# Rodar de novo não duplica os sócios
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertEqual(frappe.db.count("Contact", {"company_name": org.name}), 1)
		# ...e reaproveita o mesmo endereço
		receita = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Address", "link_doctype": "CRM Organization", "link_name": org.name},
			pluck="parent",
		)
		self.assertEqual(len(set(receita)), 1)

	@patch.object(client, "fetch", return_value=from_opencnpj(OPENCNPJ))
	def test_manual_address_is_kept(self, _fetch):
		org = self._org()
		manual = frappe.get_doc({"doctype": "Address", "address_title": "Escritório", "address_type": "Office",
		                         "address_line1": "Rua A, 1", "city": "Curitiba", "country": "Brazil"}).insert()
		org.db_set("address", manual.name)
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertEqual(frappe.db.get_value("CRM Organization", "EMPRESA EXEMPLO LTDA", "address"), manual.name)

	def test_years_between(self):
		self.assertEqual(enrich.years_between("2025-03-26", "2026-03-25"), 0)
		self.assertEqual(enrich.years_between("2025-03-26", "2026-03-26"), 1)
		self.assertEqual(enrich.years_between("2010-05-20", "2026-10-24"), 16)
		self.assertIsNone(enrich.years_between(None))

	@patch.object(client, "fetch", return_value=from_opencnpj(OPENCNPJ))
	def test_enrich_lead_fills_organization(self, _fetch):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Teste", "cnpj": CNPJ}).insert()
		enrich.enrich_doc("CRM Lead", lead.name, "11222333000181")
		lead.reload()
		self.assertEqual(lead.organization, "EXEMPLO")
		self.assertEqual(lead.legal_name, "EMPRESA EXEMPLO LTDA")
		self.assertEqual(lead.industry, "Software Development")

	@patch.object(client, "fetch", side_effect=client.CnpjNotFound("11222333000181"))
	def test_not_found_marks_consulted(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertTrue(frappe.db.get_value("CRM Organization", org.name, "cnpj_checked_on"))

	@patch.object(client, "fetch", side_effect=client.CnpjUnavailable("down"))
	def test_unavailable_allows_retry(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertFalse(frappe.db.get_value("CRM Organization", org.name, "cnpj_checked_on"))

	def test_stale_job_is_ignored(self):
		org = self._org()
		with patch.object(client, "fetch") as fetch:
			enrich.enrich_doc("CRM Organization", org.name, "33000167000101")
			fetch.assert_not_called()

	def test_person_name(self):
		self.assertEqual(enrich.person_name("ANA MARIA DA SILVA"), "Ana Maria da Silva")
		self.assertEqual(enrich.person_name("DE SOUZA E FILHOS"), "De Souza e Filhos")
