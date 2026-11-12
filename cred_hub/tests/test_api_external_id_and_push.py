"""Testes unitários para Identidade Universal (external_id), Upsert Inteligente e Porta B de ERPs (credtech#375)."""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Garantir mock do frappe caso o teste rode fora da bench (ex: pytest no Windows local)
if "frappe" not in sys.modules or not hasattr(sys.modules.get("frappe"), "_"):
	frappe_mock = MagicMock()
	frappe_mock._ = lambda msg: msg
	frappe_mock.whitelist = lambda *args, **kwargs: (lambda f: f)
	frappe_mock.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
	frappe_mock.ValidationError = type("ValidationError", (Exception,), {})
	frappe_mock.PermissionError = type("PermissionError", (Exception,), {})
	frappe_mock.flags = types.SimpleNamespace()
	frappe_mock.utils = MagicMock()
	frappe_mock.utils.escape_html = lambda x: str(x)
	frappe_mock.utils.flt = lambda v, p=2: float(v or 0)
	frappe_mock.utils.now_datetime = lambda: "2026-11-12 18:00:00"
	frappe_mock.utils.today = lambda: "2026-11-12"
	sys.modules["frappe"] = frappe_mock
	sys.modules["frappe.utils"] = frappe_mock.utils

if "crm" not in sys.modules:
	crm_mock = MagicMock()
	sys.modules["crm"] = crm_mock
	sys.modules["crm.fcrm"] = MagicMock()
	sys.modules["crm.fcrm.doctype"] = MagicMock()
	sys.modules["crm.fcrm.doctype.crm_deal_title"] = MagicMock()
	sys.modules["crm.fcrm.doctype.crm_deal_title.crm_deal_title"] = MagicMock()
	sys.modules["crm.fcrm.doctype.crm_deal_title.crm_deal_title"].balance_of = lambda row: float(getattr(row, "updated_amount", 0) or getattr(row, "original_amount", 0) or 0)
	sys.modules["crm.permissions"] = MagicMock()
	sys.modules["crm.permissions.pipeline"] = MagicMock()

import cred_hub.erp.sync
from cred_hub.bulk import resolve_record_name
from cred_hub.erp.fields import erp_custom_fields
from cred_hub.erp.push import register_result, settle_titles, upsert_titles


class TestUniversalIdentityAndPush(unittest.TestCase):
	def test_erp_custom_fields_contains_external_id(self):
		fields = erp_custom_fields()
		self.assertIn("CRM Organization", fields)
		self.assertIn("Contact", fields)
		self.assertIn("CRM Deal", fields)

		org_ext = next((f for f in fields["CRM Organization"] if f.get("fieldname") == "external_id"), None)
		self.assertIsNotNone(org_ext)
		self.assertEqual(org_ext["fieldtype"], "Data")
		self.assertEqual(org_ext["search_index"], 1)

		contact_ext = next((f for f in fields["Contact"] if f.get("fieldname") == "external_id"), None)
		self.assertIsNotNone(contact_ext)
		self.assertEqual(contact_ext["fieldtype"], "Data")

		deal_ext = next((f for f in fields["CRM Deal"] if f.get("fieldname") == "external_id"), None)
		self.assertIsNotNone(deal_ext)
		self.assertEqual(deal_ext["fieldtype"], "Data")

	@patch("frappe.db.exists")
	@patch("frappe.db.get_value")
	def test_resolve_record_name_priorities(self, mock_get_value, mock_exists):
		# 1. Match por name se existe
		mock_exists.return_value = True
		matched = resolve_record_name("CRM Organization", {"name": "CRM-ORG-2026-00001"})
		self.assertEqual(matched, "CRM-ORG-2026-00001")

		# 2. Match por external_id se name não existe ou não foi informado
		mock_exists.return_value = False
		mock_get_value.return_value = "CRM-ORG-FOUND-BY-EXT"
		matched = resolve_record_name("CRM Organization", {"external_id": "omie_123"})
		self.assertEqual(matched, "CRM-ORG-FOUND-BY-EXT")
		mock_get_value.assert_called_with("CRM Organization", {"external_id": "omie_123"}, "name")

		# 3. Match por CNPJ para CRM Organization
		mock_get_value.reset_mock()
		mock_get_value.return_value = "CRM-ORG-FOUND-BY-CNPJ"
		matched = resolve_record_name("CRM Organization", {"cnpj": "11222333000181"})
		self.assertEqual(matched, "CRM-ORG-FOUND-BY-CNPJ")
		call_args = mock_get_value.call_args[0]
		self.assertEqual(call_args[0], "CRM Organization")
		self.assertIn("11222333000181", call_args[1]["cnpj"][1])

		# 4. Match por CPF para Contact
		mock_get_value.reset_mock()
		mock_get_value.return_value = "CONT-FOUND-BY-CPF"
		matched = resolve_record_name("Contact", {"cpf": "11144477735"})
		self.assertEqual(matched, "CONT-FOUND-BY-CPF")
		call_args = mock_get_value.call_args[0]
		self.assertEqual(call_args[0], "Contact")
		self.assertIn("11144477735", call_args[1]["cpf"][1])

		# 5. Match por email para Contact
		mock_get_value.reset_mock()
		mock_get_value.return_value = "CONT-FOUND-BY-EMAIL"
		matched = resolve_record_name("Contact", {"email_id": "Financeiro@Acme.com.br"})
		self.assertEqual(matched, "CONT-FOUND-BY-EMAIL")
		mock_get_value.assert_called_with("Contact", {"email_id": "financeiro@acme.com.br"}, "name")

		# 6. Match por external_id para CRM Deal
		mock_get_value.reset_mock()
		mock_get_value.return_value = "CRM-DEAL-00042"
		matched = resolve_record_name("CRM Deal", {"external_id": "contaazul_ped_999"})
		self.assertEqual(matched, "CRM-DEAL-00042")
		mock_get_value.assert_called_with("CRM Deal", {"external_id": "contaazul_ped_999"}, "name")

	@patch("cred_hub.erp.sync._collection_pipeline")
	@patch("cred_hub.erp.sync.upsert_overdue")
	@patch("cred_hub.erp.sync._new_stats")
	@patch("frappe.db.commit")
	@patch("cred_hub.billing.plan_allows", return_value=True)
	def test_upsert_titles_push(self, mock_plan, mock_commit, mock_new_stats, mock_upsert, mock_pipe):
		mock_pipe.return_value = "Cobrança"
		mock_new_stats.return_value = {
			"titles_new": 2, "titles_update": 1, "cards_new": 1, "failed": 0, "skipped": 0,
		}

		payload = [
			{
				"erp_id": "TIT-001",
				"sacado_document": "11222333000181",
				"sacado_name": "Sacado Acme",
				"cedente_document": "43141963000118",
				"due_date": "2026-11-20",
				"amount": 1500.50,
			},
			{
				"id": "TIT-002",
				"debtor_document": "11144477735",
				"debtor_name": "João da Silva",
				"vencimento": "2026-11-25",
				"valor": 300.00,
			},
		]

		result = upsert_titles(payload, erp_source="Omie ERP")
		self.assertTrue(result["ok"])
		self.assertEqual(result["total"], 2)
		self.assertEqual(result["processed"], 2)
		self.assertEqual(result["titles_new"], 2)
		self.assertEqual(result["cards_new"], 1)
		mock_upsert.assert_called_once()
		mock_commit.assert_called_once()

	@patch("cred_hub.erp.sync.close_settled")
	@patch("cred_hub.erp.sync._new_stats")
	@patch("frappe.db.commit")
	@patch("cred_hub.billing.plan_allows", return_value=True)
	def test_settle_titles_push(self, mock_plan, mock_commit, mock_new_stats, mock_close):
		mock_new_stats.return_value = {
			"titles_closed": 3, "cards_won": 1, "cards_to_review": 0, "failed": 0, "skipped": 0,
		}

		payload = [
			{"erp_id": "TIT-001", "outcome": "paid", "paid_amount": 1500.50},
			{"id": "TIT-002", "motivo": "recomprado", "data_pagamento": "2026-11-12"},
		]

		result = settle_titles(payload, erp_source="Conta Azul")
		self.assertTrue(result["ok"])
		self.assertEqual(result["total"], 2)
		self.assertEqual(result["processed"], 2)
		self.assertEqual(result["titles_closed"], 3)
		self.assertEqual(result["cards_won"], 1)
		mock_close.assert_called_once()
		mock_commit.assert_called_once()

	@patch("frappe.db.exists")
	@patch("frappe.db.get_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.commit")
	def test_register_result_updates_card_and_external_id(self, mock_commit, mock_get_doc, mock_get_value, mock_exists):
		mock_exists.return_value = True
		fake_deal = MagicMock(name="CRM-DEAL-2026-00010", organization="ORG-001")
		fake_org = MagicMock(external_id=None)
		fake_org.get.return_value = None

		def get_doc_side_effect(dt, name):
			if dt == "CRM Deal":
				return fake_deal
			if dt == "CRM Organization":
				return fake_org
			return MagicMock()

		mock_get_doc.side_effect = get_doc_side_effect

		result = register_result(
			deal="CRM-DEAL-2026-00010",
			status="success",
			erp_source="TOTVS Protheus",
			erp_customer_id="CLI_TOTVS_888",
			message="Cliente cadastrado no Protheus com sucesso",
		)

		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "success")
		self.assertEqual(result["external_id_linked"], "CLI_TOTVS_888")
		fake_deal.add_comment.assert_called_once()
		self.assertEqual(fake_org.external_id, "CLI_TOTVS_888")
		fake_org.save.assert_called_once()
		mock_commit.assert_called_once()

	def test_curated_templates_list(self):
		from cred_hub.erp.rest_templates import list_curated_templates

		templates = list_curated_templates()
		self.assertGreaterEqual(len(templates), 4)
		ids = {t["id"] for t in templates}
		self.assertIn("asaas", ids)
		self.assertIn("omie", ids)
		self.assertIn("conta_azul", ids)
		self.assertIn("protheus", ids)
		for t in templates:
			self.assertTrue(t["name"])
			self.assertTrue(t["default_base_url"])
			self.assertIn("template", t)
			self.assertTrue(t["template"].get("template"))


if __name__ == "__main__":
	unittest.main()
