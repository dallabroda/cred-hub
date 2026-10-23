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
			self.assertTrue(frappe.get_meta(dt).has_field("razao_social"), dt)

	def test_cnpj_is_formatted(self):
		org = self._org(cnpj="11222333000181")
		self.assertEqual(org.cnpj, "11.222.333/0001-81")

	@patch.object(client, "fetch", return_value=from_opencnpj(OPENCNPJ))
	def test_enrich_organization_and_partners(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		org.reload()
		self.assertEqual(org.razao_social, "EMPRESA EXEMPLO LTDA")
		self.assertEqual(org.capital_social, 205431960490.52)
		self.assertEqual(org.uf, "SC")
		self.assertTrue(org.cnpj_consultado_em)
		contacts = frappe.get_all("Contact", filters={"company_name": org.name}, pluck="full_name")
		self.assertEqual(contacts, ["Maria da Silva Exemplo"])
		# Rodar de novo não duplica os sócios
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertEqual(frappe.db.count("Contact", {"company_name": org.name}), 1)

	@patch.object(client, "fetch", return_value=from_opencnpj(OPENCNPJ))
	def test_enrich_lead_fills_organization(self, _fetch):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Teste", "cnpj": CNPJ}).insert()
		enrich.enrich_doc("CRM Lead", lead.name, "11222333000181")
		lead.reload()
		self.assertEqual(lead.organization, "EXEMPLO")
		self.assertEqual(lead.razao_social, "EMPRESA EXEMPLO LTDA")

	@patch.object(client, "fetch", side_effect=client.CnpjNotFound("11222333000181"))
	def test_not_found_marks_consulted(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertTrue(frappe.db.get_value("CRM Organization", org.name, "cnpj_consultado_em"))

	@patch.object(client, "fetch", side_effect=client.CnpjUnavailable("down"))
	def test_unavailable_allows_retry(self, _fetch):
		org = self._org()
		enrich.enrich_doc("CRM Organization", org.name, "11222333000181")
		self.assertFalse(frappe.db.get_value("CRM Organization", org.name, "cnpj_consultado_em"))

	def test_stale_job_is_ignored(self):
		org = self._org()
		with patch.object(client, "fetch") as fetch:
			enrich.enrich_doc("CRM Organization", org.name, "33000167000101")
			fetch.assert_not_called()

	def test_person_name(self):
		self.assertEqual(enrich.person_name("ANA MARIA DA SILVA"), "Ana Maria da Silva")
		self.assertEqual(enrich.person_name("DE SOUZA E FILHOS"), "De Souza e Filhos")
