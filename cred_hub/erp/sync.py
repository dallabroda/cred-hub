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

Simulation (credtech#367, owner decision): while `sync_dry_run` is on, the run executes the same code inside the
transaction, measures it and reports what it would create, update and close; then everything is rolled back, so the
simulation can never differ from the real run. The owner (and the client) read the report before turning it off.

Owner decisions applied: nobody is assigned to the cards (the client assigns), the species is never guessed,
partial payments cannot be detected (the ERP reports only the face value of an open título), so the título stays
open until the ERP settles it.
Smart Securities only in this version; WBA has no overdue filter nor settlement data (next PR).
"""

import json
import time

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime, today

from cred_hub.collection import importer
from cred_hub.integrations import get_settings as collection_settings
from cred_hub.integrations import protest
from cred_hub.integrations.bureau_log import ERROR, SUCCESS
from cred_hub.party.document import CNPJ, document_kind

from . import import_map
from .base import ErpError
from .fields import PAID, REPURCHASED
from .settings import connector, connector_class, get_settings, log, user_message

DEAL, TITLE_ROW = "CRM Deal", "CRM Deal Title"
LOCK_SECONDS = 3 * 3600
PLAN_LINES = 300
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


def _lock_name() -> str:
	return f"cred_hub_erp_sync:{frappe.conf.db_name}"[:64]


def _acquire_lock() -> bool:
	"""One run per site, atomic (MariaDB named lock): it is released by itself if the worker dies."""
	return bool(frappe.db.sql("select get_lock(%s, 0)", (_lock_name(),))[0][0])


def _release_lock():
	frappe.db.sql("select release_lock(%s)", (_lock_name(),))


def _commit():
	"""Commits between phases, except in a simulation (which is rolled back at the end)."""
	if not frappe.flags.get("cred_hub_erp_dry_run"):
		frappe.db.commit()


def _plan(stats: dict, line: str):
	"""Line of the simulation report (only collected in a simulation, capped)."""
	if frappe.flags.get("cred_hub_erp_dry_run") and len(stats["plan"]) < PLAN_LINES:
		stats["plan"].append(line)


def _money(value) -> str:
	return frappe.utils.fmt_money(flt(value, 2), currency="BRL")


def _report(stats: dict) -> str:
	head = [
		_("SIMULATION: nothing was saved."),
		_("ERP read in {0} s: {1} overdue títulos and {2} settlements.").format(
			stats.get("seconds"), stats["overdue_read"], stats["settled_read"]
		),
		_summary(stats),
		"",
	]
	if stats["titles_new"] and not stats["titles_closed"]:
		head.insert(
			3,
			_("Settlements only appear after the first real run, when the títulos exist in the CRM."),
		)
	lines = stats["plan"]
	if len(lines) >= PLAN_LINES:
		lines = [*lines, _("... (the list shows the first {0} lines)").format(PLAN_LINES)]
	return "\n".join(head + lines)


def run(session=None, dry_run: bool | None = None) -> dict:
	"""One full run. Returns the counters (also saved in the settings and the history). `dry_run=None` follows the
	"Simulation mode" switch of the settings."""
	if not _acquire_lock():
		return {"skipped": "running"}
	settings = get_settings()
	dry = bool(settings.sync_dry_run) if dry_run is None else bool(dry_run)
	frappe.flags.cred_hub_erp_dry_run = dry
	# Marks every card and título this run creates, so a whole run can be undone in bulk
	run_id = f"erp-{now_datetime():%Y%m%d-%H%M%S}"
	frappe.flags.cred_hub_erp_run_id = run_id
	started = time.time()
	stats = _new_stats()
	stats["dry_run"] = dry
	stats["run_id"] = run_id
	try:
		cls = connector_class(settings)
		caps = cls.capabilities if cls else None
		if not (caps and caps.overdue_titles and caps.settled_titles):
			# Decided before touching credentials, as before the connector contract
			stats["message"] = _("The collection sync for {0} comes in the next version.").format(settings.erp_provider)
			return stats
		erp = connector(settings, session=session)
		_sync_titles(erp, settings, stats)
		stats["seconds"] = round(time.time() - started, 1)
		if dry:
			# Throw away everything the run wrote, keep only the report
			frappe.db.rollback()
		_save_columns(stats)
		# Raw ERP rows (names, documents) are not kept anywhere else
		stats.pop("columns", None)
		if dry:
			frappe.db.set_single_value(
				"Cred Hub ERP Settings",
				{
					"last_sync_message": _("SIMULATION (nothing was saved): {0}").format(_summary(stats)),
					"last_sync_report": _report(stats),
				},
			)
			log(settings.erp_provider, "Collection sync (simulation)", SUCCESS, response=_loggable(stats))
		else:
			frappe.db.set_single_value(
				"Cred Hub ERP Settings",
				{"last_open_sync": now_datetime(), "last_paid_sync": today(), "last_sync_message": _summary(stats)},
			)
			log(settings.erp_provider, "Collection sync", SUCCESS, response=_loggable(stats))
	except ErpError as e:
		frappe.db.rollback()
		stats["error"] = user_message(e)
		frappe.db.set_single_value("Cred Hub ERP Settings", "last_sync_message", stats["error"])
		log(settings.erp_provider, "Collection sync", ERROR, http_status=e.http_status, error=str(e))
	except Exception as e:
		# Not an ERP failure (a bug or an unexpected answer): leave a trace for the admin, then fail loudly
		frappe.db.rollback()
		message = _("The synchronization stopped because of an unexpected error. Support was notified.")
		frappe.db.set_single_value("Cred Hub ERP Settings", "last_sync_message", message)
		log(settings.erp_provider, "Collection sync", ERROR, error=f"{type(e).__name__}: {e}")
		frappe.log_error(title="Cred Hub: ERP collection sync")
		raise
	finally:
		frappe.flags.cred_hub_erp_dry_run = False
		frappe.flags.cred_hub_erp_run_id = None
		frappe.db.commit()
		_release_lock()
	return stats


def _loggable(stats: dict) -> dict:
	return {key: value for key, value in stats.items() if key not in ("plan", "columns")}


def _save_columns(stats: dict):
	"""Columns the ERP returned in this run, offered in the de-para screen (kept after a simulation too)."""
	raw = frappe.db.get_single_value("Cred Hub ERP Settings", "erp_columns")
	changed = False
	for kind, rows in (stats.get("columns") or {}).items():
		raw, new = import_map.merge_columns(raw, kind, rows)
		changed = changed or new
	if changed:
		frappe.db.set_single_value("Cred Hub ERP Settings", "erp_columns", raw)


# Títulos from the connector (ADR 0007: any system that declares overdue + settled títulos) -------------------


def _sync_titles(erp, settings, stats):
	started = now_datetime()
	pipeline = _collection_pipeline()
	overdue_until = add_days(today(), -max(int(settings.overdue_days or 0), 0))
	# Read both reports before writing anything: the database transaction is open only for the (short) write phase
	_cooldown(erp, "overdue")
	overdue = list(erp.overdue_titles(getdate(overdue_until)))
	stats["overdue_read"] = len(overdue)
	stats["columns"] = {import_map.TITLE: _distinct_shapes(t["raw"] for t in overdue)}
	lookback = max(int(settings.paid_lookback_days or 7), 1)
	since = add_days(settings.last_paid_sync or today(), -lookback)
	_cooldown(erp, "settled")
	settled = list(erp.settled_titles(getdate(since), getdate(today())))
	stats["settled_read"] = len(settled)

	upsert_overdue(pipeline, overdue, stats)
	_commit()
	close_settled(settled, stats)
	# After the settlements: a título just closed is not "no longer overdue"
	stats["left_overdue"] = count_left_overdue(erp.key, {t["erp_id"] for t in overdue})
	_commit()
	sync_parties(erp, settings, stats, overdue, started)


def _distinct_shapes(rows) -> list[dict]:
	"""One sample row per distinct set of columns: enough to learn the columns of a report with thousands of rows."""
	shapes = {}
	for row in rows:
		shapes.setdefault(tuple(row), row)
	return list(shapes.values())


# Sacado registry: the de-para fills the empty fields of the sacados the CRM already has -------------------------


# Cedente and sacado registry ---------------------------------------------------------------------------------
# Owner, 11/11/2026: a título always comes with its cedente and its sacado (the card is linked to both). Their
# registration is read ONCE: only a cedente/sacado that this run created gets the columns the company mapped from
# the ERP registry; one that already existed is never read again. The rest (address, Receita data) comes from the
# CNPJ enrichment the HubTech already runs on every new organization.
PARTIES = (
	# kind, de-para table, document of the título, ERP reader
	(import_map.SACADO, "sacado_field_map", "sacado_document"),
	(import_map.CEDENTE, "cedente_registry_map", "cedente_document"),
)


# A new cedente/sacado whose registry could not be read (ERP down, cooldown, an error filling it) is kept pending
# and tried again on the next runs, up to this many times
PENDING_TRIES = 5


def sync_parties(erp, settings, stats, titles, since):
	pending = _load_pending()
	for kind, table, document_key in PARTIES:
		rules = settings.get(table) or []
		waiting = pending.get(kind) or {}
		if not rules:
			pending.pop(kind, None)
			continue
		todo = {doc: (doctype, name) for doc, (doctype, name, _tries) in waiting.items() if frappe.db.exists(doctype, name)}
		todo.update(_created_since({t[document_key] for t in titles if t.get(document_key)}, since))
		stats[f"{kind}s_new"] = len(todo)
		if not todo:
			pending.pop(kind, None)
			continue
		started = time.time()
		try:
			if not _reads_registry(erp, kind):
				# This system has no registry of this kind: nothing to read, nothing to wait for
				pending.pop(kind, None)
				continue
			_cooldown(erp, kind)
			rows = list(erp.reader(kind)())
		except ErpError as e:
			# The títulos are already saved; these records wait for the next run (the CNPJ enrichment runs anyway)
			stats[f"{kind}_error"] = user_message(e)
			pending[kind] = _retry(waiting, todo, todo)
			continue
		stats[f"{kind}_seconds"] = round(time.time() - started, 1)
		stats.setdefault("columns", {})[kind] = _distinct_shapes(rows)
		org_types, contact_types = _fieldtypes("CRM Organization"), _fieldtypes("Contact")
		from cred_hub.cnpj.mapping import only_digits

		failed = {}
		for row in rows:
			document = only_digits(import_map.value_of(row, "CPF/CNPJ"))
			if document in todo:
				before = stats["failed"]
				_savepoint(stats, fill_party, kind, row, todo[document], rules, org_types, contact_types, stats)
				if stats["failed"] > before:
					failed[document] = todo[document]
		# Read: done, even when the registry did not have it (the registration is read once)
		pending[kind] = _retry(waiting, failed, todo)
	if not frappe.flags.get("cred_hub_erp_dry_run"):
		frappe.db.set_single_value("Cred Hub ERP Settings", "erp_pending_parties", json.dumps(pending) if any(pending.values()) else None)


def _load_pending() -> dict:
	try:
		return json.loads(frappe.db.get_single_value("Cred Hub ERP Settings", "erp_pending_parties") or "{}")
	except ValueError:
		return {}


def _retry(waiting: dict, again: dict, todo: dict) -> dict:
	"""The ones to try again on the next run, each with one more try (given up after PENDING_TRIES)."""
	result = {}
	for document, (doctype, name) in again.items():
		tries = (waiting.get(document) or [None, None, 0])[2] + 1
		if tries < PENDING_TRIES:
			result[document] = [doctype, name, tries]
	return result


def _created_since(documents, since) -> dict:
	"""{document: (doctype, name)} of the organizations (CNPJ) and contacts (CPF) created by this run."""
	from cred_hub.cnpj.mapping import format_cnpj
	from cred_hub.party.document import CPF, format_cpf

	created = {}
	for document in documents:
		kind = document_kind(document)
		if kind == CNPJ:
			doctype, field, values = "CRM Organization", "cnpj", (format_cnpj(document), document)
		elif kind == CPF:
			doctype, field, values = "Contact", "cpf", (format_cpf(document), document)
		else:
			continue
		found = frappe.get_all(doctype, {field: ("in", values), "creation": (">=", since)}, pluck="name", limit=1)
		if found:
			created[document] = (doctype, found[0])
	return created


def _fieldtypes(doctype) -> dict:
	return {df.fieldname: df.fieldtype for df in frappe.get_meta(doctype).fields if df.fieldtype in import_map.VALUE_TYPES}


def fill_party(kind, row, record, rules, org_types, contact_types, stats):
	"""Fills the EMPTY fields of a new cedente/sacado (organization or person) and its contact with the mapped ERP
	columns. A company with nobody yet gets its first contact from the ERP's phone and e-mail."""
	doctype, name = record
	contact_values = import_map.sacado_values(row, rules, "contact", contact_types)
	if doctype == "CRM Organization":
		changed = _fill_empty(doctype, name, import_map.sacado_values(row, rules, "organization", org_types))
		if contact_values:
			contact = frappe.db.get_value("Contact", {"company_name": name}, "name", order_by="creation asc")
			if contact:
				changed = _fill_empty("Contact", contact, contact_values) or changed
			else:
				person = str(import_map.value_of(row, "Nome") or "").strip() or name
				doc = frappe.get_doc({"doctype": "Contact", "first_name": person[:140], "company_name": name})
				_contact_channels(doc, contact_values)
				doc.update(contact_values)
				doc.insert(ignore_permissions=True)
				changed = True
	else:
		changed = _fill_empty("Contact", name, contact_values)
	if changed:
		stats[f"{kind}s_updated"] = stats.get(f"{kind}s_updated", 0) + 1
		_plan(stats, f"~ {_(kind)} {name}")


def _contact_channels(contact, values: dict):
	"""A Contact recomputes mobile_no, phone and email_id from its phone and e-mail tables on save: they go there."""
	if values.get("mobile_no"):
		contact.append("phone_nos", {"phone": values.pop("mobile_no"), "is_primary_mobile_no": 1})
	if values.get("phone"):
		contact.append("phone_nos", {"phone": values.pop("phone"), "is_primary_phone": 1})
	if values.get("email_id"):
		contact.append("email_ids", {"email_id": values.pop("email_id"), "is_primary": 1})


def _fill_empty(doctype, name, values) -> bool:
	"""Only empty fields: what the user or the Localizar wrote is never overwritten."""
	if not values:
		return False
	doc = frappe.get_doc(doctype, name)
	empty = {k: v for k, v in values.items() if doc.get(k) in (None, "")}
	if not empty:
		return False
	if doctype == "Contact":
		_contact_channels(doc, empty)
	doc.update(empty)
	doc.flags.cred_hub_enriching = True
	doc.save(ignore_permissions=True)
	return True


def _reads_registry(erp, kind: str) -> bool:
	caps = erp.capabilities
	return caps.sacado_registry if kind == import_map.SACADO else caps.cedente_registry


def _cooldown(erp, report: str):
	"""Never reopen the same report within the system's cooldown (its `pace`), shared by every worker of the site
	(also protects against two runs back to back)."""
	cooldown = erp.pace.report_cooldown
	if not cooldown:
		return
	key = f"cred_hub_erp_report:{erp.key}:{report}"
	last = frappe.cache.get_value(key)
	if last and time.time() - float(last) < cooldown:
		raise ErpError(
			f"The '{report}' report was requested less than {max(cooldown // 60 - 1, 1)} minutes ago.", "rate_limited", 429
		)
	frappe.cache.set_value(key, time.time(), expires_in_sec=cooldown)


# Títulos --------------------------------------------------------------------------------------------------------


def _new_stats() -> dict:
	return {
		"overdue_read": 0, "settled_read": 0, "titles_new": 0, "titles_update": 0, "titles_locked": 0,
		"cards_new": 0, "cards_update": 0, "titles_closed": 0, "cards_won": 0,
		"left_overdue": 0, "cards_to_review": 0, "protests_to_withdraw": 0, "skipped": 0, "failed": 0, "plan": [],
	}


def _summary(stats) -> str:
	text = _summary_base(stats)
	for kind, label in ((import_map.SACADO, _("sacado")), (import_map.CEDENTE, _("cedente"))):
		if stats.get(f"{kind}_error"):
			text += " " + _("The ERP {0} registry could not be read ({1}); the new ones are tried again on the next run.").format(
				label, stats[f"{kind}_error"]
			)
	if stats.get("protests_to_withdraw"):
		text += " " + _(
			"{0} títulos settled in the ERP still have a protest at the registry: withdraw or cancel it."
		).format(stats["protests_to_withdraw"])
	return text


def _summary_base(stats) -> str:
	return _(
		"{0} overdue títulos read: {1} new, {2} updated, {3} new cards. {4} settlements read: {5} títulos closed, "
		"{6} cards won, {7} to review. {8} títulos skipped, {9} with errors. {10} títulos are no longer overdue in "
		"the ERP (extended or written off) and stay open here."
	).format(
		stats["overdue_read"], stats["titles_new"], stats["titles_update"], stats["cards_new"], stats["settled_read"],
		stats["titles_closed"], stats["cards_won"], stats["cards_to_review"], stats["skipped"], stats["failed"],
		stats["left_overdue"],
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
		"erp_run_id": frappe.flags.get("cred_hub_erp_run_id") or "",
	}


def mapped_title_values(title: dict) -> dict:
	"""{título field: value} from the ERP columns the company mapped (cred_hub.erp.import_map)."""
	rules = get_settings().get("title_field_map") or []
	if not rules or not title.get("raw"):
		return {}
	return import_map.title_values(title["raw"], rules, _fieldtypes(TITLE_ROW))


def count_left_overdue(source: str, seen: set) -> int:
	"""Open títulos linked to the ERP that are no longer in today's overdue list: the due date was extended or the
	título was written off without a settlement. They stay open here (closing them is a decision for the client)."""
	rows = frappe.get_all(
		TITLE_ROW,
		{"parenttype": DEAL, "erp_source": source, "erp_title_id": ("is", "set"), "closed_reason": ("is", "not set")},
		pluck="erp_title_id",
	)
	return sum(1 for erp_id in rows if erp_id not in seen)


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
		converted["extra"] = mapped_title_values(title)
		new_titles.append(converted)
		_plan(
			stats,
			f"+ {converted['title_number']} | {converted['debtor_name'] or converted['debtor_document']} | "
			f"{_money(converted['original_amount'])} | {_('due')} {converted['due_date']}",
		)
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
	# The open report only has the face value (no balance): no payment is ever inferred from it
	amount = flt(title.get("amount"), 2)
	row.erp_situation = title.get("situation") or row.erp_situation
	row.erp_amount = amount or row.erp_amount
	if title.get("digitable_line") and not row.digitable_line:
		row.digitable_line = title["digitable_line"][:140]
	# The company's título de-para runs on every sync (the ERP owns the título), except on a título already sent
	# to the registry: what went to the cartório does not change
	if not row.get("sent_at"):
		values = mapped_title_values(title)
		if flt(row.get("paid_amount")):
			# A row adopted with payments booked here: its balance is what is left after them, never the ERP's value
			values.pop("updated_amount", None)
		row.update(values)
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
	to_withdraw = []
	for row_name, title, reason in items:
		row = by_name.get(row_name)
		if row is None or row.get("closed_reason"):
			continue
		if has_active_protest(row):
			to_withdraw.append((row.title_number, title["erp"], protest.row_status(row)))
			stats["protests_to_withdraw"] += 1
			_plan(stats, f"! {row.title_number} | {_('protest still active at the registry: withdraw it')}")
		closed_on = title.get("paid_date") or getdate(today())
		if reason == REPURCHASED:
			note = _settle_repurchased(deal, row, title, closed_on)
		else:
			note = _settle_paid(deal, row, title, closed_on)
		row.closed_reason = reason
		row.closed_on = closed_on
		row.erp_situation = title.get("situation") or row.erp_situation
		notes.append(_("Título {0}: {1}").format(row.title_number, note))
		stats["titles_closed"] += 1
		_plan(stats, f"v {row.title_number} | {_(reason)} | {frappe.utils.formatdate(closed_on)}")
	if not notes:
		return
	won, review = _close_card_if_done(deal)
	if review:
		notes.append(_("All títulos of this card are closed in the ERP, some were repurchased by the cedente. Check the card and close it."))
	deal.save(ignore_permissions=True)
	deal.add_comment("Info", "<br>".join(frappe.utils.escape_html(n) for n in notes))
	stats["cards_won"] += int(won)
	stats["cards_to_review"] += int(review)
	for number, erp, status in to_withdraw:
		warn_protest_active(deal, number, erp, status)


def people_to_notify(users: set) -> set:
	"""Only people: enabled users that are not the hidden technical user of an API key."""
	filters = {"name": ("in", list(users - {"Guest"})), "enabled": 1}
	if frappe.get_meta("User").has_field("credhub_api_key"):
		filters["credhub_api_key"] = ("is", "not set")
	return set(frappe.get_all("User", filters, pluck="name")) if users else set()


def has_active_protest(row) -> bool:
	"""The título went to the registry and the registry did not close it yet: its protest is still alive."""
	return bool(row.get("sent_at")) and protest.row_status(row) not in protest.FINAL


def warn_protest_active(deal, number: str, erp: str, status: str):
	"""The ERP settled a título that is still protested: the operator must withdraw or cancel it (owner decision:
	the system warns, and never withdraws by itself). A simulation only counts it."""
	if frappe.flags.get("cred_hub_erp_dry_run"):
		return
	text = _(
		"Title {0} was settled in {1} but its protest is still active at the registry ({2}). Withdraw or cancel "
		"the protest: the system never does it by itself."
	).format(number, erp, status or _("sent"))
	protest._note(deal, "⚠ " + text)
	users = set(frappe.get_all("Has Role", {"role": "System Manager", "parenttype": "User"}, pluck="parent"))
	if deal.get("deal_owner"):
		users.add(deal.deal_owner)
	for user in sorted(people_to_notify(users)):
		frappe.get_doc(
			{
				"doctype": "CRM Notification",
				"from_user": frappe.session.user,
				"to_user": user,
				"type": "Automation",
				"notification_text": text,
				"message": f"<p>{frappe.utils.escape_html(text)}</p>",
				"notification_type_doctype": DEAL,
				"notification_type_doc": deal.name,
				"reference_doctype": DEAL,
				"reference_name": deal.name,
			}
		).insert(ignore_permissions=True)


def _settle_paid(deal, row, title: dict, paid_on) -> str:
	"""The debtor paid. The payment is what the ERP says it received (VALOR_LIQUIDADO); what is left of the balance
	is an adjustment (discount), and what came above it is interest or fine: neither is a payment."""
	owed = _balance(row)
	received = flt(title.get("paid_amount"), 2)
	when = frappe.utils.formatdate(paid_on)
	if received <= 0:
		register_payment(deal, row, owed, paid_on, _("Paid by debtor in {0} on {1}.").format(title["erp"], when))
		extra = _("The ERP did not inform the amount received: the whole balance was settled.")
	else:
		applied = min(received, owed)
		register_payment(
			deal, row, applied, paid_on,
			_("Paid by debtor in {0} on {1}: {2} received.").format(title["erp"], when, _money(received)),
		)
		if owed - applied > CENT:
			extra = _("Adjustment (discount) of {0} to settle the título.").format(_money(owed - applied))
		elif received - applied > CENT:
			extra = _("{0} received above the balance (interest or fine), not counted as payment.").format(_money(received - applied))
		else:
			extra = ""
	row.updated_amount = 0
	# The agreement of a settled título has nothing left to collect
	for slot in deal.get("installments") or []:
		if slot.title == row.name and not slot.paid_on and not slot.get("cancelled_on"):
			slot.paid_on = paid_on
	return " ".join(part for part in (_("{0} in {1} on {2} ({3}).").format(_(PAID), title["erp"], when, title.get("situation") or ""), extra) if part)


def _settle_repurchased(deal, row, title: dict, closed_on) -> str:
	"""The cedente bought the título back: the debtor did not pay, so no payment is registered. The balance is closed
	by an adjustment and the agreement (if any) is kept in the history as cancelled."""
	owed = _balance(row)
	row.updated_amount = 0
	for slot in list(deal.get("installments") or []):
		if slot.title == row.name and not slot.paid_on and not slot.get("cancelled_on"):
			if frappe.get_meta("CRM Deal Installment").has_field("cancelled_on"):
				slot.cancelled_on = closed_on
			else:
				deal.remove(slot)
	return _("{0} in {1} on {2} ({3}). Balance of {4} closed by adjustment: not a payment of the debtor.").format(
		_(REPURCHASED), title["erp"], frappe.utils.formatdate(closed_on), title.get("situation") or "", _money(owed)
	)


def _close_card_if_done(deal) -> tuple[bool, bool]:
	"""(won, to review). Won only when every título was paid by the debtor; a repurchase never marks the card Won by
	itself: the card stays open for the operator. Cards already closed by a person are never touched."""
	if deal.get("outcome") not in (None, "", "In progress"):
		return False, False
	titles = deal.get("titles") or []
	if not titles or not all(t.get("closed_reason") for t in titles):
		return False, False
	if all(t.closed_reason == PAID for t in titles):
		deal.outcome = "Won"
		return True, False
	return False, True
