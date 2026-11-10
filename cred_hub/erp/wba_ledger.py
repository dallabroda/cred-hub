"""WBA: overdue títulos and settlements from the receivables + the accounting ledger (credtech#367).

WBA has no título status. A título is settled when the ledger has an income entry for it on an account the client
marked as "paid by the debtor" or "repurchased by the cedente" (each WBA client names its own accounts, so they are
picked in the settings, never guessed). Entries on a "partial payment" account lower what is left but do not close
the título. Entries on any other account are ignored.

The ledger points to a título by borderô number + document number (+ the sacado's CPF/CNPJ when present). When the
entry has no sacado, it still matches if that borderô + document identifies a single título.

Pure Python: the sync (Frappe) feeds it normalized receivables (`wba.normalize_receivable`) and entries
(`wba.normalize_entry`) and gets back títulos in the common shape the Smart sync already uses.

Assumptions to confirm with a real WBA credential (the docs do not say): the entry's `documento` is the título's
`numeroDocumento`; a REVERT line cancels the entries of the same `idEvento`.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .formats import digits

PAID, REPURCHASED, PARTIAL = "paid", "repurchased", "partial"
CENT = Decimal("0.005")


@dataclass
class Accounts:
	"""Ledger account ids (or names) chosen by the client for each meaning."""

	paid: set = field(default_factory=set)
	repurchased: set = field(default_factory=set)
	partial: set = field(default_factory=set)

	def meaning(self, entry: dict) -> str | None:
		for kind, chosen in ((REPURCHASED, self.repurchased), (PAID, self.paid), (PARTIAL, self.partial)):
			if entry.get("account_id") in chosen or (entry.get("account") and entry["account"] in chosen):
				return kind
		return None

	def empty(self) -> bool:
		return not (self.paid or self.repurchased)


@dataclass
class Result:
	overdue: list = field(default_factory=list)  # open, due on or before the limit: common título shape
	settled: list = field(default_factory=list)  # closed by the ledger: common shape with outcome/paid_*
	partial: list = field(default_factory=list)  # still open, with partial payments in the ledger
	unmatched: list = field(default_factory=list)  # settlement entries that point to no known título
	ambiguous: list = field(
		default_factory=list
	)  # entries without sacado whose borderô + document is not unique


def _number(value) -> str:
	text = str(value or "").strip().upper()
	return (text.lstrip("0") or "0") if text.isdigit() else text


def full_key(bordero_number, document, sacado_document) -> tuple:
	return (bordero_number, _number(document), digits(sacado_document))


def loose_key(bordero_number, document) -> tuple:
	return (bordero_number, _number(document))


def active_entries(entries: list[dict]) -> list[dict]:
	"""Income entries still standing: REVERT lines and the entries of a reverted event are dropped, and so are
	blocked ones."""
	reverted_events = {e["event"] for e in entries if e.get("reverted") and e.get("event")}
	return [
		e
		for e in entries
		if not e.get("reverted")
		and not e.get("blocked")
		and not (e.get("event") and e["event"] in reverted_events)
		and (e.get("category") or "ENTRADA") == "ENTRADA"
	]


def _index(receivables: list[dict]):
	by_full, by_loose = {}, {}
	for title in receivables:
		by_full[full_key(title.get("bordero_number"), title.get("number"), title.get("sacado_document"))] = (
			title
		)
		by_loose.setdefault(loose_key(title.get("bordero_number"), title.get("number")), []).append(title)
	return by_full, by_loose


def _find(entry, by_full, by_loose):
	"""(título, problem). Problem is "unmatched" or "ambiguous" when nothing safe was found."""
	if entry.get("sacado_document"):
		title = by_full.get(
			full_key(entry.get("bordero_number"), entry.get("document"), entry["sacado_document"])
		)
		return (title, None) if title else (None, "unmatched")
	candidates = by_loose.get(loose_key(entry.get("bordero_number"), entry.get("document"))) or []
	if len(candidates) == 1:
		return candidates[0], None
	return None, "ambiguous" if candidates else "unmatched"


def classify(receivables: list[dict], entries: list[dict], accounts: Accounts, overdue_until: date) -> Result:
	"""Split the receivables into settled / overdue / partially paid, from the ledger entries."""
	result = Result()
	by_full, by_loose = _index(receivables)
	paid = {}  # erp_id -> {"kinds": set, "amount": Decimal, "partial": Decimal, "date": date}
	for entry in active_entries(entries):
		kind = accounts.meaning(entry)
		if not kind:
			continue
		title, problem = _find(entry, by_full, by_loose)
		if problem:
			getattr(result, problem).append(entry)
			continue
		state = paid.setdefault(
			title["erp_id"], {"kinds": set(), "amount": Decimal(0), "partial": Decimal(0), "date": None}
		)
		state["kinds"].add(kind)
		amount = entry.get("amount") or Decimal(0)
		if kind == PARTIAL:
			state["partial"] += amount
		else:
			state["amount"] += amount
		if entry.get("date") and (state["date"] is None or entry["date"] > state["date"]):
			state["date"] = entry["date"]

	for title in receivables:
		state = paid.get(title["erp_id"])
		closing = state and state["kinds"] & {PAID, REPURCHASED}
		if closing:
			outcome = REPURCHASED if REPURCHASED in state["kinds"] else PAID
			result.settled.append(
				{
					**title,
					"outcome": outcome,
					"situation": "Recomprado" if outcome == REPURCHASED else "Liquidado",
					"paid_date": state["date"],
					# What came from the debtor (partials included); a repurchase is not a debtor payment
					"paid_amount": state["partial"] + (state["amount"] if outcome == PAID else Decimal(0)),
				}
			)
			continue
		if state and state["partial"] > CENT:
			result.partial.append(
				{**title, "partial_amount": state["partial"], "partial_date": state["date"]}
			)
		due = title.get("due_date")
		if due and due <= overdue_until:
			result.overdue.append({**title, "outcome": "open"})
	return result
