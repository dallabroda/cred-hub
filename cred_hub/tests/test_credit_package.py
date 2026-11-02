"""Pacote mínimo do dossiê de crédito (credtech#316): o que todo dossiê precisa consultar, o custo mostrado antes e
o trabalho em segundo plano que consulta, gera o PDF 1 e o parecer. Provedores e Gemini simulados.
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_package`."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from cred_hub.credit_check import api, opinion, package
from cred_hub.credit_check import dossier as credit_dossier
from cred_hub.credit_check.clients.directd import DirectDError
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS
from cred_hub.tests.test_credit_opinion import VERTEX, FakeVertex, good_opinion

CNPJ = "11.222.333/0001-81"
CPF = "529.982.247-25"
SETTINGS = "Cred Hub Credit Settings"


class TestCreditPackage(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"enabled": 1,
				"directd_enabled": 1,
				"directd_token": "directd-client-token",
				"serasa_enabled": 0,
				"boavista_risco_enabled": 0,
				"dossie_quod_enabled": 1,
				"scr_bacen_enabled": 1,
				"auto_trigger_stage": "",
			}
		)
		settings.save()
		frappe.db.set_single_value(SETTINGS, package.FIELD, "")
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Exemplo Pacote Teste", "cnpj": CNPJ}
		).insert()
		self.org.db_set("credit_verdict", "Pre-Approved")
		self.enqueue = patch("cred_hub.credit_check.api.frappe.enqueue")
		self.enqueue_mock = self.enqueue.start()
		self.job = patch("cred_hub.credit_check.api.is_job_enqueued", return_value=False)
		self.job.start()
		# o trabalho grava cada consulta paga na hora; no teste, tudo volta no rollback
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()

	def tearDown(self):
		self.commit.stop()
		self.job.stop()
		self.enqueue.stop()
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _query(self, bureau, days_ago=0, status=package.DONE, document=CNPJ, contact=None, raw=None):
		api._log_premium_bureau_query(
			bureau, self.org.name, None, CNPJ, 1.0, status, raw or {"ok": 1}, "Pre-Approved", False, document=document, contact_name=contact
		)
		row = frappe.get_last_doc("Cred Hub Premium Bureau Query", filters={"organization": self.org.name})
		row.db_set("requested_on", add_days(now_datetime(), -days_ago))
		return row

	def _partner(self, name, cpf=""):
		contact = frappe.get_doc({"doctype": "Contact", "first_name": name, "company_name": self.org.name})
		if cpf:
			contact.cpf = cpf
		contact.flags.ignore_mandatory = True
		return contact.insert()

	def _states(self, current):
		return {(entry["item"], entry["subject_name"]): entry["state"] for entry in current["items"]}

	def test_default_package_is_company_quod_and_scr_reusing_30_days(self):
		conf = package.config()
		self.assertEqual(conf["items"], ["company:dossie_quod", "company:scr_bacen"])
		self.assertEqual(conf["reuse_days"], 30)
		self.assertTrue(conf["is_default"])

	def test_save_validates_and_keeps_a_fixed_order(self):
		with self.assertRaises(frappe.ValidationError):
			package.save(["company:nada"], 30)
		with self.assertRaises(frappe.ValidationError):
			package.save(["partners:dossie_quod"], 30)  # sem nenhuma consulta da empresa
		with self.assertRaises(frappe.ValidationError):
			package.save(["company:scr_bacen"], 400)
		saved = package.save(json.dumps(["partners:scr_bacen", "company:scr_bacen", "company:scr_bacen"]), "15")
		self.assertEqual(saved["items"], ["company:scr_bacen", "partners:scr_bacen"])
		self.assertEqual(saved["reuse_days"], 15)
		self.assertFalse(saved["is_default"])

	def test_status_reuses_recent_queries_and_prices_what_is_missing(self):
		self._query("scr_bacen", days_ago=5)
		self._query("dossie_quod", days_ago=40)  # fora do prazo de 30 dias
		current = package.status(self.org.name)
		states = self._states(current)
		self.assertEqual(states[("company:scr_bacen", "Exemplo Pacote Teste")], "reused")
		self.assertEqual(states[("company:dossie_quod", "Exemplo Pacote Teste")], "missing")
		self.assertEqual(current["missing_cost"], PREMIUM_BUREAUS["dossie_quod"].price_reference)
		self.assertFalse(current["ready"])

	def test_failed_query_is_not_reused_and_shows_the_reason(self):
		self._query("scr_bacen", status=package.FAILED, raw={"error": "DirectD fora do ar"})
		entry = next(e for e in package.status(self.org.name)["items"] if e["bureau"] == "scr_bacen")
		self.assertEqual(entry["state"], "missing")
		self.assertEqual(entry["last_error"], "DirectD fora do ar")

	def test_bureau_off_or_without_key_is_unavailable_and_blocks_generation(self):
		package.save(["company:boavista_risco", "company:scr_bacen"], 30)
		current = package.status(self.org.name)
		self.assertEqual(self._states(current)[("company:boavista_risco", "Exemplo Pacote Teste")], "unavailable")
		with self.assertRaises(frappe.ValidationError):
			api.generate_credit_package(self.org.name, confirm_cost=1)
		self.enqueue_mock.assert_not_called()

	def test_partners_with_cpf_enter_the_package_and_the_others_are_listed(self):
		self._partner("Maria Exemplo", CPF)
		self._partner("Joao Sem CPF")
		package.save(["company:scr_bacen", "partners:dossie_quod"], 30)
		current = package.status(self.org.name)
		partner_rows = [e for e in current["items"] if e["subject_type"] == "Partner"]
		self.assertEqual([e["subject_name"] for e in partner_rows], ["Maria Exemplo"])
		self.assertEqual(partner_rows[0]["document"], "52998224725")
		self.assertEqual(current["partners_without_cpf"], ["Joao Sem CPF"])

	def test_generate_needs_cost_confirmation_and_any_user_can_run_the_paid_queries(self):
		with self.assertRaises(frappe.ValidationError):
			api.generate_credit_package(self.org.name)
		self.enqueue_mock.assert_not_called()
		with patch.object(frappe, "get_roles", return_value=["Sales User"]):
			result = api.generate_credit_package(self.org.name, confirm_cost=1)
		self.assertEqual(result["queries"], 2)
		self.assertEqual(self.enqueue_mock.call_args.args[0], "cred_hub.credit_check.package.run_job")

	def test_generate_with_alert_needs_acknowledgement(self):
		self.org.db_set("credit_verdict", "Warning")
		with self.assertRaises(frappe.ValidationError):
			api.generate_credit_package(self.org.name, confirm_cost=1)
		api.generate_credit_package(self.org.name, confirm_cost=1, acknowledged_warning=1)
		self.assertTrue(self.enqueue_mock.call_args.kwargs["warning_acknowledged"])

	def test_complete_package_needs_no_confirmation_and_any_user_can_generate(self):
		self._query("scr_bacen", days_ago=1)
		self._query("dossie_quod", days_ago=1)
		with patch.object(frappe, "get_roles", return_value=["Sales User"]):
			result = api.generate_credit_package(self.org.name)
		self.assertEqual(result["queries"], 0)
		self.assertEqual(self.enqueue_mock.call_args.args[0], "cred_hub.credit_check.package.run_job")

	def test_generate_reserves_the_opinion_or_says_why_not(self):
		with patch.dict(frappe.local.conf, {"credhub_vertex": VERTEX}):
			result = api.generate_credit_package(self.org.name, confirm_cost=1)
		self.assertTrue(result["opinion"])
		self.assertEqual(frappe.db.get_value(opinion.DOCTYPE, result["opinion"], "status"), "Queued")
		self.assertEqual(self.enqueue_mock.call_args.kwargs["opinion_name"], result["opinion"])
		self.assertEqual(self.enqueue_mock.call_count, 1)  # o parecer roda dentro do trabalho do pacote

		with patch.dict(frappe.local.conf, {"credhub_vertex": {}}):
			result = api.generate_credit_package(self.org.name, confirm_cost=1)
		self.assertIsNone(result["opinion"])
		self.assertIn("not configured", result["opinion_skipped"])

	def test_job_runs_missing_queries_records_failures_and_generates_both_pdfs(self):
		with patch.dict(frappe.local.conf, {"credhub_vertex": VERTEX}):
			opinion_name = opinion.create(self.org.name, None, False, enqueue=False)["name"]
			fake = FakeVertex(good_opinion())
			with (
				patch.object(PREMIUM_BUREAUS["dossie_quod"], "fetch", return_value={"raw": {"score": 812}}) as quod,
				patch.object(PREMIUM_BUREAUS["scr_bacen"], "fetch", side_effect=DirectDError("DirectD fora do ar")),
				patch.object(opinion, "vertex_client", return_value=fake),
			):
				package.run_job(self.org.name, None, "Pre-Approved", False, opinion_name)

		self.assertEqual(quod.call_args.args, (CNPJ, "directd-client-token"))
		rows = {
			row.bureau: row
			for row in frappe.get_all(
				"Cred Hub Premium Bureau Query", filters={"organization": self.org.name}, fields=["bureau", "status", "cost", "raw_response_json"]
			)
		}
		self.assertEqual(rows["dossie_quod"].status, package.DONE)
		self.assertEqual(rows["dossie_quod"].cost, PREMIUM_BUREAUS["dossie_quod"].price_reference)
		self.assertEqual(rows["scr_bacen"].status, package.FAILED)
		self.assertEqual(rows["scr_bacen"].cost, 0)
		self.assertIn("DirectD fora do ar", rows["scr_bacen"].raw_response_json)

		self.assertTrue(credit_dossier.list_files(self.org.name))  # PDF 1
		self.assertEqual(frappe.db.get_value(opinion.DOCTYPE, opinion_name, "status"), "Done")  # PDF 2
		sent = json.loads(fake.calls[0]["user"].split("<dados>")[1].split("</dados>")[0])
		states = {item["query"]: item["state"] for item in sent["minimum_package"]}
		self.assertEqual(states["Dossiê QUOD - Completo"], "done")
		self.assertEqual(states["SCR Analítico - Resumo BACEN"], "failed")
		comment = frappe.get_all(
			"Comment", filters={"reference_name": self.org.name, "comment_type": "Comment"}, pluck="content", order_by="creation desc"
		)
		self.assertTrue(any("1 queries made, 1 failed" in text or "1 consultas feitas" in text for text in comment))

	def test_reuse_zero_means_always_query_again_but_the_opinion_sees_the_fresh_queries(self):
		package.save(["company:scr_bacen"], 0)
		requested = add_days(now_datetime(), -1)
		self._query("scr_bacen", days_ago=0)
		self.assertEqual(package.status(self.org.name)["items"][0]["state"], "missing")
		self.assertEqual(package.for_ai(self.org.name, as_of=requested)[0]["state"], "done")

	def test_job_failure_before_the_opinion_marks_it_failed(self):
		with patch.dict(frappe.local.conf, {"credhub_vertex": VERTEX}):
			opinion_name = opinion.create(self.org.name, None, False, enqueue=False)["name"]
		with (
			patch.object(PREMIUM_BUREAUS["dossie_quod"], "fetch", return_value={"raw": {}}),
			patch.object(PREMIUM_BUREAUS["scr_bacen"], "fetch", return_value={"raw": {}}),
			patch.object(credit_dossier, "generate", side_effect=RuntimeError("wkhtmltopdf quebrou")),
			self.assertRaises(RuntimeError),
		):
			package.run_job(self.org.name, None, "Pre-Approved", False, opinion_name)
		self.assertEqual(frappe.db.get_value(opinion.DOCTYPE, opinion_name, "status"), "Failed")
