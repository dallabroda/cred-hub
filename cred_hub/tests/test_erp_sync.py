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
from cred_hub.integrations import protest
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
		erp = frappe.get_single(erp_settings.SETTINGS)
		erp.update({
			"erp_enabled": 1, "erp_provider": "Smart Securities", "smart_client_id": "cid", "smart_client_secret": "s",
			"overdue_days": 1, "paid_lookback_days": 7, "last_paid_sync": None,
		})
		erp.save()
		self.reset_cache()

	def tearDown(self):
		for p in self.patches:
			p.stop()
		frappe.db.rollback()
		self.reset_cache()
		frappe.clear_document_cache(PROVIDER_SETTINGS, PROVIDER_SETTINGS)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)

	def reset_cache(self):
		for key in (sync.LOCK, "cred_hub_erp_report:emaberto", "cred_hub_erp_report:quitados", erp_settings.TOKEN_KEY):
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

	def test_grouping_by_cedente_keeps_each_sacado_and_protests_the_right_debtor(self):
		self.grouping(importer.BY_CEDENTE)
		stats = self.run_sync(FakeSmart([open_row(401, "NF-1", DEBTOR_A, CEDENTE_B), open_row(402, "NF-2", DEBTOR_B, CEDENTE_B)]))
		self.assertEqual((stats["cards_new"], stats["titles_new"]), (1, 2))
		deal, first = self.title_row(401)
		_deal, second = self.title_row(402)
		self.assertEqual(deal.name, _deal.name)
		self.assertEqual(deal.organization, self.org(CEDENTE_B))
		self.assertEqual((first.sacado, second.sacado), (self.org(DEBTOR_A), self.org(DEBTOR_B)))
		self.assertEqual(frappe.db.get_value("CRM Organization", self.org(DEBTOR_B), FIELD), DEBTOR)
		self.assertEqual(protest.debtor_of(deal, second)["documento"], DEBTOR_B)

	def test_one_card_per_title_never_duplicates(self):
		self.grouping(importer.BY_TITLE)
		rows = [open_row(501, "NF-1", DEBTOR_A, CEDENTE_A), open_row(502, "NF-2", DEBTOR_A, CEDENTE_A)]
		stats = self.run_sync(FakeSmart(rows))
		self.assertEqual((stats["cards_new"], stats["titles_new"]), (2, 2))
		stats = self.run_sync(FakeSmart(rows))
		self.assertEqual((stats["cards_new"], stats["titles_new"], stats["titles_update"]), (0, 0, 2))

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
