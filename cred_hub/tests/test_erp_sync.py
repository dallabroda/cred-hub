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
		self.enqueue = self.patches[0].start()
		for p in self.patches[1:]:
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


	def test_card_is_won_only_when_the_debtor_paid_every_title(self):
		self.run_sync(FakeSmart([open_row(301, "NF-1", DEBTOR_A, CEDENTE_A), open_row(302, "NF-2", DEBTOR_A, CEDENTE_A)]))
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(301, "NF-1", DEBTOR_A)]))
		self.assertEqual((stats["titles_closed"], stats["cards_won"]), (1, 0))
		deal, row = self.title_row(301)
		self.assertEqual(row.closed_reason, PAID)
		self.assertEqual(deal.outcome, "In progress", "one título still open")
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(302, "NF-2", DEBTOR_A, received="1.000,00")]))
		self.assertEqual((stats["titles_closed"], stats["cards_won"], stats["cards_to_review"]), (1, 1, 0))
		deal, _row = self.title_row(302)
		self.assertEqual(deal.outcome, "Won")
		self.assertEqual(len(deal.payments), 2)
		self.assertTrue(frappe.get_all("Comment", {"reference_name": deal.name, "content": ("like", "%NF-2%")}))

	def test_a_repurchase_closes_the_title_without_a_payment_and_never_wins_the_card(self):
		self.run_sync(FakeSmart([open_row(311, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal, row = self.title_row(311)
		deal.append("installments", {"title": row.name, "number": 1, "due_date": add_days(today(), 5), "amount": 1000})
		deal.save(ignore_permissions=True)
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(311, "NF-1", DEBTOR_A, situation="Repassado c/ Desconto")]))
		self.assertEqual((stats["titles_closed"], stats["cards_won"], stats["cards_to_review"]), (1, 0, 1), stats)
		deal, row = self.title_row(311)
		self.assertEqual(row.closed_reason, REPURCHASED)
		self.assertEqual((len(deal.payments), row.paid_amount), (0, 0), "not a payment of the debtor")
		self.assertEqual(sync._balance(row), 0, "the balance is closed by adjustment")
		# the agreement stays in the history as cancelled: neither paid nor open
		self.assertEqual([(bool(i.paid_on), bool(i.cancelled_on)) for i in deal.installments], [(False, True)])
		self.assertEqual(deal.outcome, "In progress", "the operator decides")
		self.assertTrue(frappe.get_all("Comment", {"reference_name": deal.name, "content": ("like", "%adjustment%")}))

	def test_the_payment_is_what_the_erp_received_and_the_difference_is_an_adjustment(self):
		self.run_sync(FakeSmart([
			open_row(321, "NF-1", DEBTOR_A, CEDENTE_A, value="1.000,00"),
			open_row(322, "NF-2", DEBTOR_A, CEDENTE_A, value="1.000,00"),
			open_row(323, "NF-3", DEBTOR_A, CEDENTE_A, value="1.000,00"),
		]))
		self.run_sync(FakeSmart(paid_rows=[
			paid_row(321, "NF-1", DEBTOR_A, received="800,00"),  # discount of 200
			paid_row(322, "NF-2", DEBTOR_A, received="1.050,00"),  # 50 above the balance
			paid_row(323, "NF-3", DEBTOR_A, received="0,00"),  # amount not informed
		]))
		deal, discount = self.title_row(321)
		_deal, interest = self.title_row(322)
		_deal, unknown = self.title_row(323)
		self.assertEqual(
			(discount.paid_amount, interest.paid_amount, unknown.paid_amount), (800, 1000, 1000)
		)
		self.assertEqual({sync._balance(r) for r in (discount, interest, unknown)}, {0})
		notes = " ".join(frappe.get_all("Comment", {"reference_name": deal.name}, pluck="content"))
		self.assertIn("Adjustment (discount)", notes)
		self.assertIn("above the balance", notes)
		self.assertIn("did not inform", notes)

	def test_second_run_updates_the_situation_and_never_invents_a_payment(self):
		self.run_sync(FakeSmart([open_row(201, "NF-9", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		stats = self.run_sync(
			FakeSmart([open_row(201, "NF-9", DEBTOR_A, CEDENTE_A, value="600,00", situation="Inadimplente(Aberto)")])
		)
		self.assertEqual((stats["titles_new"], stats["titles_update"]), (0, 1), stats)
		deal, row = self.title_row(201)
		self.assertEqual(len(deal.titles), 1)
		# the open report only has the face value: whatever it says, no payment is inferred from it
		self.assertEqual((row.paid_amount, len(deal.payments), row.updated_amount), (0, 0, 1000))
		self.assertEqual((row.erp_situation, row.erp_amount), ("Inadimplente(Aberto)", 600))
		self.assertEqual(deal.outcome, "In progress")

	def test_a_title_whose_updated_amount_is_above_the_face_gets_no_false_payment(self):
		# a título from a spreadsheet with interest included, then linked to the ERP id (face value below)
		self.run_sync(FakeSmart([open_row(211, "NF-1", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		deal, row = self.title_row(211)
		frappe.db.set_value("CRM Deal Title", row.name, "updated_amount", 1200)
		stats = self.run_sync(FakeSmart([open_row(211, "NF-1", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		deal, row = self.title_row(211)
		self.assertEqual((row.updated_amount, row.paid_amount, len(deal.payments)), (1200, 0, 0), stats)

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

	def _protest(self, title_id, status="PROTESTADO"):
		deal, row = self.title_row(title_id)
		frappe.db.set_value("CRM Deal Title", row.name, {"sent_at": frappe.utils.now_datetime(), "protest_status": status})
		frappe.db.set_value("CRM Deal", deal.name, "deal_owner", "Administrator")
		return deal.name

	def test_settling_a_protested_title_warns_and_never_withdraws(self):
		self.run_sync(FakeSmart([open_row(961, "NF-1", DEBTOR_A, CEDENTE_A), open_row(962, "NF-2", DEBTOR_B, CEDENTE_A)]))
		protested = self._protest(961)
		withdrawn = self._protest(962, status="RETIRADO")  # already withdrawn: nothing to warn about
		stats = self.run_sync(FakeSmart(paid_rows=[paid_row(961, "NF-1", DEBTOR_A), paid_row(962, "NF-2", DEBTOR_B)]))
		self.assertEqual((stats["titles_closed"], stats["protests_to_withdraw"]), (2, 1), stats)
		self.assertIn("withdraw", frappe.get_single(erp_settings.SETTINGS).last_sync_message)
		comments = frappe.get_all("Comment", {"reference_name": protested, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any("NF-1" in c and "protest is still active" in c for c in comments), comments)
		self.assertTrue(frappe.get_all("CRM Notification", {"reference_name": protested, "to_user": "Administrator"}))
		self.assertFalse(frappe.get_all("Comment", {"reference_name": withdrawn, "comment_type": "Comment"}))
		self.assertFalse(frappe.get_all("CRM Notification", {"reference_name": withdrawn}))
		# the registry was never touched
		self.assertFalse([c for c in self.enqueue.call_args_list if "protest" in str(c.args[0])])

	def test_a_simulation_only_counts_the_protests_to_withdraw(self):
		self.run_sync(FakeSmart([open_row(971, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal = self._protest(971)
		self.reset_cache()
		stats = sync.run(session=FakeSmart(paid_rows=[paid_row(971, "NF-1", DEBTOR_A)]), dry_run=True)
		self.assertEqual(stats["protests_to_withdraw"], 1)
		self.assertTrue(any(line.startswith("! NF-1") for line in stats["plan"]), stats["plan"])
		self.assertFalse(frappe.get_all("CRM Notification", {"reference_name": deal}))

	def test_the_balance_of_an_erp_title_is_read_only_in_the_crm(self):
		from crm.api import collection

		self.run_sync(FakeSmart([open_row(951, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal, row = self.title_row(951)
		with self.assertRaisesRegex(frappe.ValidationError, "Smart Securities"):
			collection.record_payment(deal.name, row.name, 100)
		with self.assertRaises(frappe.ValidationError):
			collection.adjust_balance(deal.name, row.name, 800)
		# only the sync moves the balance: when the ERP settles the título
		self.run_sync(FakeSmart(paid_rows=[paid_row(951, "NF-1", DEBTOR_A, received="1.000,00")]))
		_deal, row = self.title_row(951)
		self.assertEqual((sync._balance(row), row.paid_amount, row.closed_reason), (0, 1000, PAID))

	# Round 3 of the owner decisions ------------------------------------------------------------------------------

	def test_every_run_marks_what_it_creates_with_its_run_id(self):
		stats = self.run_sync(FakeSmart([open_row(931, "NF-1", DEBTOR_A, CEDENTE_A)]))
		self.assertRegex(stats["run_id"], r"^erp-\d{8}-\d{6}$")
		deal, row = self.title_row(931)
		self.assertEqual((deal.erp_run_id, row.erp_run_id), (stats["run_id"], stats["run_id"]))
		self.assertEqual(frappe.get_all("CRM Deal", {"erp_run_id": stats["run_id"]}, pluck="name"), [deal.name])

	def test_a_title_settled_in_the_same_run_is_not_counted_as_no_longer_overdue(self):
		self.run_sync(FakeSmart([open_row(731, "NF-1", DEBTOR_A, CEDENTE_A), open_row(732, "NF-2", DEBTOR_A, CEDENTE_A)]))
		stats = self.run_sync(FakeSmart([open_row(732, "NF-2", DEBTOR_A, CEDENTE_A)], [paid_row(731, "NF-1", DEBTOR_A)]))
		self.assertEqual((stats["titles_closed"], stats["left_overdue"]), (1, 0), stats)

	def test_the_registry_is_never_reached_by_itself_for_a_card_from_the_erp(self):
		settings = frappe.get_single(PROVIDER_SETTINGS)
		settings.update({"cenprot_enabled": 1, "auto_send_on_stage": 1, "auto_withdraw_on_stage": 1})
		settings.save()
		self.run_sync(FakeSmart([open_row(941, "NF-1", DEBTOR_A, CEDENTE_A)]))
		settings = frappe.get_single(PROVIDER_SETTINGS)
		first, send_stage, agreement_stage = frappe.get_all(
			"CRM Deal Status", {"pipeline": settings.collection_pipeline}, pluck="name", order_by="position asc", limit=3
		)
		settings.update({"stage_send": send_stage, "stage_agreement": agreement_stage})
		settings.save()
		erp_deal, _row = self.title_row(941)
		manual = frappe.get_doc({
			"doctype": "CRM Deal", "pipeline": settings.collection_pipeline, "status": first, "deal_name": "manual",
		}).insert()
		self.enqueue.reset_mock()
		for deal, stage in ((manual, send_stage), (erp_deal, send_stage), (erp_deal, agreement_stage)):
			deal.status = stage
			deal.save(ignore_permissions=True)
		jobs = [call.args[0] for call in self.enqueue.call_args_list if "protest" in str(call.args[0])]
		# only the manual card (control) reached the registry job; the card from the ERP never did
		self.assertEqual(jobs, ["cred_hub.integrations.protest.send_in_background"], jobs)

	def test_only_active_people_are_warned_about_a_protest(self):
		def user(email, enabled=1, technical=False):
			doc = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0], "enabled": enabled,
				"send_welcome_email": 0, "roles": [{"role": "System Manager"}],
			}).insert(ignore_permissions=True)
			if technical:
				frappe.db.set_value("User", doc.name, "credhub_api_key", "key-1")
			return doc.name

		active = user("active-admin-367@example.com")
		disabled = user("disabled-admin-367@example.com", enabled=0)
		technical = user("technical-367@example.com", technical=True)
		self.run_sync(FakeSmart([open_row(981, "NF-1", DEBTOR_A, CEDENTE_A)]))
		deal = self._protest(981)
		self.run_sync(FakeSmart(paid_rows=[paid_row(981, "NF-1", DEBTOR_A)]))
		notified = set(frappe.get_all("CRM Notification", {"reference_name": deal}, pluck="to_user"))
		self.assertIn(active, notified)
		self.assertNotIn(disabled, notified)
		self.assertNotIn(technical, notified)

	def test_two_erp_titles_with_the_same_number_and_due_date_stay_two_rows(self):
		# same sacado, same cedente, same number and due date, different ERP ids (the real portfolio has these)
		rows = [open_row(991, "NF-7", DEBTOR_A, CEDENTE_A, days=-10), open_row(992, "NF-7", DEBTOR_A, CEDENTE_A, days=-10)]
		stats = self.run_sync(FakeSmart(rows))
		self.assertEqual((stats["titles_new"], stats["titles_update"], stats["cards_new"]), (2, 0, 1), stats)
		deal, first = self.title_row(991)
		_deal, second = self.title_row(992)
		self.assertEqual(deal.name, _deal.name)
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(len(deal.titles), 2)
		# and the second run recognises both by their ERP id
		stats = self.run_sync(FakeSmart(rows))
		self.assertEqual((stats["titles_new"], stats["titles_update"]), (0, 2), stats)
		self.assertEqual(len(self.title_row(991)[0].titles), 2)

	def _plain(self, title_id, number="NF-7", debtor=DEBTOR_A, cedente=CEDENTE_A):
		"""A título as the spreadsheet import brings it: no ERP identity."""
		from cred_hub.erp.smart import normalize_title

		converted = sync.to_import_title(normalize_title(open_row(title_id, number, debtor, cedente, days=-10)))
		return {k: v for k, v in converted.items() if not k.startswith("erp_")}

	def _import(self, *titles):
		stats = {"cards_new": 0, "cards_update": 0, "titles_new": 0, "titles_update": 0, "titles_locked": 0}
		pipeline = sync._collection_pipeline()
		importer.import_group(pipeline, list(titles), importer.BY_PAIR, stats)
		return stats

	def test_a_spreadsheet_row_is_adopted_by_one_erp_title_only(self):
		self._import(self._plain(1))
		# the ERP now brings TWO títulos with the same key: one adopts the spreadsheet row, the other is a new row
		rows = [open_row(1001, "NF-7", DEBTOR_A, CEDENTE_A, days=-10), open_row(1002, "NF-7", DEBTOR_A, CEDENTE_A, days=-10)]
		stats = self.run_sync(FakeSmart(rows))
		self.assertEqual(stats["titles_new"], 1, stats)
		deal, first = self.title_row(1001)
		_deal, second = self.title_row(1002)
		self.assertEqual(deal.name, _deal.name)
		self.assertNotEqual(first.name, second.name)
		self.assertEqual(len(deal.titles), 2)

	def test_a_locked_row_still_gets_its_erp_identity_when_adopted(self):
		self._import(self._plain(1))
		deal = frappe.get_doc("CRM Deal", frappe.get_all("CRM Deal", pluck="name", order_by="creation desc", limit=1)[0])
		# already at the registry: the importer must not rewrite it, but the ERP identity is recorded
		frappe.db.set_value("CRM Deal Title", deal.titles[0].name, {"sent_at": frappe.utils.now_datetime(), "protest_status": "PROTESTADO"})
		self.run_sync(FakeSmart([open_row(1003, "NF-7", DEBTOR_A, CEDENTE_A, days=-10)]))
		self.assertTrue(sync.find_row("Smart Securities", "1003"))

	def test_reimporting_a_spreadsheet_does_not_duplicate_what_the_erp_brought(self):
		self.run_sync(FakeSmart([open_row(1011, "NF-8", DEBTOR_A, CEDENTE_A, days=-10)]))
		stats = self._import(self._plain(1, number="NF-8"))
		self.assertEqual((stats["titles_new"], stats["titles_update"]), (0, 1), stats)
		self.assertEqual(len(self.title_row(1011)[0].titles), 1)

	def test_an_ambiguous_spreadsheet_title_is_added_instead_of_choosing(self):
		rows = [open_row(1021, "NF-9", DEBTOR_A, CEDENTE_A, days=-10), open_row(1022, "NF-9", DEBTOR_A, CEDENTE_A, days=-10)]
		self.run_sync(FakeSmart(rows))
		stats = self._import(self._plain(1, number="NF-9"))
		self.assertEqual((stats["titles_new"], stats["titles_update"]), (1, 0), stats)
		self.assertEqual(len(self.title_row(1021)[0].titles), 3)

	def _last_deal(self):
		return frappe.get_doc("CRM Deal", frappe.get_all("CRM Deal", pluck="name", order_by="creation desc", limit=1)[0])

	def test_a_spreadsheet_never_rewrites_the_money_of_an_erp_title(self):
		self.run_sync(FakeSmart([open_row(1031, "NF-5", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		spreadsheet = {**self._plain(1, number="NF-5"), "original_amount": 5000, "updated_amount": 5000, "species": "DMI"}
		stats = self._import(spreadsheet)
		self.assertEqual((stats["titles_new"], stats["titles_update"]), (0, 1), stats)
		_deal, row = self.title_row(1031)
		# the balance of an ERP título stays what the ERP says; only an empty, non-monetary field is completed
		self.assertEqual((row.original_amount, row.updated_amount, row.paid_amount), (1000, 1000, 0))
		self.assertEqual(row.species, "DMI")

	def test_adopting_a_spreadsheet_row_keeps_its_payments(self):
		self._import(self._plain(1, number="NF-6"))
		deal = self._last_deal()
		row = deal.titles[0]
		sync.register_payment(deal, row, 300, getdate(today()), "registered before the ERP")
		deal.save(ignore_permissions=True)
		self.run_sync(FakeSmart([open_row(1041, "NF-6", DEBTOR_A, CEDENTE_A, value="1.000,00")]))
		_deal, adopted = self.title_row(1041)
		self.assertEqual((adopted.updated_amount, adopted.paid_amount, len(_deal.payments)), (700, 300, 1))
		# the row existed before the run: undoing the run (by erp_run_id) must not take it
		self.assertFalse(adopted.get("erp_run_id"))



class FakeSmartWithSacados(FakeSmart):
	"""Also answers the sacado (/sacado) and cedente (/cedente) registries, with the real API's key spelling."""

	def __init__(self, *args, sacados=(), cedentes=(), **kwargs):
		super().__init__(*args, **kwargs)
		self.sacados, self.cedentes = list(sacados), list(cedentes)

	def request(self, method, url, headers=None, timeout=None, **kwargs):
		for path, key in (("/sacado?", "sacado"), ("/cedente?", "cedente")):
			if path in url:
				self.queries.append(url.split("?", 1)[1])
				rows = getattr(self, key + "s")
				return Response(200, {"_embedded": {key: rows}, "page": 1, "page_count": 1, "total_items": len(rows)})
		return super().request(method, url, headers=headers, timeout=timeout, **kwargs)


class TestErpImportMap(TestErpSync):
	"""The company's de-para of títulos and sacados (credtech#367)."""

	def setUp(self):
		super().setUp()
		self.maps()

	def maps(self, title=(), sacado=(), cedente=()):
		erp = frappe.get_single(erp_settings.SETTINGS)
		erp.set("title_field_map", [{"erp_column": c, "crm_field": f} for c, f in title])
		erp.set("sacado_field_map", [{"erp_column": c, "crm_field": f} for c, f in sacado])
		erp.set("cedente_registry_map", [{"erp_column": c, "crm_field": f} for c, f in cedente])
		erp.save()
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def run_sync(self, fake):
		for kind in ("sacado", "cedente"):
			frappe.cache.delete_value(f"cred_hub_erp_report:{kind}")
		return super().run_sync(fake)

	def test_titulo_columns_fill_the_fields_the_company_chose(self):
		self.maps(title=[("TIPO", "erp_title_type"), ("VALOR_TOTAL", "updated_amount")])
		row = {**open_row(2001, "NF-20", DEBTOR_A, CEDENTE_A, value="1.000,00"), "VALOR_TOTAL": "1.087,50"}
		self.run_sync(FakeSmart([row]))
		_deal, title = self.title_row(2001)
		self.assertEqual((title.erp_title_type, title.original_amount, title.updated_amount), ("DMR", 1000, 1087.5))
		self.assertFalse(title.species)  # the protest species is never read from the ERP's type
		# the ERP owns the título: the next run brings the new value
		self.run_sync(FakeSmart([{**row, "VALOR_TOTAL": "1.120,00"}]))
		self.assertEqual(self.title_row(2001)[1].updated_amount, 1120)

	def test_columns_the_erp_returned_are_kept_even_in_a_simulation(self):
		frappe.db.set_single_value(erp_settings.SETTINGS, "sync_dry_run", 1)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		self.run_sync(FakeSmart([{**open_row(2011, "NF-21", DEBTOR_A, CEDENTE_A), "PRAZO_MEDIO": "30"}]))
		columns = json.loads(frappe.db.get_single_value(erp_settings.SETTINGS, "erp_columns"))["title"]
		self.assertIn("PRAZO_MEDIO", columns)
		self.assertFalse(sync.find_row("Smart Securities", "2011"))

	def test_nothing_mapped_never_reads_the_registries(self):
		self.maps()
		fake = FakeSmartWithSacados([open_row(2021, "NF-22", DEBTOR_A, CEDENTE_A)])
		self.run_sync(fake)
		self.assertFalse([q for q in fake.queries if "tipo=" not in q])

	def test_new_sacado_and_cedente_get_the_registry_once(self):
		self.maps(
			sacado=[("E-mail", "organization.registry_email"), ("Telefone", "contact.mobile_no")],
			cedente=[("TELEFONE", "contact.mobile_no")],
		)
		sacados = [{"ID": "9", "Nome": "SACADO A LTDA", "CPF/CNPJ": masked(DEBTOR_A), "E-Mail": "fin@sacado.com.br", "Telefone": "(11) 98765-4321"}]
		cedentes = [{"ID_CEDENTE": "5", "NOME": "CEDENTE A", "CPF_CNPJ": masked(CEDENTE_A), "TELEFONE": "(21) 99876-5432"}]
		stats = self.run_sync(FakeSmartWithSacados([open_row(2031, "NF-23", DEBTOR_A, CEDENTE_A)], sacados=sacados, cedentes=cedentes))
		self.assertEqual((stats.get("sacados_updated"), stats.get("cedentes_updated")), (1, 1), stats)
		org = self.org(DEBTOR_A)
		self.assertEqual(frappe.db.get_value("CRM Organization", org, "registry_email"), "fin@sacado.com.br")
		self.assertEqual(frappe.db.get_value("Contact", {"company_name": org}, "mobile_no"), "(11) 98765-4321")
		self.assertEqual(frappe.db.get_value("Contact", {"company_name": self.org(CEDENTE_A)}, "mobile_no"), "(21) 99876-5432")
		self.assertNotIn("columns", stats)
		# Registration data is read once: on the next run they already exist, the registries are not read
		frappe.db.set_value("CRM Organization", org, "registry_email", "")
		fake = FakeSmartWithSacados([open_row(2031, "NF-23", DEBTOR_A, CEDENTE_A)], sacados=sacados, cedentes=cedentes)
		self.run_sync(fake)
		self.assertFalse([q for q in fake.queries if "tipo=" not in q])
		self.assertFalse(frappe.db.get_value("CRM Organization", org, "registry_email"))

	def test_a_sacado_that_already_existed_is_not_read(self):
		self.maps(sacado=[("E-mail", "organization.registry_email")])
		importer.organization_for_cnpj(DEBTOR_A, "SACADO ANTIGO LTDA")
		frappe.db.set_value("CRM Organization", self.org(DEBTOR_A), "creation", add_days(today(), -30))
		fake = FakeSmartWithSacados([open_row(2041, "NF-24", DEBTOR_A, CEDENTE_A)], sacados=[{"CPF/CNPJ": masked(DEBTOR_A), "E-mail": "x@y.com"}])
		self.run_sync(fake)
		self.assertFalse([q for q in fake.queries if "dataIniSacado" in q])
