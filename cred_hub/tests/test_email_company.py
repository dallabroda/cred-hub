"""Descobrir a empresa pelo domínio do e-mail, Fase 1 (credtech#184): domínio, atalho por Organização, fila e cache.
Roda no bench: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_email_company`."""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.cnpj import client as cnpj_client
from cred_hub.email_company import apply, finder, rdap, safe_http
from cred_hub.email_company.domain import email_domain, url_domain


class TestEmailDomain(unittest.TestCase):
	def test_corporate_domain(self):
		self.assertEqual(email_domain("Ana@Vendas.Acme.com.br "), "acme.com.br")
		self.assertEqual(email_domain("rui@acme.com"), "acme.com")
		self.assertEqual(email_domain("joao@empresa.ind.br"), "empresa.ind.br")

	def test_personal_and_invalid_emails_stop(self):
		for email in (
			"ana@gmail.com", "ana@hotmail.com.br", "ana@outlook.com", "ana@uol.com.br", "ana@bol.com.br",
			"ana@terra.com.br", "ana@icloud.com", "ana@yahoo.com.br", "ana@example.com", "sem-arroba",
			"@acme.com.br", "ana@", "ana@localhost", "ana@acme..com.br", None, "",
		):
			with self.subTest(email=email):
				self.assertIsNone(email_domain(email))

	def test_site_domain(self):
		self.assertEqual(url_domain("https://www.acme.com.br/contato"), "acme.com.br")
		self.assertEqual(url_domain("acme.com.br"), "acme.com.br")
		self.assertIsNone(url_domain(""))


class TestEmailCompany(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_keys(finder.CACHE_PREFIX)

	def tearDown(self):
		frappe.cache.delete_keys(finder.CACHE_PREFIX)
		frappe.db.rollback()

	def organization(self, name, website):
		return frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": name, "website": website}
		).insert(ignore_permissions=True).name

	def page(self, url):
		return safe_http.Page(url, 200, {}, b"", [])

	def test_links_existing_organization_by_site_domain(self):
		org = self.organization("Acme Teste", "https://www.acme-teste.com.br/")
		self.organization("Outra", "https://acme-teste.com.br.golpe.com/")
		with patch.object(safe_http, "fetch", return_value=self.page("https://www.acme-teste.com.br/")):
			result = finder.discover("ana@acme-teste.com.br")
		self.assertEqual((result["organization"], result["confidence"], result["source"]), (org, "high", "organization"))

	def test_follows_site_redirect_to_the_final_domain(self):
		# O e-mail é acme-teste.com, o site redireciona para acme-teste.com.br, onde está a Organização
		org = self.organization("Acme Teste", "acme-teste.com.br")
		with patch.object(safe_http, "fetch", return_value=self.page("https://www.acme-teste.com.br/inicio")):
			result = finder.discover("ana@acme-teste.com")
		self.assertEqual((result["final_domain"], result["organization"]), ("acme-teste.com.br", org))

	def test_personal_email_does_nothing(self):
		with patch.object(safe_http, "fetch") as fetch:
			self.assertIsNone(finder.discover("ana@gmail.com"))
		fetch.assert_not_called()

	def test_no_match_keeps_everything_as_today(self):
		with patch.object(safe_http, "fetch", side_effect=safe_http.BlockedAddress("non-public")):
			result = finder.discover("ana@empresa-sem-site.com.br")
		self.assertEqual((result["organization"], result["confidence"]), (None, None))

	def test_site_lookup_cached_per_domain(self):
		with patch.object(safe_http, "fetch", return_value=self.page("https://acme-teste.com.br/")) as fetch:
			finder.discover("ana@acme-teste.com.br")
			finder.discover("rui@acme-teste.com.br")
		self.assertEqual(fetch.call_count, 1)

	def test_unreachable_site_also_cached(self):
		with patch.object(safe_http, "fetch", side_effect=OSError("timeout")) as fetch:
			finder.discover("ana@fora-do-ar.com.br")
			finder.discover("rui@fora-do-ar.com.br")
		self.assertEqual(fetch.call_count, 3)  # https, https://www e http, uma vez só

	# ---------- Fase 2: RDAP + Receita + confiança (credtech#185) ----------

	def receita(self, legal="ACME SEGUROS LTDA", trade="", status="ATIVA"):
		return {"legal_name": legal, "trade_name": trade, "registration_status": status, "partners": [{"name": "X"}]}

	def rdap_case(self, holder=None, company=None, email="ana@acmeseguros.com.br", company_name=None):
		holder = {"cnpj": "11222333000181", "holder": "ACME SEGUROS LTDA"} if holder is None else holder
		with (
			patch.object(safe_http, "fetch", side_effect=safe_http.BlockedAddress("sem site")),
			patch.object(rdap, "lookup", return_value=holder or None) as lookup,
			patch.object(cnpj_client, "fetch", return_value=company or self.receita()) as fetch,
		):
			result = finder.discover(email, company_name=company_name)
		return result, lookup, fetch

	def test_rdap_cnpj_confirmed_and_matching_is_high(self):
		result, _lookup, _fetch = self.rdap_case()
		self.assertEqual((result["cnpj"], result["confidence"], result["source"]), ("11222333000181", "high", "rdap"))
		# Só o necessário fica guardado: nada de sócios
		self.assertNotIn("partners", result["company"])

	def test_rdap_agency_holder_is_low(self):
		result, _lookup, _fetch = self.rdap_case(company=self.receita(legal="AGENCIA WEB PIXEL LTDA"))
		self.assertEqual(result["confidence"], "low")

	def test_form_company_name_gives_medium(self):
		result, _lookup, _fetch = self.rdap_case(
			company=self.receita(legal="ACME COMERCIO DE PECAS LTDA"),
			email="ana@pecasrapidas.com.br",
			company_name="Acme Peças",
		)
		self.assertEqual(result["confidence"], "medium")

	def test_existing_organization_with_the_cnpj_wins(self):
		if not frappe.get_meta("CRM Organization").has_field("cnpj"):
			self.skipTest("sem o campo CNPJ")
		org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Acme", "cnpj": "11.222.333/0001-81"}
		).insert(ignore_permissions=True).name
		result, _lookup, fetch = self.rdap_case()
		self.assertEqual((result["organization"], result["confidence"]), (org, "high"))
		fetch.assert_not_called()

	def test_invalid_cnpj_digits_and_person_holder_stop(self):
		result, _lookup, fetch = self.rdap_case(holder={"cnpj": "11222333000180", "holder": "X"})
		self.assertIsNone(result["confidence"])
		fetch.assert_not_called()
		frappe.cache.delete_keys(finder.CACHE_PREFIX)
		result, _lookup, _fetch = self.rdap_case(holder={})
		self.assertIsNone(result["confidence"])

	def test_rdap_only_for_br(self):
		result, lookup, _fetch = self.rdap_case(email="ana@acme.com")
		lookup.assert_not_called()
		self.assertIsNone(result["confidence"])

	def test_rdap_and_receita_cached_unavailable_not_cached(self):
		self.rdap_case()
		_result, lookup, fetch = self.rdap_case()
		lookup.assert_not_called()
		fetch.assert_not_called()
		frappe.cache.delete_keys(finder.CACHE_PREFIX)
		with (
			patch.object(safe_http, "fetch", side_effect=safe_http.BlockedAddress("sem site")),
			patch.object(rdap, "lookup", side_effect=rdap.RdapUnavailable("429")),
		):
			self.assertIsNone(finder.discover("ana@acmeseguros.com.br")["confidence"])
		_result, lookup, _fetch = self.rdap_case()
		lookup.assert_called_once()

	def test_receita_unavailable_does_not_decide(self):
		with (
			patch.object(safe_http, "fetch", side_effect=safe_http.BlockedAddress("sem site")),
			patch.object(rdap, "lookup", return_value={"cnpj": "11222333000181", "holder": "ACME"}),
			patch.object(cnpj_client, "fetch", side_effect=cnpj_client.CnpjUnavailable("fora")),
		):
			self.assertIsNone(finder.discover("ana@acmeseguros.com.br")["confidence"])
	def switch_on(self):
		frappe.local.conf.credhub_email_company = 1
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_email_company", None))

	def test_lead_insert_enqueues_only_corporate_emails_after_commit(self):
		self.switch_on()
		with patch.object(frappe, "enqueue") as enqueue:
			finder.on_lead_insert(frappe._dict(name="L1", email="ana@acme-teste.com.br"))
			finder.on_lead_insert(frappe._dict(name="L2", email="ana@gmail.com"))
			finder.on_lead_insert(frappe._dict(name="L3", email=None))
		self.assertEqual(enqueue.call_count, 1)
		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["lead"], kwargs["enqueue_after_commit"]), ("L1", True))

	def test_switch_off_in_settings_means_no_queue_no_http(self):
		# Interruptor em Configurações (credtech#187), ligado por padrão
		frappe.local.conf.pop("credhub_email_company", None)
		self.assertTrue(apply.enabled())
		apply.save_setting(0)
		self.addCleanup(lambda: frappe.db.set_single_value(apply.SETTINGS, apply.SETTING_FIELD, 1))
		with patch.object(frappe, "enqueue") as enqueue, patch.object(safe_http, "fetch") as fetch:
			finder.on_lead_insert(frappe._dict(name="L1", email="ana@acme-teste.com.br"))
		enqueue.assert_not_called()
		fetch.assert_not_called()

	def test_job_never_raises(self):
		lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "Ana", "email": "ana@acme-teste.com.br"}
		).insert(ignore_permissions=True)
		with (
			patch.object(finder, "discover", side_effect=RuntimeError("falha")),
			patch.object(frappe, "log_error", MagicMock()) as log_error,
		):
			self.assertIsNone(finder.run(lead.name))
		log_error.assert_called_once()
