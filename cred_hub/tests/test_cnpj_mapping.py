"""Testes das regras puras de CNPJ (rodam sem Frappe: `python -m pytest cred_hub/tests/test_cnpj_mapping.py`)."""

import unittest

import requests

from cred_hub.cnpj import client
from cred_hub.cnpj.mapping import format_cnpj, from_brasilapi, from_opencnpj, is_valid_cnpj, parse_brl_number

# Respostas reduzidas no formato real das APIs (dados fictícios)
OPENCNPJ = {
	"cnpj": "11222333000181",
	"razao_social": "EMPRESA EXEMPLO LTDA",
	"nome_fantasia": "EXEMPLO",
	"situacao_cadastral": "Ativa",
	"data_inicio_atividade": "2010-05-20",
	"cnae_principal": "6201501",
	"cnaes": [
		{"codigo": "6202300", "descricao": "Desenvolvimento de software customizável", "is_principal": False},
		{"codigo": "6201501", "descricao": "Desenvolvimento de programas de computador sob encomenda", "is_principal": True},
	],
	"natureza_juridica": "Sociedade Empresária Limitada",
	"tipo_logradouro": "RUA",
	"logradouro": "DAS FLORES",
	"numero": "100",
	"complemento": "SALA 2",
	"bairro": "CENTRO",
	"cep": "88010000",
	"uf": "SC",
	"municipio": "FLORIANOPOLIS",
	"email": "CONTATO@EXEMPLO.COM.BR",
	"telefones": [{"ddd": "48", "numero": "33330000", "is_fax": True}, {"ddd": "48", "numero": "33331111", "is_fax": False}],
	"capital_social": "205431960490,52",
	"porte_empresa": "Demais",
	"QSA": [
		{"nome_socio": "MARIA DA SILVA EXEMPLO", "cnpj_cpf_socio": "***123456**", "qualificacao_socio": "Sócio-Administrador"},
		{"nome_socio": "", "qualificacao_socio": "Sócio"},
	],
}

BRASILAPI = {
	"razao_social": "EMPRESA EXEMPLO LTDA",
	"nome_fantasia": "",
	"descricao_situacao_cadastral": "ATIVA",
	"data_inicio_atividade": "2010-05-20",
	"cnae_fiscal": 600001,
	"cnae_fiscal_descricao": "Extração de petróleo e gás natural",
	"natureza_juridica": "Sociedade Empresária Limitada",
	"descricao_tipo_de_logradouro": "AVENIDA",
	"logradouro": "BRASIL",
	"numero": "1",
	"complemento": "",
	"bairro": "CENTRO",
	"cep": "20031170",
	"uf": "RJ",
	"municipio": "RIO DE JANEIRO",
	"ddd_telefone_1": "2121660000",
	"capital_social": 205431960000,
	"porte": "DEMAIS",
	"email": None,
	"qsa": [{"nome_socio": "JOAO DE SOUZA EXEMPLO", "qualificacao_socio": "Diretor", "cnpj_cpf_do_socio": "***912137**"}],
}


class TestCnpjRules(unittest.TestCase):
	def test_valid_cnpj(self):
		self.assertTrue(is_valid_cnpj("11.222.333/0001-81"))
		self.assertTrue(is_valid_cnpj("33000167000101"))
		self.assertFalse(is_valid_cnpj("11.222.333/0001-80"))
		self.assertFalse(is_valid_cnpj("00000000000000"))
		self.assertFalse(is_valid_cnpj("123"))
		self.assertFalse(is_valid_cnpj(None))

	def test_format(self):
		self.assertEqual(format_cnpj("11222333000181"), "11.222.333/0001-81")
		self.assertEqual(format_cnpj(" 11.222.333/0001-81 "), "11.222.333/0001-81")
		self.assertEqual(format_cnpj("123"), "123")

	def test_brazilian_numbers(self):
		# Issue #58: a OpenCNPJ devolve vírgula decimal
		self.assertEqual(parse_brl_number("205431960490,52"), 205431960490.52)
		self.assertEqual(parse_brl_number("1.234.567,89"), 1234567.89)
		self.assertEqual(parse_brl_number("1500.00"), 1500.0)
		self.assertEqual(parse_brl_number(205431960000), 205431960000.0)
		self.assertEqual(parse_brl_number("R$ 10,00"), 10.0)
		self.assertIsNone(parse_brl_number(""))
		self.assertIsNone(parse_brl_number("abc"))


class TestMapping(unittest.TestCase):
	def test_opencnpj(self):
		d = from_opencnpj(OPENCNPJ)
		self.assertEqual(d["razao_social"], "EMPRESA EXEMPLO LTDA")
		self.assertEqual(d["situacao_cadastral"], "ATIVA")
		self.assertEqual(d["cnae_principal"], "6201501 - Desenvolvimento de programas de computador sob encomenda")
		self.assertEqual(d["capital_social"], 205431960490.52)
		self.assertEqual(d["telefone"], "(48) 33331111")
		self.assertEqual(d["email"], "contato@exemplo.com.br")
		self.assertEqual(d["endereco"], "RUA DAS FLORES, 100, SALA 2, CENTRO")
		self.assertEqual(d["socios"], [{"nome": "MARIA DA SILVA EXEMPLO", "qualificacao": "Sócio-Administrador", "documento": "***123456**"}])
		self.assertEqual(d["fonte"], "OpenCNPJ")

	def test_brasilapi(self):
		d = from_brasilapi(BRASILAPI)
		self.assertEqual(d["cnae_principal"], "0600001 - Extração de petróleo e gás natural")
		self.assertEqual(d["capital_social"], 205431960000.0)
		self.assertEqual(d["telefone"], "(21) 21660000")
		self.assertEqual(d["email"], "")
		self.assertEqual(d["endereco"], "AVENIDA BRASIL, 1, CENTRO")
		self.assertEqual(d["socios"][0]["nome"], "JOAO DE SOUZA EXEMPLO")
		self.assertEqual(d["fonte"], "BrasilAPI")


class _Resp:
	def __init__(self, status, payload=None):
		self.status_code = status
		self._payload = payload

	def json(self):
		if isinstance(self._payload, Exception):
			raise self._payload
		return self._payload


class _Session:
	def __init__(self, *responses):
		self.responses = list(responses)
		self.urls = []

	def get(self, url, **kwargs):
		self.urls.append(url)
		r = self.responses.pop(0)
		if isinstance(r, Exception):
			raise r
		return r


class TestClient(unittest.TestCase):
	def test_opencnpj_first(self):
		s = _Session(_Resp(200, OPENCNPJ))
		self.assertEqual(client.fetch("11.222.333/0001-81", session=s)["fonte"], "OpenCNPJ")
		self.assertEqual(s.urls, ["https://api.opencnpj.org/11222333000181"])

	def test_fallback_on_error(self):
		s = _Session(requests.ConnectionError("down"), _Resp(200, BRASILAPI))
		self.assertEqual(client.fetch("11222333000181", session=s)["fonte"], "BrasilAPI")

	def test_fallback_on_bad_payload(self):
		s = _Session(_Resp(200, ValueError("not json")), _Resp(200, BRASILAPI))
		self.assertEqual(client.fetch("11222333000181", session=s)["fonte"], "BrasilAPI")

	def test_not_found(self):
		with self.assertRaises(client.CnpjNotFound):
			client.fetch("11222333000181", session=_Session(_Resp(404), _Resp(404)))

	def test_unavailable(self):
		with self.assertRaises(client.CnpjUnavailable):
			client.fetch("11222333000181", session=_Session(_Resp(429), _Resp(500)))


if __name__ == "__main__":
	unittest.main()
