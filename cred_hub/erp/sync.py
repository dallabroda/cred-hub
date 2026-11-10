"""Daily ERP -> Collection sync (credtech#367).

Every morning, after the ERP settles the day's payments:
1. Overdue títulos (due at least `overdue_days` ago) come from the ERP. A título already in the CRM (same ERP id)
   is updated in place; a new one goes through the collection importer, so the card grouping chosen by the client
   (pair, debtor, cedente or one card per título), the sacado/cedente organizations and their roles work exactly
   like the spreadsheet import.
2. Settlements since the last successful run (minus a look-back for back-dated entries) close the matching títulos:
   "Quitado*" = paid by the debtor, "Recomprado*"/"Repassado*" = repurchased by the cedente. When every título of a
   card is closed, the card is marked Won (owner decision 10/11/2026: the ERP is the source of truth).
3. The watermark (`last_paid_sync`) only moves when the whole run succeeded, so an outage never loses a settlement.

Owner decisions applied: nobody is assigned to the cards (the client assigns), the species is never guessed,
a partial payment keeps the título open with its balance.
Smart Securities only in this version; WBA has no overdue filter nor settlement data (next PR).
"""

import time

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime, today

from cred_hub.collection import importer
from cred_hub.integrations import get_settings as collection_settings
from cred_hub.integrations import protest
from cred_hub.integrations.bureau_log import ERROR, SUCCESS
from cred_hub.party.document import CNPJ, document_kind

from . import SMART
from .base import ErpError
from .fields import EXTENDED, PAID, REPURCHASED
from .settings import client, get_settings, log, user_message
from .smart import OPEN, OPEN_RANGE_START, normalize_title
from .smart import PAID as SETTLED

DEAL, TITLE_ROW = "CRM Deal", "CRM Deal Title"
LOCK = "cred_hub_erp_sync_running"
LOCK_SECONDS = 3 * 3600
# Smart: asking the same report again within 10 minutes restarts its cooldown, so never reopen it sooner
REPORT_COOLDOWN = 11 * 60
CENT = 0.005
OUTCOME_REASON = {"paid": PAID, "repurchased": REPURCHASED}


# Entry points ---------------------------------------------------------------------------------------------------


def scheduled():
	"""Daily job (hooks cron). Does nothing unless the company turned the integration on."""
	settings = get_settings()
	if settings.erp_enabled and settings.erp_provider:
		run()


@frappe.whitelist(methods=["POST"])
def sync_now() -> dict:
	"""Admin button: queue a run now (it still respects the ERP's 10-minute cooldown)."""
	frappe.only_for("System Manager")
	settings = get_settings()
	if not (settings.erp_enabled and settings.erp_provider):
		return {"ok": False, "message": _("Turn the ERP integration on and save the credentials first.")}
	frappe.enqueue(
		"cred_hub.erp.sync.run", queue="long", timeout=LOCK_SECONDS, job_id="cred_hub_erp_sync", deduplicate=True
	)
	return {"ok": True, "message": _("Synchronization queued. The result appears in the settings when it ends.")}


def run(session=None) -> dict:
	"""One full run. Returns the counters (also saved in the settings and the history)."""
	if frappe.cache.get_value(LOCK):
		return {"skipped": "running"}
	frappe.cache.set_value(LOCK, 1, expires_in_sec=LOCK_SECONDS)
	settings = get_settings()
	stats = _new_stats()
	try:
		if settings.erp_provider != SMART:
			stats["message"] = _("The collection sync for {0} comes in the next version.").format(settings.erp_provider)
			return stats
		erp = client(settings, session=session)
		_sync_smart(erp, settings, stats)
		frappe.db.set_single_value(
			"Cred Hub ERP Settings",
			{"last_open_sync": now_datetime(), "last_paid_sync": today(), "last_sync_message": _summary(stats)},
		)
		log(settings.erp_provider, "Collection sync", SUCCESS, response=stats)
	except ErpError as e:
		frappe.db.rollback()
		stats["error"] = user_message(e)
		frappe.db.set_single_value("Cred Hub ERP Settings", "last_sync_message", stats["error"])
		log(settings.erp_provider, "Collection sync", ERROR, http_status=e.http_status, error=str(e))
	finally:
		frappe.db.commit()
		frappe.cache.delete_value(LOCK)
	return stats


# Smart Securities -----------------------------------------------------------------------------------------------


def _sync_smart(erp, settings, stats):
	pipeline = _collection_pipeline()
	overdue_until = add_days(today(), -max(int(settings.overdue_days or 0), 0))
	_cooldown(OPEN)
	overdue = [
		normalize_title(row)
		for row in erp.titles(OPEN, vencimentoIni=OPEN_RANGE_START, vencimentoFim=getdate(overdue_until))
	]
	stats["overdue_read"] = len(overdue)
	upsert_overdue(pipeline, overdue, stats)
	frappe.db.commit()

	lookback = max(int(settings.paid_lookback_days or 7), 1)
	since = add_days(settings.last_paid_sync or today(), -lookback)
	_cooldown(SETTLED)
	settled = [normalize_title(row) for row in erp.titles(SETTLED, quitacaoIni=getdate(since), quitacaoFim=getdate(today()))]
	stats["settled_read"] = len(settled)
	close_settled(settled, stats)


def _cooldown(report: str):
	"""Never reopen the same ERP report within its cooldown (also protects against two runs back to back)."""
	key = f"cred_hub_erp_report:{report}"
	last = frappe.cache.get_value(key)
	if last and time.time() - float(last) < REPORT_COOLDOWN:
		raise ErpError(
			f"The '{report}' report was requested less than 10 minutes ago.", "rate_limited", 429
		)
	frappe.cache.set_value(key, time.time(), expires_in_sec=REPORT_COOLDOWN)


# Títulos --------------------------------------------------------------------------------------------------------


def _new_stats() -> dict:
	return {
		"overdue_read": 0, "settled_read": 0, "titles_new": 0, "titles_update": 0, "titles_locked": 0,
		"cards_new": 0, "cards_update": 0, "partial_payments": 0, "titles_closed": 0, "cards_won": 0,
		"skipped": 0, "failed": 0,
	}


def _summary(stats) -> str:
	return _(
		"{0} overdue títulos read: {1} new, {2} updated, {3} new cards. {4} settlements read: {5} títulos closed, "
		"{6} cards won. {7} partial payments. {8} títulos skipped, {9} with errors."
	).format(
		stats["overdue_read"], stats["titles_new"], stats["titles_update"], stats["cards_new"], stats["settled_read"],
		stats["titles_closed"], stats["cards_won"], stats["partial_payments"], stats["skipped"], stats["failed"],
	)


def _collection_pipeline() -> str:
	settings = collection_settings()
	if not settings.collection_pipeline:
		doc = frappe.get_single("Cred Hub Provider Settings")
		protest.create_collection_pipeline(doc)
		doc.save(ignore_permissions=True)
		settings = collection_settings()
	return settings.collection_pipeline


def to_import_title(title: dict) -> dict | None:
	"""Common ERP título -> the collection importer's título. None when the sacado document is unusable."""
	debtor_kind = document_kind(title["sacado_document"])
	if not debtor_kind or not title.get("erp_id") or not title.get("due_date") or not title.get("amount"):
		return None
	cedente = title["cedente_document"] if document_kind(title["cedente_document"]) == CNPJ else ""
	amount = flt(title["amount"], 2)
	return {
		"line": title["erp_id"],
		"debtor_document": title["sacado_document"],
		"debtor_kind": debtor_kind,
		"debtor_name": title["sacado_name"],
		"cedente_document": cedente,
		"cedente_name": title["cedente_name"] if cedente else "",
		"title_number": (title["number"] or title["erp_id"])[:140],
		"our_number": (title.get("our_number") or "")[:140],
		# Never guessed (owner rule): the operator picks the species before protesting
		"species": "",
		"issue_date": title.get("issue_date"),
		"due_date": title["due_date"],
		"original_amount": amount,
		"updated_amount": amount,
		"digitable_line": (title.get("digitable_line") or "")[:140],
		"erp_source": title["erp"],
		"erp_title_id": title["erp_id"],
		"erp_operation": title.get("operation") or "",
		"erp_situation": title.get("situation") or "",
		"erp_amount": amount,
	}


def find_row(source: str, erp_id: str):
	"""(card name, row name) of the título already linked to this ERP id, or None."""
	if not erp_id:
		return None
	rows = frappe.get_all(
		TITLE_ROW,
		{"parenttype": DEAL, "erp_source": source, "erp_title_id": str(erp_id)},
		["parent", "name"],
		order_by="creation asc",
		limit=1,
	)
	return (rows[0].parent, rows[0].name) if rows else None


def upsert_overdue(pipeline: str, titles: list[dict], stats: dict):
	grouping = importer._grouping(collection_settings())
	new_titles = []
	for title in titles:
		found = find_row(title["erp"], title["erp_id"])
		if found:
			_savepoint(stats, update_open_row, found, title, stats)
			continue
		converted = to_import_title(title)
		if converted is None:
			stats["skipped"] += 1
			continue
		new_titles.append(converted)
	groups = {}
	for title in new_titles:
		groups.setdefault(importer._group_key(title, grouping), []).append(title)
	for group in groups.values():
		_savepoint(stats, importer.import_group, pipeline, group, grouping, stats)


def _savepoint(stats, fn, *args):
	"""One título/card with an error never stops the run; it is counted and logged."""
	frappe.db.savepoint("cred_hub_erp_sync")
	try:
		fn(*args)
	except Exception:  # noqa: BLE001 - one bad título must not stop the daily run
		frappe.db.rollback(save_point="cred_hub_erp_sync")
		frappe.clear_messages()
		stats["failed"] += 1
		frappe.log_error(title="Cred Hub: ERP collection sync")


def update_open_row(found, title: dict, stats: dict):
	deal = frappe.get_doc(DEAL, found[0])
	row = next((r for r in deal.titles if r.name == found[1]), None)
	if row is None or row.get("closed_reason"):
		return
	amount = flt(title.get("amount"), 2)
	previous = flt(row.get("erp_amount"))
	if previous and amount and amount < previous - CENT:
		# The ERP open amount dropped: partial payment (owner decision: keep collecting the balance)
		register_payment(
			deal, row, flt(previous - amount, 2), getdate(today()),
			_("Partial payment reported by {0}.").format(title["erp"]),
		)
		stats["partial_payments"] += 1
	row.erp_situation = title.get("situation") or row.erp_situation
	row.erp_amount = amount or row.erp_amount
	if title.get("digitable_line") and not row.digitable_line:
		row.digitable_line = title["digitable_line"][:140]
	deal.save(ignore_permissions=True)
	stats["titles_update"] += 1


def register_payment(deal, row, amount: float, paid_on, note: str):
	"""Same bookkeeping as the card's "Register payment" (crm.api.collection), without its permission check."""
	from crm.fcrm.doctype.crm_deal_title.crm_deal_title import balance_of

	balance = balance_of(row)
	applied = min(flt(amount, 2), balance)
	if applied <= 0:
		return 0
	after = max(flt(balance - applied, 2), 0)
	deal.append(
		"payments",
		{
			"title": row.name,
			"paid_on": paid_on,
			"amount": applied,
			"balance_after": after,
			"note": note,
			"registered_by": frappe.session.user,
		},
	)
	row.updated_amount = after
	row.paid_amount = flt(flt(row.paid_amount) + applied, 2)
	return applied


def _balance(row) -> float:
	from crm.fcrm.doctype.crm_deal_title.crm_deal_title import balance_of

	return balance_of(row)


def close_settled(titles: list[dict], stats: dict):
	cards = {}
	for title in titles:
		reason = OUTCOME_REASON.get(title.get("outcome"))
		found = find_row(title["erp"], title["erp_id"]) if reason else None
		if not found:
			continue
		cards.setdefault(found[0], []).append((found[1], title, reason))
	for card, items in cards.items():
		_savepoint(stats, close_titles, card, items, stats)


def close_titles(card: str, items: list, stats: dict):
	deal = frappe.get_doc(DEAL, card)
	by_name = {row.name: row for row in deal.titles}
	notes = []
	for row_name, title, reason in items:
		row = by_name.get(row_name)
		if row is None or row.get("closed_reason"):
			continue
		paid_on = title.get("paid_date") or getdate(today())
		received = flt(title.get("paid_amount"), 2)
		note = _("{0} in {1} on {2} ({3}); amount received: {4}.").format(
			_(reason),
			title["erp"],
			frappe.utils.formatdate(paid_on),
			title.get("situation") or "",
			frappe.utils.fmt_money(received, currency="BRL") if received else "-",
		)
		# The ERP closed the título: the whole open balance is settled in one entry (interest, discount and
		# repurchase included); the amount the ERP received stays in the note
		register_payment(deal, row, _balance(row), paid_on, note)
		row.closed_reason = reason
		row.closed_on = paid_on
		row.erp_situation = title.get("situation") or row.erp_situation
		notes.append(_("Título {0}: {1}").format(row.title_number, note))
		stats["titles_closed"] += 1
	if not notes:
		return
	won = _close_card_if_done(deal)
	deal.save(ignore_permissions=True)
	deal.add_comment("Info", "<br>".join(frappe.utils.escape_html(n) for n in notes))
	if won:
		stats["cards_won"] += 1


def _close_card_if_done(deal) -> bool:
	"""Every título closed by the ERP -> the card is Won (Lost when all were only extended). Cards already closed
	by a person are never touched."""
	if deal.get("outcome") not in (None, "", "In progress"):
		return False
	titles = deal.get("titles") or []
	if not titles or not all(t.get("closed_reason") for t in titles):
		return False
	if all(t.closed_reason == EXTENDED for t in titles):
		deal.outcome = "Lost"
		deal.lost_reason = _lost_reason(EXTENDED)
		return False
	deal.outcome = "Won"
	return True


def _lost_reason(name: str) -> str:
	if not frappe.db.exists("CRM Lost Reason", name):
		frappe.get_doc({"doctype": "CRM Lost Reason", "lost_reason": name}).insert(ignore_permissions=True)
	return name
