"""Cedente registration from the credit funnel (credtech#367): the resolvers that turn CRM data into the ERP's own
ids by name. Pure: `python -m pytest cred_hub/tests/test_erp_cedente.py` (lists shaped like the real Smart answers of
10/11/2026)."""

import unittest

from cred_hub.erp.cedente import (
	FROM_DEFAULT,
	FROM_OWNER,
	cnae_division,
	parse_overrides,
	smart_manager,
	smart_regime,
	smart_sector,
)

# The real sector list of the test client's Smart (10/11/2026), ids made up
SECTORS = [
	{"idSetor": str(i), "descricao": name}
	for i, name in enumerate(
		[
			"Serviços", "Comércio", "Indústria", "Agro-indústria", "Autônomo", "Produtor Rural", "Agro-comércio",
			"Embalagens", "Plástico", "Lubrificantes", "Cosméticos", "Construção", "Transporte/ logistica", "Saúde",
			"Comercio Eletro/ Eletroeletrônico", "Metalurgica", "Comercio de bebiba/ alimento", "Confecção/ Vestuário",
			"Serviço de TI",
		],
		start=260,
	)
]
REGIMES = [
	{"idRegimeTributario": "S", "descricao": "Simples"},
	{"idRegimeTributario": "M", "descricao": "Simples - MEI"},
	{"idRegimeTributario": "P", "descricao": "Presumido"},
	{"idRegimeTributario": "R", "descricao": "Real"},
]
MANAGERS = [
	{"idGerente": "1060026", "descricao": "NF SECURITIZADORA SA"},
	{"idGerente": "11", "descricao": "Gilberto Pilla"},
	{"idGerente": "12", "descricao": "André Gonçalves"},
	{"idGerente": "13", "descricao": "Joao Silva"},
	{"idGerente": "14", "descricao": "João Silva"},
]


def name(sector):
	return sector and sector["descricao"]


class TestSector(unittest.TestCase):
	def test_most_specific_sector_wins(self):
		# Real test of 10/11/2026: CredTech (6204000) went as "Serviços"; the list has "Serviço de TI"
		self.assertEqual(name(smart_sector("6204000 - Consultoria em tecnologia da informação", SECTORS)), "Serviço de TI")
		self.assertEqual(name(smart_sector("4930202 - Transporte rodoviário de carga", SECTORS)), "Transporte/ logistica")
		self.assertEqual(name(smart_sector("8610101 - Atividades de atendimento hospitalar", SECTORS)), "Saúde")
		self.assertEqual(name(smart_sector("4120400 - Construção de edifícios", SECTORS)), "Construção")
		self.assertEqual(name(smart_sector("1412601 - Confecção de peças do vestuário", SECTORS)), "Confecção/ Vestuário")
		# Preference follows the words, not the order of the Smart's list ("Embalagens" comes first there)
		self.assertEqual(name(smart_sector("2229399 - Fabricação de artefatos de material plástico", SECTORS)), "Plástico")
		self.assertEqual(name(smart_sector("0115600 - Cultivo de soja", SECTORS)), "Produtor Rural")
		# Food industry is not "Comércio de bebida/alimento": it stays Indústria unless the company overrides
		self.assertEqual(name(smart_sector("1091101 - Fabricação de produtos de panificação", SECTORS)), "Indústria")

	def test_generic_section_when_nothing_specific(self):
		self.assertEqual(name(smart_sector("6422100 - Bancos múltiplos, com carteira comercial", SECTORS)), "Serviços")
		self.assertEqual(name(smart_sector("4751201 - Comércio varejista de informática", SECTORS)), "Comércio")
		self.assertEqual(name(smart_sector("2930101 - Fabricação de cabines", SECTORS)), "Indústria")

	def test_company_override_by_division(self):
		overrides = parse_overrides("62 = Serviços\n\n49=Serviços\nlixo sem igual")
		self.assertEqual(overrides, {62: "Serviços", 49: "Serviços"})
		self.assertEqual(name(smart_sector("6204000", SECTORS, overrides)), "Serviços")
		# An override that names no Smart sector finds nothing (the card gets the "missing" note, never a guess)
		self.assertIsNone(smart_sector("6204000", SECTORS, {62: "Não existe"}))

	def test_no_cnae_no_sector(self):
		self.assertIsNone(smart_sector("", SECTORS))
		self.assertIsNone(cnae_division(None))
		self.assertEqual(cnae_division("0111-3/01"), 1)


class TestManager(unittest.TestCase):
	def test_card_owner_by_name_ignoring_accents_and_case(self):
		manager, source = smart_manager("GILBERTO PILLA", "NF SECURITIZADORA SA", MANAGERS)
		self.assertEqual((manager["idGerente"], source), ("11", FROM_OWNER))
		manager, _ = smart_manager("andre goncalves", "", MANAGERS)
		self.assertEqual(manager["idGerente"], "12")

	def test_default_manager_when_the_owner_is_not_one(self):
		manager, source = smart_manager("Vendedor Novo", "NF Securitizadora S.A", MANAGERS)
		self.assertIsNone(manager)  # "S.A" != "SA": the default must be written as in the Smart
		manager, source = smart_manager("Vendedor Novo", "NF SECURITIZADORA SA", MANAGERS)
		self.assertEqual((manager["idGerente"], source), ("1060026", FROM_DEFAULT))

	def test_ambiguous_owner_name_falls_back_to_the_default(self):
		# "Joao Silva" and "João Silva" are two Smart managers: never pick one at random
		_manager, source = smart_manager("João Silva", "NF SECURITIZADORA SA", MANAGERS)
		self.assertEqual(source, FROM_DEFAULT)

	def test_nothing_matches(self):
		self.assertEqual(smart_manager("", "", MANAGERS), (None, ""))


class TestRegime(unittest.TestCase):
	def test_receita_regime_to_smart_letter(self):
		self.assertEqual(smart_regime("Lucro Real", REGIMES)["idRegimeTributario"], "R")
		self.assertEqual(smart_regime("Lucro Presumido", REGIMES)["idRegimeTributario"], "P")
		self.assertEqual(smart_regime("Simples Nacional", REGIMES)["idRegimeTributario"], "S")
		self.assertEqual(smart_regime("MEI", REGIMES)["idRegimeTributario"], "M")
		self.assertIsNone(smart_regime("", REGIMES))
		self.assertIsNone(smart_regime("Lucro Real", []))
