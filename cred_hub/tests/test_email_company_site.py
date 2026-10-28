"""Leitura leve do site da empresa para achar o CNPJ (credtech#186). Nada sai do servidor: as páginas são simuladas.
Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.email_company import finder, rdap, safe_http, score, site

MATRIZ = "11222333000181"
FILIAL = "11222333000262"
OUTRA = "44555666000181"
COMPANY = {"legal_name": "ACME SEGUROS LTDA", "trade_name": "ACME SEGUROS", "registration_status": "ATIVA"}

HOME = f"""<html><body>
<a href="/politica-de-privacidade">Política de Privacidade</a>
<a href="https://outro-site.com/privacidade">Parceiro</a>
<footer>Acme Seguros · CNPJ 11.222.333/0002-62 (filial)</footer></body></html>"""
PRIVACY = """<html><body><p>A controladora dos seus dados é a ACME SEGUROS LTDA, inscrita no CNPJ 11.222.333/0001-81,
com sede em Campinas.</p><p>Fornecedor: 44.555.666/0001-81</p></body></html>"""
JSON_LD_HOME = f"""<html><head><script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Organization", "legalName": "ACME SEGUROS LTDA", "taxID": "{MATRIZ}"}}
</script></head><body>CNPJ {OUTRA}</body></html>"""


class FakePage:
	def __init__(self, url, body, status=200):
		self.url, self.status, self.headers, self.body = url, status, {"content-type": "text/html; charset=utf-8"}, body.encode()
		self.redirects = []

	@property
	def text(self):
		return self.body.decode()


def fake_site(pages):
	def fetch(url, **_kwargs):
		if url in pages:
			return FakePage(url, pages[url])
		raise safe_http.BlockedAddress("fora do ar")

	return fetch


class TestSiteReading(IntegrationTestCase):
	def setUp(self):
		frappe.cache.delete_keys(finder.CACHE_PREFIX)

	def tearDown(self):
		frappe.db.rollback()

	def test_json_ld_tax_id_comes_first(self):
		self.assertEqual(site.json_ld_companies(JSON_LD_HOME)[0], {"cnpj": MATRIZ, "legal_name": "ACME SEGUROS LTDA"})
		with patch.object(safe_http, "fetch", fake_site({"https://acme.com/": JSON_LD_HOME})):
			found = site.find_cnpj("acme.com")
		self.assertEqual((found["cnpj"], found["site_detail"]), (MATRIZ, "json-ld"))

	def test_text_cnpjs_need_valid_digits(self):
		found = {c["cnpj"] for c in site.text_cnpjs("CNPJ 11.222.333/0001-81 e 11.222.333/0001-82 e 12345678000100")}
		self.assertEqual(found, {MATRIZ})

	def test_matriz_near_controladora_wins_over_branch_and_supplier(self):
		pages = {"https://acme.com/": HOME, "https://acme.com/politica-de-privacidade": PRIVACY}
		with patch.object(safe_http, "fetch", fake_site(pages)):
			found = site.find_cnpj("acme.com")
		self.assertEqual(found["cnpj"], MATRIZ)
		self.assertEqual(found["url"], "https://acme.com/politica-de-privacidade")

	def test_only_policy_links_of_the_same_site_are_read(self):
		self.assertEqual(site.policy_links(HOME, "https://acme.com/"), ["https://acme.com/politica-de-privacidade"])

	def test_site_down_finds_nothing(self):
		with patch.object(safe_http, "fetch", fake_site({})):
			self.assertIsNone(site.find_cnpj("acme.com"))

	def test_discovery_uses_the_site_when_rdap_has_nothing(self):
		pages = {"https://acmeseguros.com/": HOME, "https://acmeseguros.com/politica-de-privacidade": PRIVACY}
		with (
			patch.object(safe_http, "fetch", fake_site(pages)),
			patch.object(rdap, "lookup", return_value=None),
			patch.object(finder, "receita", return_value=COMPANY),
		):
			result = finder.discover("ana@acmeseguros.com")
		self.assertEqual((result["cnpj"], result["source"], result["confidence"]), (MATRIZ, "site", score.HIGH))
