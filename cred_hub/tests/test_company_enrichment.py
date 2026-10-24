"""Testes de integração da presença digital (rodam no bench: `bench --site <site> run-tests --app cred_hub`)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.company import discover as discovery
from cred_hub.company import enrich

FOUND = {"site": "https://exemplo.com.br/", "linkedin": "https://www.linkedin.com/company/exemplo",
         "funcionarios": 0, "faixa": "51-200", "crm_bucket": "51-200", "fonte": "LinkedIn"}


class TestCompanyEnrichment(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _org(self, **fields):
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": "Exemplo Presença Teste", **fields}).insert()

	def test_fields_installed(self):
		for dt in ("CRM Organization", "CRM Lead"):
			for field in ("linkedin", "employee_count", "employee_range", "employee_source", "company_checked_at"):
				self.assertTrue(frappe.get_meta(dt).has_field(field), (dt, field))

	@patch.object(discovery, "discover", return_value=FOUND)
	def test_fills_without_overwriting_user_data(self, discover):
		org = self._org(website="https://site-do-usuario.com.br", company_size="Other size", main_cnae="6201501 - Software", is_mei="No")
		enrich.enrich_company("CRM Organization", org.name)
		org.reload()
		self.assertEqual(org.website, "https://site-do-usuario.com.br")
		self.assertEqual(org.linkedin, FOUND["linkedin"])
		self.assertEqual((org.employee_range, org.employee_source, org.no_of_employees), ("51-200", "LinkedIn", "51-200"))
		self.assertTrue(org.company_checked_at)
		# Porte/CNAE/MEI da v0.2 chegam no formato que o piso SEBRAE entende
		self.assertEqual(discover.call_args.kwargs, {"porte": "DEMAIS", "cnae": "6201501", "opcao_mei": False})
		# Só roda uma vez por registro
		enrich.enrich_company("CRM Organization", org.name)
		self.assertEqual(discover.call_count, 1)

	@patch.object(discovery, "discover", side_effect=RuntimeError("sem rede"))
	def test_failure_never_blocks(self, _discover):
		org = self._org()
		enrich.enrich_company("CRM Organization", org.name)
		self.assertFalse(frappe.db.get_value("CRM Organization", org.name, "company_checked_at"))
