"""RDAP do Registro.br e confiança do CNPJ (credtech#185). Sem rede: respostas no formato verificado em 27/10/2026,
com dados fictícios. Roda no bench ou com `python -m unittest cred_hub.tests.test_email_company_rdap`."""

import unittest
from unittest.mock import MagicMock

import requests

from cred_hub.email_company import rdap, score

CNPJ = "11222333000181"  # válido nos dígitos, fictício


def answer(roles=("registrant",), id_type="cnpj", identifier="11.222.333/0001-81", name="ACME SEGUROS LTDA"):
	return {
		"objectClassName": "domain",
		"ldhName": "acmeseguros.com.br",
		"entities": [
			{
				"roles": list(roles),
				"vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", name]]],
				"publicIds": [{"type": id_type, "identifier": identifier}],
				"legalRepresentative": "Fulano de Tal",
			},
			{"roles": ["technical"], "vcardArray": ["vcard", [["fn", {}, "text", "Suporte"]]]},
		],
	}


def session(status=200, payload=None, exc=None):
	http = MagicMock()
	if exc:
		http.get.side_effect = exc
	else:
		response = MagicMock(status_code=status)
		response.json.return_value = payload
		http.get.return_value = response
	return http


class TestRdap(unittest.TestCase):
	def test_registrant_company_cnpj_without_mask(self):
		self.assertEqual(rdap.parse(answer()), {"cnpj": CNPJ, "holder": "ACME SEGUROS LTDA"})

	def test_legal_representative_is_not_read(self):
		self.assertNotIn("Fulano", repr(rdap.parse(answer())))

	def test_person_holder_is_discarded(self):
		self.assertIsNone(rdap.parse(answer(id_type="cpf", identifier="123.456.789-09")))
		self.assertIsNone(rdap.parse(answer(roles=("technical",))))
		self.assertIsNone(rdap.parse({"entities": []}))

	def test_lookup_only_br_and_404_is_none(self):
		http = session(payload=answer())
		self.assertIsNone(rdap.lookup("acme.com", session=http))
		self.assertIsNone(rdap.lookup("../etc.br", session=http))
		http.get.assert_not_called()
		self.assertEqual(rdap.lookup("acmeseguros.com.br", session=http)["cnpj"], CNPJ)
		self.assertEqual(http.get.call_args.args[0], "https://rdap.registro.br/domain/acmeseguros.com.br")
		self.assertIsNone(rdap.lookup("naoexiste.com.br", session=session(status=404)))

	def test_unavailable_raises_to_retry_later(self):
		for http in (session(status=429), session(status=503), session(exc=requests.Timeout())):
			with self.subTest(http=http), self.assertRaises(rdap.RdapUnavailable):
				rdap.lookup("acmeseguros.com.br", session=http)


class TestConfidence(unittest.TestCase):
	def company(self, legal, trade="", status="ATIVA"):
		return {"legal_name": legal, "trade_name": trade, "registration_status": status}

	def test_high_when_name_matches_domain(self):
		for legal, trade, domain in (
			("ACME SEGUROS LTDA", "", "acmeseguros.com.br"),
			("MAGAZINE LUIZA S/A", "MAGAZINE LUIZA", "magazineluiza.com.br"),
			("CREDTECH INTELIGENCIA DE NEGOCIOS LTDA", "CREDTECH", "credtechdigital.com.br"),
			("Padaria São João Comércio de Alimentos Ltda", "", "padariasaojoao.com.br"),
			("Casa do Pão de Queijo Ltda", "", "casadopao.com.br"),
		):
			with self.subTest(domain=domain):
				self.assertEqual(score.confidence(self.company(legal, trade), domain), score.HIGH)

	def test_medium_when_only_the_form_or_a_word_matches(self):
		self.assertEqual(
			score.confidence(self.company("ACME COMERCIO DE PECAS LTDA"), "pecasrapidas.com.br", "Acme Peças"),
			score.MEDIUM,
		)
		self.assertEqual(
			score.confidence(self.company("CREDTECH INTELIGENCIA DE NEGOCIOS LTDA"), "credtechdigital.com.br"),
			score.MEDIUM,
		)

	def test_short_name_inside_a_long_domain_is_not_full(self):
		# "Casa Ltda" não é dona de casadoscelulares.com.br só por "casa" estar no domínio: vira sugestão, não liga
		self.assertEqual(score.confidence(self.company("CASA LTDA"), "casadoscelulares.com.br"), score.MEDIUM)
		self.assertEqual(score.confidence(self.company("ACME LTDA"), "acmeseguros.com.br"), score.MEDIUM)
		self.assertEqual(score.confidence(self.company("ACME SEGUROS LTDA"), "acmeseguros.com.br"), score.HIGH)

	def test_low_when_name_does_not_match_or_not_active(self):
		# Domínio registrado por uma agência, em nome dela
		self.assertEqual(score.confidence(self.company("AGENCIA WEB PIXEL LTDA"), "acmeseguros.com.br"), score.LOW)
		self.assertEqual(
			score.confidence(self.company("ACME SEGUROS LTDA", status="BAIXADA"), "acmeseguros.com.br"), score.LOW
		)
		self.assertEqual(score.confidence(self.company("NU PAGAMENTOS S.A."), "nubank.com.br"), score.LOW)


if __name__ == "__main__":
	unittest.main()
