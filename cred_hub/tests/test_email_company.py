"""Descobrir a empresa pelo domínio do e-mail, Fase 1 (credtech#184): domínio, atalho por Organização, fila e cache.
Roda no bench: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_email_company`."""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.email_company import finder, safe_http
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

	def test_lead_insert_enqueues_only_corporate_emails_after_commit(self):
		with patch.object(frappe, "enqueue") as enqueue:
			finder.on_lead_insert(frappe._dict(name="L1", email="ana@acme-teste.com.br"))
			finder.on_lead_insert(frappe._dict(name="L2", email="ana@gmail.com"))
			finder.on_lead_insert(frappe._dict(name="L3", email=None))
		self.assertEqual(enqueue.call_count, 1)
		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["lead"], kwargs["enqueue_after_commit"]), ("L1", True))

	def test_switched_off_by_site_config(self):
		frappe.local.conf.credhub_email_company = 0
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_email_company", None))
		with patch.object(frappe, "enqueue") as enqueue:
			finder.on_lead_insert(frappe._dict(name="L1", email="ana@acme-teste.com.br"))
		enqueue.assert_not_called()

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
