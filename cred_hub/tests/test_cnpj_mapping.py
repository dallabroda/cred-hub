"""Testes das regras puras de CNPJ (rodam sem Frappe: `python -m pytest cred_hub/tests/test_cnpj_mapping.py`)."""

import unittest

import requests

from cred_hub.cnpj import client
from cred_hub.cnpj.mapping import company_size, format_cnpj, from_brasilapi, from_opencnpj, is_valid_cnpj, parse_brl_number
from cred_hub.cnpj.sectors import SECTORS, sector_for_cnae

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
	"opcao_mei": "N",
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
	"codigo_porte": 5,
	"opcao_pelo_mei": None,
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
		self.assertEqual(d["legal_name"], "EMPRESA EXEMPLO LTDA")
		self.assertEqual(d["registration_status"], "ATIVA")
		self.assertEqual(d["main_cnae"], "6201501 - Desenvolvimento de programas de computador sob encomenda")
		self.assertEqual(d["share_capital"], 205431960490.52)
		self.assertEqual(d["registry_phone"], "(48) 33331111")
		self.assertEqual(d["registry_email"], "contato@exemplo.com.br")
		self.assertEqual(d["company_size"], "Other size")
		self.assertEqual(d["is_mei"], "No")
		self.assertEqual((d["neighborhood"], d["city"], d["state"]), ("CENTRO", "FLORIANOPOLIS", "SC"))
		self.assertEqual(
			d["address"],
			{"address_line1": "RUA DAS FLORES, 100", "address_line2": "SALA 2, CENTRO", "city": "FLORIANOPOLIS", "state": "SC", "pincode": "88010-000"},
		)
		self.assertEqual(d["partners"], [{"name": "MARIA DA SILVA EXEMPLO", "qualification": "Sócio-Administrador", "document": "***123456**"}])
		self.assertEqual(d["source"], "OpenCNPJ")

	def test_brasilapi(self):
		d = from_brasilapi(BRASILAPI)
		self.assertEqual(d["main_cnae"], "0600001 - Extração de petróleo e gás natural")
		self.assertEqual(d["share_capital"], 205431960000.0)
		self.assertEqual(d["registry_phone"], "(21) 21660000")
		self.assertEqual(d["registry_email"], "")
		self.assertEqual(d["company_size"], "Other size")
		self.assertEqual(d["is_mei"], "")
		self.assertEqual(d["address"]["address_line1"], "AVENIDA BRASIL, 1")
		self.assertEqual(d["address"]["address_line2"], "CENTRO")
		self.assertEqual(d["address"]["pincode"], "20031-170")
		self.assertEqual(d["partners"][0]["name"], "JOAO DE SOUZA EXEMPLO")
		self.assertEqual(d["source"], "BrasilAPI")

	def test_company_size(self):
		self.assertEqual(company_size(text="Microempresa (ME)"), "ME")
		self.assertEqual(company_size(text="MICRO EMPRESA"), "ME")
		self.assertEqual(company_size(text="Empresa de Pequeno Porte (EPP)"), "EPP")
		self.assertEqual(company_size(text="Demais"), "Other size")
		self.assertEqual(company_size(code=3, text=""), "EPP")
		self.assertEqual(company_size(code="05"), "Other size")
		self.assertEqual(company_size(text=""), "Not informed")


class TestSectors(unittest.TestCase):
	def test_examples_from_owner(self):
		self.assertEqual(sector_for_cnae("6204000 - Consultoria em tecnologia da informação"), "IT Consulting and Services")
		self.assertEqual(sector_for_cnae("0600001"), "Mining, Oil and Gas")

	def test_group_and_class_rules(self):
		self.assertEqual(sector_for_cnae("6201501"), "Software Development")
		self.assertEqual(sector_for_cnae("4930202"), "Road and Rail Freight Transport")
		self.assertEqual(sector_for_cnae("4921301"), "Passenger Transport")
		self.assertEqual(sector_for_cnae("4711302"), "Supermarkets and Food Retail")
		self.assertEqual(sector_for_cnae("4771701"), "Pharmacies, Perfumeries and Opticians")
		self.assertEqual(sector_for_cnae("4781400"), "Apparel, Footwear and Accessories Retail")
		self.assertEqual(sector_for_cnae("6911701"), "Legal Services")
		self.assertEqual(sector_for_cnae("6920601"), "Accounting and Auditing")
		self.assertEqual(sector_for_cnae("8610101"), "Hospitals")
		self.assertEqual(sector_for_cnae("8630503"), "Clinics, Dental Offices and Laboratories")
		self.assertEqual(sector_for_cnae("6422100"), "Banking, Credit and Financing")
		self.assertEqual(sector_for_cnae("6204-0/00"), "IT Consulting and Services")

	def test_unknown(self):
		self.assertIsNone(sector_for_cnae(""))
		self.assertIsNone(sector_for_cnae(None))

	def test_every_cnae_division_has_a_sector(self):
		# Divisões existentes no CNAE 2.3 (01 a 99, sem as que não existem)
		absent = {"04", "34", "40", "44", "48", "54", "57", "67", "76", "83", "89"}
		# Divisões separadas por grupo/classe: todos os grupos reais delas
		split = {
			"47": ("4711302", "4721102", "4731800", "4741500", "4751201", "4761001", "4771701", "4781400", "4784900", "4785701", "4789001", "4790000"),
			"49": ("4911600", "4912401", "4921301", "4930202", "4940000", "4950700"),
			"62": ("6201501", "6202300", "6203100", "6204000", "6209100"),
			"69": ("6911701", "6912500", "6920601"),
			"86": ("8610101", "8621601", "8630501", "8640202", "8650001", "8660700", "8690901"),
		}
		for division in (f"{n:02d}" for n in range(1, 100)):
			if division in absent:
				continue
			for code in split.get(division, (division + "00000",)):
				self.assertIsNotNone(sector_for_cnae(code), code)

	def test_sector_names_unique(self):
		names = [s for _p, s, _pt in SECTORS]
		self.assertEqual(len(names), len(set(names)))


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
		self.assertEqual(client.fetch("11.222.333/0001-81", session=s)["source"], "OpenCNPJ")
		self.assertEqual(s.urls, ["https://api.opencnpj.org/11222333000181"])

	def test_fallback_on_error(self):
		s = _Session(requests.ConnectionError("down"), _Resp(200, BRASILAPI))
		self.assertEqual(client.fetch("11222333000181", session=s)["source"], "BrasilAPI")

	def test_fallback_on_bad_payload(self):
		s = _Session(_Resp(200, ValueError("not json")), _Resp(200, BRASILAPI))
		self.assertEqual(client.fetch("11222333000181", session=s)["source"], "BrasilAPI")

	def test_not_found(self):
		with self.assertRaises(client.CnpjNotFound):
			client.fetch("11222333000181", session=_Session(_Resp(404), _Resp(404)))

	def test_unavailable(self):
		with self.assertRaises(client.CnpjUnavailable):
			client.fetch("11222333000181", session=_Session(_Resp(429), _Resp(500)))


if __name__ == "__main__":
	unittest.main()
