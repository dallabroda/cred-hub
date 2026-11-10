"""Field-mapping engine and formats (credtech#367). Pure: `python -m pytest cred_hub/tests/test_erp_mapper.py`."""

import unittest
from datetime import date
from decimal import Decimal

from cred_hub.erp import formats, mapper
from cred_hub.erp.mapper import DEFAULT_RULES, Rule, build, finalize

RECORDS = {
	"organization": {
		"organization_name": "Carizon", "legal_name": "CARIZON ENGENHARIA LTDA", "trade_name": "Carizon",
		"cnpj": "43.141.963/0001-18", "registry_email": "Financeiro@Carizon.com.br", "registry_phone": "(11) 4445-4444",
		"neighborhood": "Bela Vista", "activity_start_date": date(2010, 5, 3), "share_capital": 150000,
		"main_cnae": "4120-4/00",
	},
	"address": {
		"address_line1": "Avenida Paulista, 1000", "address_line2": "Andar 10", "city": "São Paulo", "state": "sp",
		"pincode": "01310-100", "ibge_code": "3550308",
	},
	"contact": {"full_name": "João Silva", "email_id": "joao@carizon.com.br", "mobile_no": "+55 (11) 98888-7777"},
	"deal": {"deal_value": 150000},
}


class TestFormats(unittest.TestCase):
	def test_money(self):
		self.assertEqual(formats.to_decimal("1.234,56"), Decimal("1234.56"))
		self.assertEqual(formats.to_decimal("4471.18"), Decimal("4471.18"))
		self.assertEqual(formats.to_decimal("1.234.567"), Decimal(1234567))
		self.assertIsNone(formats.to_decimal(""))
		self.assertEqual(formats.br_money(1587.65), "1.587,65")
		self.assertEqual(formats.br_money("1234567.891"), "1.234.567,89")
		self.assertEqual(formats.br_money(0), "0,00")

	def test_dates(self):
		self.assertEqual(formats.to_date("05/11/2026"), date(2026, 11, 5))
		self.assertEqual(formats.to_date("05/11/2026 10:20:30"), date(2026, 11, 5))
		self.assertEqual(formats.to_date("2026-11-05"), date(2026, 11, 5))
		self.assertIsNone(formats.to_date("00/00/0000"))
		self.assertEqual(formats.br_date(date(2026, 1, 2)), "02/01/2026")

	def test_phone(self):
		self.assertEqual(formats.split_phone("+55 (11) 98888-7777"), ("11", "988887777"))
		self.assertEqual(formats.split_phone("1144454444"), ("11", "44454444"))
		self.assertEqual(formats.split_phone("4444"), ("", "4444"))

	def test_address_helpers(self):
		self.assertEqual(formats.street_name("Rua das Flores, 100"), "Rua das Flores")
		self.assertEqual(formats.street_number("Rua das Flores, 100"), "100")
		self.assertEqual(formats.street_number("Rua sem número"), "")
		self.assertEqual(formats.ascii_text("Av. São João (fundos) - 1/2"), "Av Sao Joao 1 2")

	def test_keys(self):
		self.assertEqual(formats.normalize_key("E-Mail"), "e_mail")
		self.assertEqual(formats.normalize_key("ENDEREÇO"), "endereco")


class TestMapper(unittest.TestCase):
	def test_paths_build_nested_objects_and_lists(self):
		payload = {}
		mapper.set_path(payload, "endereco.cep", "1")
		mapper.set_path(payload, "telefones[0].ddd", "11")
		mapper.set_path(payload, "telefones[1].ddd", "21")
		self.assertEqual(payload, {"endereco": {"cep": "1"}, "telefones": [{"ddd": "11"}, {"ddd": "21"}]})

	def test_default_fallback_and_required(self):
		rules = [
			Rule("a", "organization.missing", default="X"),
			Rule("b", "organization.missing", required=True, label="B"),
			Rule("c", "organization.missing|organization.trade_name"),
			Rule("d", "organization.missing"),
		]
		result = build(rules, RECORDS)
		self.assertEqual(result.payload, {"a": "X", "c": "Carizon"})
		self.assertEqual([r.erp_field for r in result.missing], ["b"])
		self.assertFalse(result.ok)

	def test_unknown_transform_is_reported(self):
		result = build([Rule("a", "organization.cnpj", transform="nope")], RECORDS)
		self.assertTrue(result.errors)

	def test_custom_field_source(self):
		records = {**RECORDS, "deal": {"custom_floating": "3"}}
		result = build([Rule("cedente.floatingMinimo", "deal.custom_floating", "int")], records)
		self.assertEqual(result.payload, {"cedente": {"floatingMinimo": 3}})

	def test_wba_defaults(self):
		result = build(DEFAULT_RULES["WBA"], RECORDS)
		self.assertTrue(result.ok, [r.erp_field for r in result.missing] + result.errors)
		person = finalize("WBA", result.payload)[0]
		self.assertEqual(person["documento"], "43141963000118")
		self.assertEqual(person["tipoPessoa"], "PJ")
		self.assertIs(person["cadastroAtivo"], True)
		self.assertEqual(person["pessoaJuridica"]["razaoSocial"], "CARIZON ENGENHARIA LTDA")
		self.assertEqual(person["cedente"], {"limite": 150000.0, "floatingMinimo": 1})
		self.assertIsNone(person["sacado"])
		self.assertEqual(person["celular"], {"ddd": 11, "numero": 988887777})
		self.assertEqual(person["telefone"], {"ddd": 11, "numero": 44454444})
		self.assertEqual(person["email"], "financeiro@carizon.com.br")
		self.assertEqual(
			person["endereco"],
			{"cep": "01310100", "endereco": "Avenida Paulista", "numero": "1000", "complemento": "Andar 10",
			 "bairro": "Bela Vista", "cidade": "São Paulo", "uf": "SP", "codigoIbge": "3550308"},
		)

	def test_smart_defaults_need_sector_and_manager(self):
		result = build(DEFAULT_RULES["Smart Securities"], RECORDS, today=date(2026, 11, 10))
		# Smart IDs have no CRM source: the company sets them as defaults in the settings
		self.assertEqual(sorted(r.erp_field for r in result.missing), ["idGerente", "idSetor"])
		self.assertEqual(result.auto, ["numeroContratoMae"])

	def test_smart_payload_formats(self):
		rules = [dict(r) for r in DEFAULT_RULES["Smart Securities"]]
		for rule in rules:
			if rule["erp_field"] in ("idSetor", "idGerente"):
				rule["default_value"] = "4"
		result = build(rules, RECORDS, today=date(2026, 11, 10))
		self.assertTrue(result.ok, result.errors)
		payload = finalize("Smart Securities", result.payload)
		self.assertEqual(payload["cpfCnpj"], "43141963000118")
		self.assertEqual(payload["clienteDesde"], "10/11/2026")
		self.assertEqual(payload["cepEnderecoPrincipal"], 1310100)  # JSON int, leading zero gone
		self.assertEqual(payload["capitalSocial"], "150.000,00")
		self.assertEqual(payload["dataFundacao"], "03/05/2010")
		self.assertEqual(payload["cnae"], "4120400")
		self.assertEqual(payload["idSetor"], 4)
		self.assertEqual(payload["logradouroEnderecoPrincipal"], "Avenida Paulista")
		self.assertEqual(payload["codigoMunicipioIbgeEnderecoPrincipal"], 3550308)
		self.assertEqual(payload["telefones"], [{"ddd": "11", "numero": "988887777", "nomeContato": "Joao Silva"}])

	def test_smart_street_without_number_gets_1(self):
		records = {**RECORDS, "address": {**RECORDS["address"], "address_line1": "Rodovia BR-101 Km 12"}}
		rules = [Rule("numeroEnderecoPrincipal", "address.address_line1", "street_number", "1", True)]
		payload = finalize("Smart Securities", build(rules, records).payload)
		self.assertEqual(payload["numeroEnderecoPrincipal"], "1")

	def test_smart_drops_incomplete_phones(self):
		payload = finalize("Smart Securities", {"telefones": [{"ddd": "", "numero": "4444"}]})
		self.assertNotIn("telefones", payload)


if __name__ == "__main__":
	unittest.main()
