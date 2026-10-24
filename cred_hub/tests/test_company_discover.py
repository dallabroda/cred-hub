"""Testes unitários da descoberta de empresa (sem Frappe nem rede): `python -m unittest cred_hub.tests.test_company_discover`."""

import unittest

from cred_hub.company import discover as d


class TestParsing(unittest.TestCase):
	def test_registered_domain(self):
		self.assertEqual(d.registered_domain("https://nuapp.nubank.com.br/x"), "nubank.com.br")
		self.assertEqual(d.registered_domain("https://www.magalu.com"), "magalu.com")

	def test_blacklist_and_root(self):
		self.assertTrue(d.is_blacklisted("https://www.linkedin.com/company/x"))
		self.assertFalse(d.is_blacklisted("https://nubank.com.br"))
		self.assertEqual(d.root_url("https://nuapp.nubank.com.br/p"), "https://nubank.com.br/")

	def test_parse_range(self):
		self.assertEqual(d.parse_range("Acme | 51-200 funcionários"), "51-200")
		self.assertEqual(d.parse_range("x 10,001+ employees"), "10001+")
		self.assertEqual(d.parse_range("sem número"), "")

	def test_headcounts(self):
		self.assertEqual(d.parse_headcounts("approximately 34,832 total employees worldwide"), [34832])
		self.assertEqual(d.parse_headcounts("mais de 40.000 colaboradores"), [40000])

	def test_buckets(self):
		self.assertEqual(d.to_crm_bucket(12755), "1000+")
		self.assertEqual(d.to_crm_bucket(8), "1-10")
		self.assertEqual(d.range_to_crm_bucket("201-500"), "201-500")
		self.assertEqual(d.range_to_crm_bucket("10001+"), "1000+")

	def test_sebrae(self):
		self.assertEqual(d.sebrae_range("MICRO EMPRESA", "4711302"), "1-9")
		self.assertEqual(d.sebrae_range("EPP", "1011201"), "20-99")
		self.assertEqual(d.sebrae_range("DEMAIS", "6201501"), "50+")
		self.assertEqual(d.sebrae_range("", ""), "")

	def test_slug_matches(self):
		self.assertTrue(d.slug_matches("https://linkedin.com/company/magazine-luiza", "Magazine Luiza"))
		self.assertFalse(d.slug_matches("https://linkedin.com/company/roqqu", "Hebron Capital"))
		# Homônimo achado no teste real (24/10/2026): só uma palavra no meio do slug não basta
		self.assertFalse(d.slug_matches("https://www.linkedin.com/company/nima-credtech", "Credtech Inteligencia de Negocios"))
		self.assertFalse(d.slug_matches("https://www.linkedin.com/company/credtech-solutions", "Credtech Inteligencia de Negocios"))
		self.assertTrue(d.slug_matches("https://www.linkedin.com/company/credtech-inteligencia-de-negocios", "Credtech Inteligência de Negócios"))
		self.assertTrue(d.slug_matches("https://www.linkedin.com/company/grupo-boticario", "O Boticário"))


class FakeSearcher(d.Searcher):
	def __init__(self):
		pass

	def hits(self, query, n=8):
		raise RuntimeError("sem rede")


class TestDiscoverFailsSoft(unittest.TestCase):
	def test_sebrae_floor_when_everything_else_fails(self):
		class Http:
			def get(self, *a, **k):
				raise RuntimeError("sem rede")

		s = FakeSearcher()
		s.http = Http()
		out = d.discover("Padaria X", porte="MICRO EMPRESA", cnae="4721102", searcher=s)
		self.assertEqual(out["crm_bucket"], "1-10")
		self.assertEqual(out["fonte"], "SEBRAE/CNPJ")
		self.assertEqual(out["site"], "")


class TestLinkedinEmployeesOff(unittest.TestCase):
	def test_linkedin_range_and_page_are_ignored(self):
		class Http:
			def __init__(self):
				self.urls = []

			def get(self, url, *a, **k):
				self.urls.append(url)
				raise RuntimeError("sem rede")

		class Searcher(d.Searcher):
			def __init__(self):
				self.http = Http()

			def hits(self, query, n=8):
				if "linkedin" in query:
					return [{"url": "https://www.linkedin.com/company/padaria-x", "text": "Padaria X | 51-200 funcionários"}]
				return []

		s = Searcher()
		out = d.discover("Padaria X", porte="MICRO EMPRESA", cnae="4721102", searcher=s)
		self.assertEqual(out["linkedin"], "https://www.linkedin.com/company/padaria-x")
		self.assertEqual((out["faixa"], out["fonte"]), ("1-9", "SEBRAE/CNPJ"))
		self.assertFalse(any("linkedin.com" in u for u in s.http.urls))
