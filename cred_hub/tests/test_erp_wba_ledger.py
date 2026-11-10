"""WBA ledger (credtech#367): settlements and overdue títulos from receivables + accounting entries.
Pure: `python -m pytest cred_hub/tests/test_erp_wba_ledger.py` (no Frappe, nothing leaves the machine)."""

import unittest
from datetime import date
from decimal import Decimal

from cred_hub.erp.base import ErpError
from cred_hub.erp.wba import LEDGER_PATH, normalize_entry, normalize_receivable
from cred_hub.erp.wba_ledger import PAID, REPURCHASED, Accounts, active_entries, classify
from cred_hub.tests.test_erp_clients import FakeResponse, wba

BORDERO = {
	"idBordero": "b-1",
	"numeroBordero": 1001,
	"documentoCedente": "43.141.963/0001-18",
	"razaoSocialCedente": "CEDENTE SA",
}
TODAY = date(2026, 11, 10)


def receivable(number, sacado="11222333000181", due="01/10/2026", value=1000.0, rid=None):
	return normalize_receivable(
		{
			"idRecebivel": rid or f"r-{number}-{sacado[-2:]}",
			"idOperacao": "op-1",
			"numeroDocumento": number,
			"documentoSacado": sacado,
			"nomeSacado": "SACADO",
			"valor": value,
			"dataVencimento": due,
		},
		BORDERO,
	)


def entry(document, account="acc-liq", value=1000.0, sacado="11.222.333/0001-81", when="05/11/2026", **extra):
	return normalize_entry(
		{
			"id": extra.pop("id", f"e-{document}-{account}"),
			"idEvento": extra.pop("event", f"ev-{document}-{account}"),
			"valor": value,
			"data": when,
			"categoria": extra.pop("categoria", "ENTRADA"),
			"numeroBordero": extra.pop("bordero", 1001),
			"documento": document,
			"documentoSacado": sacado,
			"idContaEvento": account,
			"contaEvento": {
				"acc-liq": "Liquidação de título",
				"acc-rec": "Recompra",
				"acc-abat": "Abatimento",
			}.get(account, account),
			"situacao": extra.pop("situacao", "INSERT"),
			**extra,
		}
	)


ACCOUNTS = Accounts(paid={"acc-liq"}, repurchased={"acc-rec"}, partial={"acc-abat"})


class TestClassify(unittest.TestCase):
	def test_paid_repurchased_open_and_not_yet_due(self):
		titles = [receivable("1"), receivable("2"), receivable("3"), receivable("4", due="20/11/2026")]
		entries = [entry("1"), entry("2", "acc-rec")]
		result = classify(titles, entries, ACCOUNTS, TODAY)
		settled = {t["number"]: t for t in result.settled}
		self.assertEqual(settled["1"]["outcome"], PAID)
		self.assertEqual(settled["1"]["paid_amount"], Decimal("1000.0"))
		self.assertEqual(settled["1"]["paid_date"], date(2026, 11, 5))
		# A repurchase is not money from the debtor
		self.assertEqual(settled["2"]["outcome"], REPURCHASED)
		self.assertEqual(settled["2"]["paid_amount"], Decimal(0))
		# 3 is overdue and open; 4 is not due yet
		self.assertEqual([t["number"] for t in result.overdue], ["3"])

	def test_accounts_not_chosen_by_the_client_are_ignored(self):
		result = classify([receivable("1")], [entry("1", "acc-tarifa")], ACCOUNTS, TODAY)
		self.assertEqual(result.settled, [])
		self.assertEqual([t["number"] for t in result.overdue], ["1"])
		self.assertEqual(result.unmatched, [])

	def test_partial_payment_keeps_the_title_open(self):
		result = classify([receivable("1")], [entry("1", "acc-abat", 300)], ACCOUNTS, TODAY)
		self.assertEqual(result.settled, [])
		self.assertEqual(result.partial[0]["partial_amount"], Decimal(300))
		self.assertEqual([t["number"] for t in result.overdue], ["1"])

	def test_partial_then_settlement_counts_both_as_debtor_money(self):
		entries = [
			entry("1", "acc-abat", 300, when="01/11/2026"),
			entry("1", "acc-liq", 700, when="08/11/2026"),
		]
		settled = classify([receivable("1")], entries, ACCOUNTS, TODAY).settled[0]
		self.assertEqual(
			(settled["outcome"], settled["paid_amount"], settled["paid_date"]),
			(PAID, Decimal(1000), date(2026, 11, 8)),
		)

	def test_reverted_event_does_not_settle(self):
		entries = [entry("1", event="ev-9"), entry("1", event="ev-9", situacao="REVERT", id="e-rev")]
		result = classify([receivable("1")], entries, ACCOUNTS, TODAY)
		self.assertEqual(result.settled, [])
		self.assertEqual(len(result.overdue), 1)

	def test_blocked_and_outgoing_entries_are_ignored(self):
		entries = [entry("1", bloqueado=True), entry("1", categoria="SAIDA", id="e-out", event="ev-out")]
		self.assertEqual(active_entries(entries), [])

	def test_leading_zeros_and_masked_documents_still_match(self):
		result = classify([receivable("000123")], [entry("123", sacado="11222333000181")], ACCOUNTS, TODAY)
		self.assertEqual(result.settled[0]["number"], "000123")

	def test_same_document_in_two_borderos_never_mixes(self):
		title = receivable("1")
		result = classify([title], [entry("1", bordero=2002)], ACCOUNTS, TODAY)
		self.assertEqual(result.settled, [])
		self.assertEqual(len(result.unmatched), 1)

	def test_entry_without_sacado_matches_only_a_unique_title(self):
		unique = classify([receivable("1")], [entry("1", sacado="")], ACCOUNTS, TODAY)
		self.assertEqual(len(unique.settled), 1)
		# Two sacados with the same document number in the borderô: nothing is closed, the entry is reported
		twin = classify(
			[receivable("1"), receivable("1", sacado="99888777000166")],
			[entry("1", sacado="")],
			ACCOUNTS,
			TODAY,
		)
		self.assertEqual(twin.settled, [])
		self.assertEqual(len(twin.ambiguous), 1)
		self.assertEqual(len(twin.overdue), 2)

	def test_entry_of_another_sacado_does_not_close_the_title(self):
		result = classify([receivable("1")], [entry("1", sacado="99888777000166")], ACCOUNTS, TODAY)
		self.assertEqual(result.settled, [])
		self.assertEqual(len(result.unmatched), 1)

	def test_account_can_be_chosen_by_name(self):
		accounts = Accounts(paid={"Liquidação de título"})
		self.assertEqual(
			classify([receivable("1")], [entry("1")], accounts, TODAY).settled[0]["outcome"], PAID
		)
		self.assertTrue(Accounts().empty())

	def test_receivable_keeps_bordero_number(self):
		self.assertEqual(receivable("1")["bordero_number"], 1001)


def ledger_row(i, account="acc-liq"):
	return {
		"id": f"e-{i}",
		"idEvento": f"ev-{i}",
		"valor": 10,
		"data": "05/11/2026",
		"categoria": "ENTRADA",
		"idContaEvento": account,
		"contaEvento": account.upper(),
		"grupoConta": "Receitas",
	}


class TestLedgerClient(unittest.TestCase):
	def test_pages_until_a_short_page_and_sends_dates(self):
		client, session = wba(
			{
				("POST", LEDGER_PATH): [
					FakeResponse(200, [ledger_row(1), ledger_row(2)]),
					FakeResponse(200, [ledger_row(3)]),
				]
			}
		)
		rows = list(client.list_entries(date(2026, 11, 1), date(2026, 11, 10), page_size=2))
		self.assertEqual([r["id"] for r in rows], ["e-1", "e-2", "e-3"])
		calls = [s for s in session.sent if s["path"] == LEDGER_PATH]
		self.assertEqual([c["json"]["paginaAtual"] for c in calls], [0, 1])
		self.assertEqual(
			(calls[0]["json"]["periodoDe"], calls[0]["json"]["periodoAte"]), ("01/11/2026", "10/11/2026")
		)
		self.assertEqual(calls[0]["json"]["categoria"], "ENTRADA")
		self.assertEqual(calls[0]["headers"]["Tenant-ID"], "tenant-uuid")

	def test_same_page_twice_stops_instead_of_looping(self):
		client, _ = wba({("POST", LEDGER_PATH): [FakeResponse(200, [ledger_row(1), ledger_row(2)])]})
		with self.assertRaises(ErpError):
			list(client.list_entries(date(2026, 11, 1), date(2026, 11, 10), page_size=2))

	def test_both_dates_are_required(self):
		client, _ = wba({})
		with self.assertRaises(ValueError):
			list(client.list_entries(None, date(2026, 11, 10)))

	def test_ledger_accounts_for_the_settings(self):
		client, _ = wba(
			{
				("POST", LEDGER_PATH): [
					FakeResponse(
						200,
						[
							ledger_row(1),
							ledger_row(2),
							ledger_row(3, "acc-rec"),
						],
					)
				]
			}
		)
		accounts = client.ledger_accounts(date(2026, 10, 1), date(2026, 11, 10))
		self.assertEqual([(a["id"], a["entries"]) for a in accounts], [("acc-liq", 2), ("acc-rec", 1)])
		self.assertEqual(accounts[0]["group"], "Receitas")

	def test_normalize_entry(self):
		row = normalize_entry(
			{
				**ledger_row(1),
				"numeroBordero": 1001,
				"documento": " 77 ",
				"documentoSacado": "11.222.333/0001-81",
				"situacao": "REVERT",
				"valor": "1.234,56",
			}
		)
		self.assertEqual(
			(row["bordero_number"], row["document"], row["sacado_document"]), (1001, "77", "11222333000181")
		)
		self.assertTrue(row["reverted"])
		self.assertEqual(row["amount"], Decimal("1234.56"))
		self.assertEqual(row["date"], date(2026, 11, 5))


class TestReviewFixes(unittest.TestCase):
	"""Findings of the cross-review of cred-hub#93."""

	def test_blocked_as_text_false_does_not_hide_a_settlement(self):
		for value in ("false", "N", "0", "", None, False):
			self.assertFalse(entry("1", bloqueado=value)["blocked"], value)
		for value in ("true", "S", "1", True):
			self.assertTrue(entry("1", bloqueado=value)["blocked"], value)
		result = classify([receivable("1")], [entry("1", bloqueado="false")], ACCOUNTS, TODAY)
		self.assertEqual(len(result.settled), 1)

	def test_titles_without_bordero_number_are_reported_and_nothing_matching_is_flagged(self):
		title = {**receivable("1"), "bordero_number": None}
		result = classify([title], [entry("1")], ACCOUNTS, TODAY)
		self.assertEqual(result.untracked, [title])
		self.assertEqual(result.settled, [])
		self.assertEqual(len(result.unmatched), 1)
		self.assertTrue(result.looks_wrong)
		# One match is enough to trust the rule
		ok = classify([receivable("1"), receivable("2")], [entry("1"), entry("9")], ACCOUNTS, TODAY)
		self.assertFalse(ok.looks_wrong)

	def test_empty_account_choice_matches_nothing(self):
		accounts = Accounts(paid={"", None, " "}, repurchased=set())
		self.assertTrue(accounts.empty())
		no_account = entry("1", account="")
		self.assertEqual(classify([receivable("1")], [no_account], accounts, TODAY).settled, [])

	def test_full_debtor_payment_wins_over_a_repurchase_entry(self):
		entries = [entry("1", "acc-liq", 1000), entry("1", "acc-rec", 1000)]
		settled = classify([receivable("1")], entries, ACCOUNTS, TODAY).settled[0]
		self.assertEqual((settled["outcome"], settled["paid_amount"]), (PAID, Decimal(1000)))

	def test_partial_then_repurchase_keeps_the_debtor_money(self):
		entries = [entry("1", "acc-abat", 300), entry("1", "acc-rec", 700)]
		settled = classify([receivable("1")], entries, ACCOUNTS, TODAY).settled[0]
		self.assertEqual((settled["outcome"], settled["paid_amount"]), (REPURCHASED, Decimal(300)))
