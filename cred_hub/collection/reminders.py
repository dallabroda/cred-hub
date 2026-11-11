"""Régua de cobrança (credtech#367, PR 6): approved WhatsApp templates sent through the SmartChat to the sacado of
each open título of the collection funnel, on the days the company chooses.

Owner decisions (11/11/2026):
- the company configures only the steps (days from the due date + template) and the WhatsApp number. The AI agent
  that answers the sacado and the templates themselves are configured by the company in the SmartChat;
- one message per título, to that título's sacado; the conversation is opened with no agent assigned, so the
  SmartChat's AI agent (when the company has one) takes it;
- collection does not stop because the debtor asks; a título is never charged again once it is protested (sent to the
  cartório), paid/closed, or has a payment promise (or an agreement in installments);
- comes turned off: Meta charges each template the company sends.

A step goes once per título (Cred Hub Collection Reminder Log, unique key título + days). The job runs every day at
9h and only sends the step whose day is today: a título that is already 200 days overdue when the régua is turned on
does not receive the old steps.
"""

import json

import frappe
import phonenumbers
from frappe import _
from frappe.utils import add_days, cint, fmt_money, formatdate, getdate, today

from cred_hub import welcome
from cred_hub.integrations import get_settings as collection_settings
from cred_hub.integrations.protest import row_status

SETTINGS = "Cred Hub Collection Reminders"
LOG = "Cred Hub Collection Reminder Log"
DEAL, TITLE_ROW = "CRM Deal", "CRM Deal Title"
SENT, NOT_SENT, FAILED, SENDING = "Sent", "Not sent", "Failed", "Sending"
ESTIMATE_DAYS = 30

# Título information that can fill a template variable (labels translated in the .po)
VARIABLES = {
	"sacado": "Sacado name",
	"title_number": "Título number",
	"due_date": "Due date",
	"balance": "Amount due",
	"digitable_line": "Digitable line",
	"cedente": "Cedente",
	"days_overdue": "Days overdue",
}


# Pure rules -----------------------------------------------------------------------------------------------------


def step_days(due_date, on_day) -> int:
	"""Days from the due date (negative = before it)."""
	return (getdate(on_day) - getdate(due_date)).days


def stop_reason(row, has_open_agreement=False) -> str:
	"""Why a título is never charged (empty = it can be charged)."""
	from crm.fcrm.doctype.crm_deal_title.crm_deal_title import balance_of

	if row.get("closed_reason") or balance_of(row) <= 0:
		return _("paid or closed")
	if row.get("sent_at") or row_status(row):
		return _("protested")
	if row.get("promise_date"):
		return _("payment promise")
	if has_open_agreement:
		return _("agreement in installments")
	return ""


def variable_values(row, deal_values, on_day) -> dict:
	"""Value of each VARIABLES key for a título (empty string when the título does not have it)."""
	from crm.fcrm.doctype.crm_deal_title.crm_deal_title import balance_of

	overdue = step_days(row.get("due_date"), on_day)
	return {
		"sacado": (deal_values.get("sacado") or "").strip(),
		"title_number": (row.get("title_number") or "").strip(),
		"due_date": formatdate(row.get("due_date"), "dd/mm/yyyy") if row.get("due_date") else "",
		"balance": fmt_money(balance_of(row), 2, "BRL", format="#.###,##"),
		"digitable_line": (row.get("digitable_line") or "").strip(),
		"cedente": (deal_values.get("cedente") or "").strip(),
		"days_overdue": str(max(overdue, 0)),
	}


def load_variables(raw) -> list:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
	except ValueError:
		value = []
	return [str(v) for v in value] if isinstance(value, list) else []


def is_mobile(phone) -> bool:
	try:
		number = phonenumbers.parse(phone or "", "BR")
	except phonenumbers.NumberParseException:
		return False
	return phonenumbers.number_type(number) in (
		phonenumbers.PhoneNumberType.MOBILE,
		phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
	)


# Settings -------------------------------------------------------------------------------------------------------


def validate_settings(doc):
	"""Steps: unique days, a template approved in the chosen WhatsApp number and a título information for each of its
	variables. Checked in the SmartChat only when the régua is on (off, the company may save a draft)."""
	seen = set()
	for step in doc.get("steps") or []:
		step.days = cint(step.days)
		if step.days in seen:
			frappe.throw(_("Two steps on the same day ({0}).").format(step.days))
		seen.add(step.days)
		step.variables = json.dumps(load_variables(step.variables))
	doc.steps = sorted(doc.get("steps") or [], key=lambda s: s.days)
	for index, step in enumerate(doc.steps, 1):
		step.idx = index
	if not doc.enabled:
		return
	if not doc.steps:
		frappe.throw(_("Add at least one step to the collection reminders."))
	connection, token = welcome._connection()
	if not connection:
		frappe.throw(_("Connect the SmartChat before turning on the collection reminders."))
	if not doc.inbox_id:
		frappe.throw(_("Choose the WhatsApp number of the collection reminders."))
	try:
		inbox = next((i for i in welcome._inboxes(connection.account_id, token) if i["id"] == cint(doc.inbox_id)), None)
	except welcome.ChatError as e:
		frappe.throw(str(e))
	if not inbox:
		frappe.throw(_("This WhatsApp number is not in the connected SmartChat account."))
	for step in doc.steps:
		template = _template(inbox, step.template_name, step.template_language)
		if not template:
			frappe.throw(_("The template {0} is not approved for this WhatsApp number.").format(step.template_name))
		step.template_language, step.template_category = template["language"], template["category"]
		variables = load_variables(step.variables)
		if len(variables) != len(template["variables"]) or any(v not in VARIABLES for v in variables):
			frappe.throw(_("Choose the título information of each variable of the template {0}.").format(step.template_name))


def _template(inbox, name, language=None):
	return next(
		(t for t in (inbox or {}).get("templates") or [] if t["name"] == name and (not language or t["language"] == language)),
		None,
	)


@frappe.whitelist(methods=["GET"])
def get_reminders():
	"""Settings screen: the steps, the WhatsApp numbers with their approved templates, the título information a
	variable can use and how many messages the steps would send in the next 30 days (Meta charges each one)."""
	welcome._only_managers()
	doc = frappe.get_single(SETTINGS)
	return {
		"enabled": bool(doc.enabled),
		"inbox_id": cint(doc.inbox_id) or None,
		"steps": [
			{
				"days": cint(s.days),
				"template_name": s.template_name,
				"template_language": s.template_language,
				"variables": load_variables(s.variables),
			}
			for s in doc.steps
		],
		"variables": [{"key": key, "label": _(label)} for key, label in VARIABLES.items()],
		"estimate": estimate([cint(s.days) for s in doc.steps]),
		"estimate_days": ESTIMATE_DAYS,
	}


@frappe.whitelist(methods=["POST"])
def save_reminders(enabled=None, inbox_id=None, steps=None):
	welcome._only_managers()
	doc = frappe.get_single(SETTINGS)
	if enabled is not None:
		doc.enabled = cint(enabled)
	if inbox_id is not None:
		doc.inbox_id = cint(inbox_id) or None
	if steps is not None:
		doc.set("steps", [])
		for step in frappe.parse_json(steps) or []:
			doc.append(
				"steps",
				{
					"days": cint(step.get("days")),
					"template_name": step.get("template_name"),
					"template_language": step.get("template_language"),
					"variables": json.dumps(load_variables(step.get("variables"))),
				},
			)
	doc.save(ignore_permissions=True)
	return get_reminders()


# Títulos --------------------------------------------------------------------------------------------------------


def _open_rows(due_dates):
	"""Títulos of open cards of the collection funnel due on one of these dates."""
	pipeline = collection_settings().get("collection_pipeline")
	if not pipeline or not due_dates:
		return []
	deals = frappe.get_all(
		DEAL,
		filters={"pipeline": pipeline},
		or_filters=[["outcome", "is", "not set"], ["outcome", "=", "In progress"]],
		pluck="name",
	)
	if not deals:
		return []
	return frappe.get_all(
		TITLE_ROW,
		filters={"parenttype": DEAL, "parent": ("in", deals), "due_date": ("in", list(due_dates))},
		fields=["*"],
	)


def _open_agreements(row_names) -> set:
	if not row_names or not frappe.db.exists("DocType", "CRM Deal Installment"):
		return set()
	filters = {"title": ("in", list(row_names)), "paid_on": ("is", "not set")}
	if frappe.get_meta("CRM Deal Installment").has_field("cancelled_on"):
		filters["cancelled_on"] = ("is", "not set")
	return set(frappe.get_all("CRM Deal Installment", filters=filters, pluck="title"))


def estimate(days_list, on_day=None) -> int:
	"""Messages the steps would send in the next 30 days to the títulos that are open today (before checking phones)."""
	on_day = getdate(on_day or today())
	days_list = sorted(set(days_list or []))
	if not days_list:
		return 0
	due_dates = {add_days(on_day, offset - d) for d in days_list for offset in range(ESTIMATE_DAYS)}
	rows = _open_rows({getdate(d) for d in due_dates})
	agreements = _open_agreements([r.name for r in rows])
	total = 0
	for row in rows:
		if stop_reason(row, row.name in agreements):
			continue
		total += sum(1 for d in days_list if 0 <= step_days(on_day, add_days(row.due_date, d)) < ESTIMATE_DAYS)
	return total


def _people(deal, row):
	"""Who receives: the título's sacado (person, or the people of the sacado company; the card's own when the título has
	no sacado), the ones the Localizar marked with WhatsApp first."""
	contacts = []
	if row.get("sacado_contact"):
		contacts.append(row.sacado_contact)
	organization = row.get("sacado") or (None if row.get("sacado_contact") else deal.get("organization"))
	if organization:
		order = "has_whatsapp desc, creation asc" if frappe.get_meta("Contact").has_field("has_whatsapp") else "creation asc"
		contacts += frappe.get_all("Contact", filters={"company_name": organization}, order_by=order, pluck="name")
	if not row.get("sacado") and not row.get("sacado_contact") and deal.get("contact"):
		contacts.append(deal.contact)
	return organization, list(dict.fromkeys(contacts))


def recipient(deal, row) -> tuple[str, str]:
	"""(name, WhatsApp in E.164) of the título's sacado; ("", "") when no mobile is known."""
	organization, contacts = _people(deal, row)
	for name in contacts:
		contact = frappe.db.get_value("Contact", name, ["full_name", "first_name", "mobile_no", "phone"], as_dict=True)
		if not contact:
			continue
		for phone in (contact.mobile_no, contact.phone):
			e164 = welcome._e164(phone)
			if e164 and is_mobile(e164):
				return contact.full_name or contact.first_name or e164, e164
	if organization:
		phone = welcome._e164(frappe.db.get_value("CRM Organization", organization, "registry_phone"))
		if phone and is_mobile(phone):
			return frappe.db.get_value("CRM Organization", organization, "organization_name") or phone, phone
	return "", ""


def _sacado_name(deal, row):
	if row.get("sacado"):
		return frappe.db.get_value("CRM Organization", row.sacado, "organization_name") or row.sacado
	if row.get("sacado_contact"):
		return frappe.db.get_value("Contact", row.sacado_contact, "full_name") or row.sacado_contact
	if deal.get("organization"):
		return frappe.db.get_value("CRM Organization", deal.organization, "organization_name") or deal.organization
	return ""


# Daily job ------------------------------------------------------------------------------------------------------


def scheduled():
	settings = frappe.get_single(SETTINGS)
	if not settings.enabled or not settings.steps:
		return
	frappe.enqueue("cred_hub.collection.reminders.run", queue="long", timeout=3600, job_id="cred_hub_collection_reminders", deduplicate=True)


def run(on_day=None):
	"""Sends today's step of every open título of the collection funnel (each step once per título)."""
	settings = frappe.get_single(SETTINGS)
	if not settings.enabled or not settings.steps:
		return {}
	on_day = getdate(on_day or today())
	steps = {cint(s.days): s for s in settings.steps}
	rows = _open_rows({getdate(add_days(on_day, -d)) for d in steps})
	agreements = _open_agreements([r.name for r in rows])
	due = [
		(row, steps[step_days(row.due_date, on_day)])
		for row in rows
		if step_days(row.due_date, on_day) in steps and not stop_reason(row, row.name in agreements)
	]
	due = [(row, step) for row, step in due if not frappe.db.exists(LOG, f"{row.name}:{step.days}")]
	stats = {SENT: 0, NOT_SENT: 0, FAILED: 0}
	if not due:
		return stats
	try:
		inbox = _inbox(settings)
	except welcome.ChatError:
		# Nothing recorded: the títulos get today's step on a run after the SmartChat is back (same day only)
		frappe.log_error(title="Collection reminders: SmartChat unavailable")
		return stats
	for row, step in due:
		key = f"{row.name}:{step.days}"
		try:
			status = _send_one(settings, inbox, step, row, key, on_day)
		except Exception:  # noqa: BLE001 - one título must not stop the others
			frappe.db.rollback()
			frappe.log_error(title=f"Collection reminder {key}")
			# The step is recorded before the SmartChat call; never sent again, shown as failed
			# Only a step still "Sending" is ours and undelivered (a "Sent" one, or another run's, stays as it is)
			if frappe.db.get_value(LOG, key, "status") == SENDING:
				frappe.db.set_value(LOG, key, {"status": FAILED, "error": "Unexpected error (support notified)"})
				status = FAILED
			else:
				status = frappe.db.get_value(LOG, key, "status") or FAILED
		stats[status] = stats.get(status, 0) + 1
		frappe.db.commit()
	return stats


def _inbox(settings):
	connection, token = welcome._connection()
	if not connection:
		raise welcome.ChatError(_("The SmartChat was disconnected."))
	inbox = next((i for i in welcome._inboxes(connection.account_id, token) if i["id"] == cint(settings.inbox_id)), None)
	if not inbox:
		raise welcome.ChatError(_("The WhatsApp number of the collection reminders is not in the SmartChat account."))
	return {"connection": connection, "token": token, **inbox}


def _send_one(settings, inbox, step, row, key, on_day) -> str:
	deal = frappe.get_doc(DEAL, row.parent)
	log = frappe.get_doc(
		{
			"doctype": LOG,
			"key": key,
			"deal": deal.name,
			"title_row": row.name,
			"title_number": row.title_number,
			"days": step.days,
			"template_name": step.template_name,
			"status": SENDING,
		}
	).insert(ignore_permissions=True)
	# Recorded before the SmartChat call: a crash after it never sends the same step twice
	frappe.db.commit()
	label = _("título {0}").format(row.title_number) if row.title_number else _("título due on {0}").format(
		formatdate(row.due_date, "dd/mm/yyyy")
	)
	name, phone = recipient(deal, row)
	if not phone:
		return _finish(log, deal, NOT_SENT, _("no mobile number of the sacado"), label)
	cedente = frappe.db.get_value("CRM Organization", row.cedente, "organization_name") if row.get("cedente") else ""
	values = variable_values(row, {"sacado": _sacado_name(deal, row), "cedente": cedente}, on_day)
	template = _template(inbox, step.template_name, step.template_language)
	if not template:
		return _finish(log, deal, FAILED, _("the template {0} is no longer approved").format(step.template_name), label)
	params = {}
	for variable, info in zip(template["variables"], load_variables(step.variables)):
		if not values.get(info):
			return _finish(log, deal, NOT_SENT, _("the título has no {0}").format(_(VARIABLES.get(info, info)).lower()), label)
		params[variable] = values[info]
	log.phone = phone
	try:
		conversation = _deliver(inbox, template, name, phone, params)
	except welcome.ChatError as e:
		return _finish(log, deal, FAILED, str(e), label)
	# Delivered: recorded at once, so nothing after this (the card comment) can mark it as failed
	log.db_set({"status": SENT, "conversation": conversation, "phone": phone, "error": None})
	frappe.db.commit()
	return _finish(log, deal, SENT, "", label, template=step.template_name, phone=phone)


def _deliver(inbox, template, name, phone, params) -> str:
	"""New conversation with the template and no agent assigned: the SmartChat's AI agent takes it."""
	connection, token = inbox["connection"], inbox["token"]
	contact = welcome._contact(connection.account_id, token, name, phone)
	content = welcome.VARIABLE.sub(lambda m: params.get(m.group(1), m.group(0)), template["body"])
	conversation = welcome._call(
		"POST",
		f"/api/v1/accounts/{connection.account_id}/conversations",
		token,
		json={
			"contact_id": contact,
			"inbox_id": inbox["id"],
			"message": {
				"content": content,
				"template_params": {
					"name": template["name"],
					"category": template["category"],
					"language": template["language"],
					"processed_params": {"body": params},
				},
			},
		},
	)
	return str(conversation.get("id") or "")


def _finish(log, deal, status, reason, label, template=None, phone=None) -> str:
	log.status, log.error = status, (reason or None) and reason[:500]
	log.save(ignore_permissions=True)
	day = f"D{cint(log.days):+d}" if cint(log.days) else "D0"
	if status == SENT:
		text = _("Collection reminder {0} of {1} sent to the WhatsApp {2} (template {3}).").format(day, label, phone, template)
	else:
		text = _("Collection reminder {0} of {1} was not sent: {2}.").format(day, label, reason)
	deal.add_comment("Info", frappe.utils.escape_html(text))
	return status
