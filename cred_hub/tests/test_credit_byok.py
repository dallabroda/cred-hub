"""Chave do próprio cliente em todos os bureaus e trava de pré-aprovação virando aviso (credtech#316).
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_byok`. Nenhuma
chamada sai do servidor: os provedores são simulados."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import billing
from cred_hub.credit_check import api, enrich
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS

SETTINGS = "Cred Hub Credit Settings"
CNPJ = "11.222.333/0001-81"


def _clean(data):
	return dict(data)


class TestCreditClientKeys(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"enabled": 1,
				"cgu_enabled": 0,
				"escavador_enabled": 0,
				"infosimples_enabled": 1,
				"infosimples_token": "infosimples-client-token",
				"directd_enabled": 1,
				"directd_token": "directd-client-token",
				"directd_fgts_enabled": 0,
				"directd_sintegra_enabled": 0,
				"min_directd_score": 0,
				"auto_trigger_stage": "",
				"serasa_enabled": 0,
				"boavista_risco_enabled": 1,
				"dossie_quod_enabled": 0,
				"scr_bacen_enabled": 1,
			}
		)
		settings.save()
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Exemplo Crédito BYOK Teste", "cnpj": CNPJ}
		).insert()
		self.balance_before = billing.wallet_balance()

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _patch_automatic_providers(self):
		return (
			patch.object(enrich, "fetch_fiscal_and_protests", return_value=_clean(enrich.EMPTY_INFOSIMPLES_DATA)),
			patch.object(enrich, "fetch_score", return_value=_clean(enrich.EMPTY_DIRECTD_SCORE_DATA)),
			patch.object(enrich, "fetch_protests_nacional", return_value=_clean(enrich.EMPTY_DIRECTD_PROTESTS_DATA)),
			patch.object(enrich, "fetch_lawsuits_directd", return_value=_clean(enrich.EMPTY_ESCAVADOR_DATA)),
		)

	def test_pipeline_uses_client_tokens_and_never_charges_the_wallet(self):
		p_info, p_score, p_protests, p_lawsuits = self._patch_automatic_providers()
		with p_info as info, p_score as score, p_protests as protests, p_lawsuits as lawsuits:
			report_name = enrich.run_background_check(organization_name=self.org.name, force=True)

		self.assertEqual(info.call_args.args[1], "infosimples-client-token")
		for mocked in (score, protests, lawsuits):
			self.assertEqual(mocked.call_args.args[1], "directd-client-token")

		report = frappe.get_doc("Cred Hub Credit Report", report_name)
		self.assertEqual(report.verdict, "Pre-Approved")
		self.assertEqual(report.client_total_cost, 0)
		expected_list_price = (
			enrich.INFOSIMPLES_ESTIMATED_COST
			+ enrich.DIRECTD_SCORE_ESTIMATED_COST
			+ enrich.DIRECTD_PROTESTS_ESTIMATED_COST
			+ enrich.DIRECTD_PROCESSOS_ESTIMATED_COST
		)
		self.assertAlmostEqual(report.total_cost, expected_list_price, places=2)
		self.assertEqual(billing.wallet_balance(), self.balance_before)
		self.assertFalse(hasattr(enrich, "spend_credits"))

	def test_enabled_provider_without_client_token_fails_closed(self):
		frappe.db.set_single_value(SETTINGS, "directd_token", "")
		frappe.db.delete("__Auth", {"doctype": SETTINGS, "fieldname": "directd_token"})
		frappe.clear_document_cache(SETTINGS, SETTINGS)

		p_info, p_score, p_protests, p_lawsuits = self._patch_automatic_providers()
		with p_info, p_score as score, p_protests, p_lawsuits:
			report_name = enrich.run_background_check(organization_name=self.org.name, force=True)

		score.assert_not_called()
		report = frappe.get_doc("Cred Hub Credit Report", report_name)
		self.assertEqual(report.verdict, "Error")
		self.assertIn("DirectD Score (QUOD): no API token configured", report.raw_details_json)

	def test_final_bureau_options_show_list_price_and_pre_filter_alert(self):
		self.org.db_set("credit_verdict", "Warning")
		options = api.get_final_bureau_options(organization_name=self.org.name)

		self.assertTrue(options["needs_acknowledgement"])
		self.assertTrue(options["can_request"])
		self.assertEqual([item["key"] for item in options["bureaus"]], ["boavista_risco", "scr_bacen"])
		scr = options["bureaus"][1]
		self.assertEqual(scr["provider"], "DirectD")
		self.assertEqual(scr["price_reference"], PREMIUM_BUREAUS["scr_bacen"].price_reference)
		self.assertTrue(scr["configured"])

	def test_final_bureau_with_alert_needs_cost_and_alert_confirmation(self):
		self.org.db_set("credit_verdict", "Warning")
		with patch.object(PREMIUM_BUREAUS["scr_bacen"], "fetch", return_value={"raw": {"ok": 1}}) as fetch:
			with self.assertRaises(frappe.ValidationError):
				api.request_bureau_final(organization_name=self.org.name, bureau="scr_bacen")
			with self.assertRaises(frappe.ValidationError):
				api.request_bureau_final(organization_name=self.org.name, bureau="scr_bacen", confirm_cost=1)
			fetch.assert_not_called()

			result = api.request_bureau_final(
				organization_name=self.org.name, bureau="scr_bacen", confirm_cost=1, acknowledged_warning=1
			)

		self.assertEqual(fetch.call_args.args[1], "directd-client-token")
		self.assertTrue(result["warning_acknowledged"])
		log = frappe.get_last_doc("Cred Hub Premium Bureau Query", filters={"organization": self.org.name})
		self.assertEqual(log.bureau, "scr_bacen")
		self.assertEqual(log.pre_filter_verdict, "Warning")
		self.assertEqual(log.warning_acknowledged, 1)
		self.assertEqual(log.client_price, 0)
		self.assertEqual(log.cost, PREMIUM_BUREAUS["scr_bacen"].price_reference)
		comment = frappe.get_last_doc(
			"Comment", filters={"reference_doctype": "CRM Organization", "reference_name": self.org.name}
		)
		self.assertIn("pre-filter alert", comment.content)
		self.assertEqual(billing.wallet_balance(), self.balance_before)

	def test_pre_approved_needs_only_cost_confirmation(self):
		self.org.db_set("credit_verdict", "Pre-Approved")
		with patch.object(PREMIUM_BUREAUS["boavista_risco"], "fetch", return_value={"raw": {}}):
			result = api.request_bureau_final(organization_name=self.org.name, bureau="boavista_risco", confirm_cost=1)
		self.assertFalse(result["warning_acknowledged"])
		log = frappe.get_last_doc("Cred Hub Premium Bureau Query", filters={"organization": self.org.name})
		self.assertEqual(log.warning_acknowledged, 0)

	def test_disabled_bureau_is_refused(self):
		self.org.db_set("credit_verdict", "Pre-Approved")
		with patch.object(PREMIUM_BUREAUS["dossie_quod"], "fetch") as fetch, self.assertRaises(frappe.ValidationError):
			api.request_bureau_final(
				organization_name=self.org.name, bureau="dossie_quod", confirm_cost=1, acknowledged_warning=1
			)
		fetch.assert_not_called()

	def test_price_references_cover_every_paid_query(self):
		prices = api.get_price_references()
		for key in ("escavador", "infosimples", "directd_score", "boavista_risco", "dossie_quod", "scr_bacen"):
			self.assertGreater(prices[key], 0)
		self.assertEqual(set(prices["test_connection"]), {"escavador", "infosimples", "directd"})
