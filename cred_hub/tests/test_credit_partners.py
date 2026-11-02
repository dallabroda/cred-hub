"""Sócios no crédito: CPF completo, escolha de quem consultar e consulta de sócio nos bureaus que aceitam CPF
(credtech#316). Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_partners`.
Nenhuma chamada sai do servidor: os provedores são simulados."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import billing
from cred_hub.credit_check import api, partners
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS

SETTINGS = "Cred Hub Credit Settings"
CNPJ = "11.222.333/0001-81"
CPF_A = "529.982.247-25"
CPF_B = "111.444.777-35"


class TestCpf(IntegrationTestCase):
	def test_cpf_validation_and_format(self):
		self.assertTrue(partners.is_valid_cpf(CPF_A))
		self.assertTrue(partners.is_valid_cpf("52998224725"))
		self.assertFalse(partners.is_valid_cpf("529.982.247-24"))  # dígito verificador errado
		self.assertFalse(partners.is_valid_cpf("111.111.111-11"))  # repetido
		self.assertFalse(partners.is_valid_cpf("***.982.247-**"))  # mascarado pela Receita
		self.assertFalse(partners.is_valid_cpf(""))
		self.assertEqual(partners.format_cpf("52998224725"), CPF_A)


class TestPartnersInCredit(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"directd_token": "directd-client-token",
				"serasa_enabled": 0,
				"boavista_risco_enabled": 1,
				"dossie_quod_enabled": 1,
				"scr_bacen_enabled": 1,
			}
		)
		settings.save()
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Exemplo Sócios Crédito Teste", "cnpj": CNPJ}
		).insert()
		self.org.db_set("credit_verdict", "Pre-Approved")
		self.other_org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Outra Empresa Sócios Teste", "cnpj": "22.333.444/0001-81"}
		).insert()
		self.ana = self._contact("Ana Souza Teste", self.org.name, share=60, cpf=CPF_A)
		self.bruno = self._contact("Bruno Lima Teste", self.org.name, share=40)  # CPF mascarado/ausente
		self.stranger = self._contact("Carla Alheia Teste", self.other_org.name, cpf=CPF_B)
		self.balance_before = billing.wallet_balance()

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _contact(self, name, company, share=None, cpf=""):
		contact = frappe.get_doc({"doctype": "Contact", "first_name": name, "company_name": company})
		if share is not None:
			contact.share_percentage = share
		if cpf:
			contact.cpf = cpf
		contact.flags.ignore_mandatory = True
		return contact.insert()

	def test_partners_list_shows_cpf_status_and_only_this_organization(self):
		rows = api.get_partners(self.org.name)
		self.assertEqual([row["contact"] for row in rows], [self.ana.name, self.bruno.name])
		self.assertTrue(rows[0]["has_cpf"])
		self.assertEqual(rows[0]["cpf"], CPF_A)
		self.assertFalse(rows[1]["has_cpf"])
		self.assertEqual(rows[1]["cpf"], "")

	def test_save_partner_cpf_validates_and_formats(self):
		with self.assertRaises(frappe.ValidationError):
			api.save_partner_cpf(self.org.name, self.bruno.name, "111.222.333-44")
		saved = api.save_partner_cpf(self.org.name, self.bruno.name, "11144477735")
		self.assertEqual(saved["cpf"], CPF_B)
		self.assertEqual(frappe.db.get_value("Contact", self.bruno.name, "cpf"), CPF_B)
		# contato de outra empresa não é sócio desta
		with self.assertRaises(frappe.ValidationError):
			api.save_partner_cpf(self.org.name, self.stranger.name, CPF_A)

	def test_options_mark_which_bureaus_accept_a_partner(self):
		options = api.get_final_bureau_options(organization_name=self.org.name)
		accepts = {item["key"]: item["partner_ok"] for item in options["bureaus"]}
		self.assertEqual(accepts, {"boavista_risco": False, "dossie_quod": True, "scr_bacen": True})

	def test_partner_query_uses_the_partner_cpf_and_is_logged_as_partner(self):
		with patch.object(PREMIUM_BUREAUS["scr_bacen"], "fetch", return_value={"raw": {"situacao": "ok"}}) as fetch:
			result = api.request_bureau_final(
				organization_name=self.org.name, bureau="scr_bacen", confirm_cost=1, contact_name=self.ana.name
			)

		self.assertEqual(fetch.call_args.args[0], "52998224725")
		self.assertEqual(fetch.call_args.args[1], "directd-client-token")
		self.assertEqual(result["status"], "Concluído")
		log = frappe.get_last_doc("Cred Hub Premium Bureau Query", filters={"organization": self.org.name})
		self.assertEqual(log.subject_type, "Partner")
		self.assertEqual(log.contact, self.ana.name)
		self.assertEqual(log.document, "52998224725")
		self.assertEqual(log.cnpj, CNPJ)
		comment = frappe.get_last_doc(
			"Comment", filters={"reference_doctype": "CRM Organization", "reference_name": self.org.name}
		)
		self.assertIn("Ana Souza Teste", comment.content)
		self.assertEqual(billing.wallet_balance(), self.balance_before)

	def test_partner_query_refused_without_cpf_or_in_company_only_bureaus(self):
		with patch.object(PREMIUM_BUREAUS["scr_bacen"], "fetch") as scr, patch.object(
			PREMIUM_BUREAUS["boavista_risco"], "fetch"
		) as boavista:
			with self.assertRaises(frappe.ValidationError):  # Bruno ainda não tem CPF
				api.request_bureau_final(
					organization_name=self.org.name, bureau="scr_bacen", confirm_cost=1, contact_name=self.bruno.name
				)
			with self.assertRaises(frappe.ValidationError):  # Boa Vista PJ não aceita CPF
				api.request_bureau_final(
					organization_name=self.org.name, bureau="boavista_risco", confirm_cost=1, contact_name=self.ana.name
				)
			with self.assertRaises(frappe.ValidationError):  # sócio de outra empresa
				api.request_bureau_final(
					organization_name=self.org.name, bureau="scr_bacen", confirm_cost=1, contact_name=self.stranger.name
				)
		scr.assert_not_called()
		boavista.assert_not_called()

	def test_results_list_company_and_partner_queries_with_full_return(self):
		with patch.object(PREMIUM_BUREAUS["dossie_quod"], "fetch", return_value={"raw": {"score": 750, "nested": {"a": 1}}}):
			api.request_bureau_final(organization_name=self.org.name, bureau="dossie_quod", confirm_cost=1)
			api.request_bureau_final(
				organization_name=self.org.name, bureau="dossie_quod", confirm_cost=1, contact_name=self.ana.name
			)

		rows = api.get_bureau_results(self.org.name)
		self.assertEqual(len(rows), 2)
		self.assertEqual({row["subject_type"] for row in rows}, {"Organization", "Partner"})
		partner_row = next(row for row in rows if row["subject_type"] == "Partner")
		self.assertEqual(partner_row["subject_name"], "Ana Souza Teste")
		self.assertEqual(partner_row["bureau_label"], PREMIUM_BUREAUS["dossie_quod"].label)
		self.assertEqual(partner_row["price_reference"], PREMIUM_BUREAUS["dossie_quod"].price_reference)
		self.assertNotIn("raw", partner_row)

		detail = api.get_bureau_result(partner_row["name"])
		self.assertEqual(detail["raw"], {"score": 750, "nested": {"a": 1}})
		self.assertEqual(detail["document"], "52998224725")
