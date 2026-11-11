"""Full de-para (credtech#367): every ERP field and column offered, converted to the CRM field's type. Pure Python:
`python -m pytest cred_hub/tests/test_erp_import_map.py`."""

import json
import unittest
from datetime import date

from cred_hub.erp import catalog, import_map
from cred_hub.erp.mapper import DEFAULT_RULES, ERP_LIST, build


class TestCatalog(unittest.TestCase):
	def test_every_smart_cedente_field_is_offered(self):
		names = {f["erp_field"] for f in catalog.fields("Smart Securities")}
		for field in (
			"inscricaoEstadual", "idOperador", "cpfCnpjCaptador", "idControlador", "idResponsavelCobranca",
			"idFonteCaptacao", "idGrupoAnaliseVadu", "dataNascimento", "idEstadoCivil", "cepEnderecoCobranca",
			"codigoMunicipioIbgeEnderecoCobranca", "telefones[1].numero",
		):
			self.assertIn(field, names)

	def test_defaults_use_the_catalog_format(self):
		for provider, rules in DEFAULT_RULES.items():
			for rule in rules:
				spec = catalog.field(provider, rule["erp_field"])
				self.assertIsNotNone(spec, rule["erp_field"])
				self.assertEqual(spec["transform"], rule["transform"], rule["erp_field"])
				self.assertEqual(spec["required"] == catalog.REQUIRED, bool(rule["required"]), rule["erp_field"])


class TestListFields(unittest.TestCase):
	def test_a_list_name_is_left_for_the_sending_step(self):
		rules = [
			{"erp_field": "idOperador", "source": ERP_LIST, "default_value": "Maria Operadora"},
			{"erp_field": "idCaptador", "source": ERP_LIST, "default_value": ""},
		]
		result = build(rules, {})
		self.assertEqual(result.lists, [("idOperador", "Maria Operadora")])
		self.assertEqual(result.payload, {})
		self.assertTrue(result.ok)


TYPES = {"updated_amount": "Currency", "issue_date": "Date", "erp_title_type": "Data", "species": "Select", "days": "Int"}


class TestImportMap(unittest.TestCase):
	def test_titulo_columns_go_to_the_chosen_fields_with_their_type(self):
		row = {"TIPO": " DMR ", "VALOR_TOTAL": "15.487,32", "DATA_EMISSAO": "18/02/2024", "PRAZO": "30 dias"}
		rules = [
			{"erp_column": "TIPO", "crm_field": "erp_title_type"},
			{"erp_column": "VALOR_TOTAL", "crm_field": "updated_amount"},
			{"erp_column": "DATA_EMISSAO", "crm_field": "issue_date"},
			{"erp_column": "PRAZO", "crm_field": "days"},
		]
		self.assertEqual(
			import_map.title_values(row, rules, TYPES),
			{"erp_title_type": "DMR", "updated_amount": 15487.32, "issue_date": date(2024, 2, 18), "days": 30},
		)

	def test_protest_species_is_never_filled_from_the_erp(self):
		rules = [{"erp_column": "TIPO", "crm_field": "species"}]
		self.assertEqual(import_map.title_values({"TIPO": "DMI"}, rules, TYPES), {})

	def test_empty_columns_change_nothing(self):
		rules = [{"erp_column": "VALOR_TOTAL", "crm_field": "updated_amount"}]
		self.assertEqual(import_map.title_values({"VALOR_TOTAL": ""}, rules, TYPES), {})

	def test_sacado_columns_match_any_spelling(self):
		row = {"E-Mail": "FIN@SACADO.COM.BR", "Telefone": "(11) 98765-4321", "Grupo Econômico": "Grupo X"}
		rules = [
			{"erp_column": "E-mail", "crm_field": "organization.registry_email"},
			{"erp_column": "Grupo Economico", "crm_field": "organization.economic_group"},
			{"erp_column": "Telefone", "crm_field": "contact.mobile_no"},
		]
		org = import_map.sacado_values(row, rules, "organization", {"registry_email": "Data", "economic_group": "Data"})
		self.assertEqual(org, {"registry_email": "FIN@SACADO.COM.BR", "economic_group": "Grupo X"})
		contact = import_map.sacado_values(row, rules, "contact", {"mobile_no": "Data"})
		self.assertEqual(contact, {"mobile_no": "(11) 98765-4321"})

	def test_columns_seen_in_the_erp_are_offered_once(self):
		raw, changed = import_map.merge_columns(None, import_map.TITLE, [{"VALOR": 1, "PRAZO_MEDIO": 2}])
		self.assertTrue(changed)
		raw, changed = import_map.merge_columns(raw, import_map.TITLE, [{"VALOR": 1}])
		self.assertFalse(changed)
		offered = import_map.columns(raw, import_map.TITLE, "Smart Securities")
		self.assertIn("PRAZO_MEDIO", offered)
		self.assertEqual(offered.count("VALOR"), 1)
		self.assertEqual(json.loads(raw)["title"], ["VALOR", "PRAZO_MEDIO"])


if __name__ == "__main__":
	unittest.main()
