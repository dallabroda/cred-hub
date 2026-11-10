"""ERP -> Collection sync (credtech#367) with a fake Smart Securities API shaped like the real one (probed 10/11/2026).
Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_erp_sync`."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from cred_hub.collection import importer
from cred_hub.erp import settings as erp_settings
from cred_hub.erp import sync
from cred_hub.erp.fields import PAID, REPURCHASED
from cred_hub.integrations import SETTINGS as PROVIDER_SETTINGS
from cred_hub.party.roles import CEDENTE, DEBTOR, FIELD

DEBTOR_A, DEBTOR_B = "11222333000181", "11444777000161"
CEDENTE_A, CEDENTE_B = "45723174000110", "33445566000186"

def masked(cnpj):
	return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"

def br(days):
	return getdate(add_days(today(), days)).strftime("%d/%m/%Y")

def open_row(title_id, number, debtor, cedente, days=-10, value="1.000,00", situation="Aberto"):
	return {
		"ID_TITULO": str(title_id), "DOCUMENTO": number, "TIPO": "DMR", "CPF_CNPJ_SACADO": masked(debtor),
		"SACADO": f"SACADO {debtor[:4]} LTDA", "CPF_CNPJ_CEDENTE": masked(cedente), "CEDENTE": f"CEDENTE {cedente[:4]} LTDA",
		"DATA_EMISSAO": br(-60), "ORIGINAL": br(days), "VENCIMENTO": br(days), "SITUACAO": situation, "OP": "13594",
		"VALOR": value, "LINHA_DIGITAVEL": "", "NOSSO_NUMERO": "",
	}

def paid_row(title_id, number, debtor, situation="Quitado", received="1.020,00", days=-1):
	return {
		"ID_TITULO": int(title_id), "NUMERO": number, "CPF_CNPJ_SACADO": masked(debtor), "QUITACAO": br(days),
		"VALOR_LIQUIDADO": received, "VALOR_FACE_": "1.000,00", "SITUACAO": situation, "OP": 13594,
		"VENCIMENTO": br(-10),
	}

class Response:
	def __init__(self, status, body):
		self.status_code = status
		self._body = body
		self.text = json.dumps(body)

	def json(self):
		return self._body

class FakeSmart:
	"""Smart Securities by report type; records the queries."""

	def __init__(self, open_rows=(), paid_rows=(), fail_paid=False):
		self.open_rows, self.paid_rows, self.fail_paid = list(open_rows), list(paid_rows), fail_paid
		self.queries = []

	def request(self, method, url, headers=None, timeout=None, **kwargs):
		if url.endswith("/oauth"):
			return Response(200, {"access_token": "t", "expires_in": 3600})
		query = url.split("?", 1)[1]
		self.queries.append(query)
		params = dict(pair.split("=", 1) for pair in query.split("&"))
		if params["tipo"] == "quitados":
			if self.fail_paid:
				return Response(500, {"detail": "Sistema em manutenção"})
			rows = self.paid_rows
		else:
			rows = self.open_rows
		return Response(200, {"_embedded": {"titulo": rows}, "page": 1, "page_count": 1, "total_items": len(rows)})

class TestErpSync(IntegrationTestCase):
	def setUp(self):
		self.patches = [patch("frappe.enqueue"), patch.object(frappe.db, "commit"), patch("time.sleep")]
		for p in self.patches:
			p.start()
		provider = frappe.get_single(PROVIDER_SETTINGS)
		provider.update({"collection_pipeline": None, "collection_grouping": importer.BY_PAIR})
		provider.save()
		self.configure_erp()
		self.reset_cache()

	def configure_erp(self):
		erp = frappe.get_single(erp_settings.SETTINGS)
		erp.update({
			"erp_enabled": 1, "erp_provider": "Smart Securities", "smart_client_id": "cid", "smart_client_secret": "s",
			"overdue_days": 1, "paid_lookback_days": 7, "last_paid_sync": None, "sync_dry_run": 0,
		})
		erp.save()
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def tearDown(self):
		for p in self.patches:
			p.stop()
		frappe.db.rollback()
		self.reset_cache()
		frappe.clear_document_cache(PROVIDER_SETTINGS, PROVIDER_SETTINGS)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def reset_cache(self):
		for key in ("cred_hub_erp_report:emaberto", "cred_hub_erp_report:quitados", erp_settings.TOKEN_KEY):
			frappe.cache.delete_value(key)

	def grouping(self, value):
		frappe.db.set_single_value(PROVIDER_SETTINGS, "collection_grouping", value)
		frappe.clear_document_cache(PROVIDER_SETTINGS, PROVIDER_SETTINGS)

	def run_sync(self, fake):
		self.reset_cache()
		return sync.run(session=fake)

	def title_row(self, title_id):
		found = sync.find_row("Smart Securities", str(title_id))
		self.assertTrue(found, f"título {title_id} not in the CRM")
		deal = frappe.get_doc("CRM Deal", found[0])
		return deal, next(r for r in deal.titles if r.name == found[1])

	def org(self, cnpj):
		return importer._existing_org(cnpj)

	def test_overdue_titles_become_cards_with_roles_and_erp_ids(self):
		fake = FakeSmart([
			open_row(101, "NF-1", DEBTOR_A, CEDENTE_A),
			open_row(102, "NF-2", DEBTOR_A, CEDENTE_A),
			open_row(103, "NF-3", DEBTOR_B, CEDENTE_A),
		])
		stats = self.run_sync(fake)
		self.assertEqual((stats["titles_new"], stats["cards_new"], stats["failed"]), (3, 2, 0), stats)
		# Both ends of the due-date filter, raw slashes (the only way Smart filters)
		self.assertIn(f"vencimentoIni=01/01/2000&vencimentoFim={br(-1)}", fake.queries[0])
		deal, row = self.title_row(101)
		self.assertEqual(deal.organization, self.org(DEBTOR_A))
		self.assertEqual(deal.second_organization, self.org(CEDENTE_A))
		self.assertEqual((row.erp_situation, row.erp_operation, row.erp_amount), ("Aberto", "13594", 1000))
		self.assertEqual(row.sacado, self.org(DEBTOR_A))
		self.assertFalse(row.species, "species is never guessed")
		self.assertEqual(frappe.db.get_value("CRM Organization", self.org(DEBTOR_A), FIELD), DEBTOR)
		self.assertEqual(frappe.db.get_value("CRM Organization", self.org(CEDENTE_A), FIELD), CEDENTE)
		settings = frappe.get_single(erp_settings.SETTINGS)
		self.assertEqual(getdate(settings.last_paid_sync), getdate(today()))
		self.assertIn("3 overdue", settings.last_sync_message)

	def test_second_run_updates_and_registers_partial_payment(self):
		self.run_sync(FakeSmart([open_row(201, "NF-9", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		stats = self.run_sync(FakeSmart([open_row(201, "NF-9", DEBTOR_A, CEDENTE_A, value="600,00", situation="Inadimplente(Aberto)")]))
		self.assertEqual((stats["titles_new"], stats["titles_update"], stats["partial_payments"]), (0, 1, 1), stats)
		deal, row = self.title_row(201)
		self.assertEqual(len(deal.titles), 1)
		self.assertEqual((row.updated_amount, row.paid_amount, row.erp_amount), (600, 400, 600))
		self.assertEqual(row.erp_situation, "Inadimplente(Aberto)")
		self.assertFalse(row.closed_reason)
		self.assertEqual(deal.outcome, "In progress")

	def test_settlement_closes_titles_and_wins_the_card(self):
		self.run_sync(FakeSmart([open_row(301, "NF-1", DEBTOR_A, CEDENTE_A), open_row(302, "NF-2", DEBTOR_A, CEDENTE_A)]))
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(301, "NF-1", DEBTOR_A)]))
		self.assertEqual((stats["titles_closed"], stats["cards_won"]), (1, 0))
		deal, row = self.title_row(301)
		self.assertEqual((row.closed_reason, row.updated_amount), (PAID, 0))
		self.assertEqual(deal.outcome, "In progress", "one título still open")
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(302, "NF-2", DEBTOR_A, situation="Repassado c/ Desconto")]))
		self.assertEqual((stats["titles_closed"], stats["cards_won"]), (1, 1))
		deal, row = self.title_row(302)
		self.assertEqual(row.closed_reason, REPURCHASED)
		self.assertEqual(deal.outcome, "Won")
		self.assertEqual(len(deal.payments), 2)
		# Settlements come with the look-back window
		self.assertTrue(frappe.get_all("Comment", {"reference_name": deal.name, "content": ("like", "%NF-2%")}))

	def test_settlement_of_an_unknown_title_is_ignored(self):
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(999, "NF-X", DEBTOR_A)]))
		self.assertEqual(stats["titles_closed"], 0)

	def test_outage_keeps_the_watermark(self):
		# In production the run commits as it goes; here commit is off, so check what the run tried to write
		with patch.object(frappe.db, "set_single_value", wraps=frappe.db.set_single_value) as writes:
			stats = self.run_sync(FakeSmart([open_row(601, "NF-1", DEBTOR_A, CEDENTE_A)], fail_paid=True))
		self.assertIn("error", stats)
		self.assertIn("maintenance", stats["error"])
		written = [call.args[1] for call in writes.call_args_list]
		self.assertFalse(any(isinstance(w, dict) and "last_paid_sync" in w for w in written), written)
		self.assertIn("last_sync_message", written)

	def test_settlement_window_starts_at_the_watermark_minus_look_back(self):
		frappe.db.set_single_value(erp_settings.SETTINGS, "last_paid_sync", add_days(today(), -2))
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		fake = FakeSmart()
		self.run_sync(fake)
		paid_query = next(q for q in fake.queries if "tipo=quitados" in q)
		self.assertIn(f"quitacaoIni={br(-9)}&quitacaoFim={br(0)}", paid_query)

	def test_same_report_is_not_reopened_within_ten_minutes(self):
		self.run_sync(FakeSmart())
		stats = sync.run(session=FakeSmart())  # no cache reset: the cooldown is still on
		self.assertIn("error", stats)

	def test_only_admins_can_sync_now(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				sync.sync_now()
		finally:
			frappe.set_user("Administrator")

	# Review of cred-hub#91 ------------------------------------------------------------------------------------

	def test_manual_payment_then_erp_drop_registers_one_payment_only(self):
		self.run_sync(FakeSmart([open_row(721, "NF-1", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		deal, row = self.title_row(721)
		sync.register_payment(deal, row, 400, getdate(today()), "registered by the operator")
		deal.save(ignore_permissions=True)
		stats = self.run_sync(FakeSmart([open_row(721, "NF-1", DEBTOR_A, CEDENTE_A, value="600,00")]))
		self.assertEqual(stats["partial_payments"], 0, stats)
		deal, row = self.title_row(721)
		self.assertEqual((row.updated_amount, row.paid_amount, len(deal.payments)), (600, 400, 1))
		# the ERP drops again with nothing registered by hand: the difference is registered once
		stats = self.run_sync(FakeSmart([open_row(721, "NF-1", DEBTOR_A, CEDENTE_A, value="500,00")]))
		deal, row = self.title_row(721)
		self.assertEqual((stats["partial_payments"], row.updated_amount, row.paid_amount), (1, 500, 500))

	def test_titles_that_left_the_overdue_list_are_counted(self):
		self.run_sync(FakeSmart([open_row(731, "NF-1", DEBTOR_A, CEDENTE_A), open_row(732, "NF-2", DEBTOR_A, CEDENTE_A)]))
		stats = self.run_sync(FakeSmart([open_row(731, "NF-1", DEBTOR_A, CEDENTE_A)]))
		self.assertEqual(stats["left_overdue"], 1)
		self.assertIn("1 títulos are no longer", frappe.get_single(erp_settings.SETTINGS).last_sync_message)

	def test_unexpected_error_leaves_a_trace_and_still_raises(self):
		with (
			patch.object(sync, "_sync_smart", side_effect=KeyError("boom")),
			patch.object(frappe.db, "set_single_value", wraps=frappe.db.set_single_value) as writes,
			self.assertRaises(KeyError),
		):
			sync.run(session=FakeSmart())
		messages = [c.args[2] for c in writes.call_args_list if len(c.args) > 2 and c.args[1] == "last_sync_message"]
		self.assertTrue(messages and "unexpected error" in messages[-1], messages)

	def test_a_second_run_at_the_same_time_is_skipped(self):
		with patch.object(sync, "_acquire_lock", return_value=False):
			self.assertEqual(sync.run(session=FakeSmart()), {"skipped": "running"})

	def test_settling_a_title_pays_its_agreement_installments(self):
		self.run_sync(FakeSmart([open_row(741, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal, row = self.title_row(741)
		deal.append("installments", {"title": row.name, "number": 1, "due_date": add_days(today(), 5), "amount": 500})
		deal.append("installments", {"title": row.name, "number": 2, "due_date": add_days(today(), 35), "amount": 500})
		deal.save(ignore_permissions=True)
		self.run_sync(FakeSmart(paid_rows=[paid_row(741, "NF-1", DEBTOR_A)]))
		deal, _row = self.title_row(741)
		self.assertTrue(all(slot.paid_on for slot in deal.installments))

	def test_company_listed_as_cedente_of_its_own_debt_stays_only_a_debtor(self):
		self.run_sync(FakeSmart([open_row(751, "NF-1", CEDENTE_B, CEDENTE_B)]))
		self.assertEqual(frappe.db.get_value("CRM Organization", self.org(CEDENTE_B), FIELD), DEBTOR)

	# Simulation (first run) ------------------------------------------------------------------------------------

	def test_simulation_is_on_by_default(self):
		self.assertEqual(str(frappe.get_meta(erp_settings.SETTINGS).get_field("sync_dry_run").default), "1")

	def test_simulation_reports_what_it_would_do_and_saves_nothing(self):
		fake = FakeSmart([open_row(901, "NF-1", DEBTOR_A, CEDENTE_A), open_row(902, "NF-2", DEBTOR_B, CEDENTE_A)])
		self.reset_cache()
		stats = sync.run(session=fake, dry_run=True)
		self.assertTrue(stats["dry_run"])
		self.assertEqual((stats["overdue_read"], stats["titles_new"], stats["cards_new"]), (2, 2, 2), stats)
		self.assertEqual(len(stats["plan"]), 2)
		self.assertIsNone(sync.find_row("Smart Securities", "901"), "nothing was saved")
		self.assertFalse(importer._existing_org(DEBTOR_A), "no organization was created either")
		settings = frappe.get_single(erp_settings.SETTINGS)
		self.assertIsNone(settings.last_paid_sync, "the watermark does not move in a simulation")
		self.assertTrue(settings.last_sync_message.startswith("SIMULATION"))
		self.assertIn("NF-1", settings.last_sync_report)
		self.assertIn("2 overdue", settings.last_sync_report)

	def test_after_a_simulation_the_real_run_does_the_same_work(self):
		rows = [open_row(911, "NF-1", DEBTOR_A, CEDENTE_A), open_row(912, "NF-2", DEBTOR_B, CEDENTE_A)]
		self.reset_cache()
		simulated = sync.run(session=FakeSmart(rows), dry_run=True)
		# The simulation rolled the test transaction back (in production the settings are committed before the job)
		self.configure_erp()
		real = self.run_sync(FakeSmart(rows))
		for key in ("overdue_read", "titles_new", "cards_new", "titles_update", "skipped", "failed"):
			self.assertEqual(simulated[key], real[key], key)
		self.assertTrue(sync.find_row("Smart Securities", "911"))
		self.assertFalse(real["dry_run"])

	def test_the_balance_of_an_erp_title_is_read_only_in_the_crm(self):
		from crm.api import collection

		self.run_sync(FakeSmart([open_row(951, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal, row = self.title_row(951)
		with self.assertRaisesRegex(frappe.ValidationError, "Smart Securities"):
			collection.record_payment(deal.name, row.name, 100)
		with self.assertRaises(frappe.ValidationError):
			collection.adjust_balance(deal.name, row.name, 800)
		# only the sync moves the balance
		self.run_sync(FakeSmart([open_row(951, "NF-1", DEBTOR_A, CEDENTE_A, value="700,00")]))
		_deal, row = self.title_row(951)
		self.assertEqual((row.updated_amount, row.paid_amount), (700, 300))
