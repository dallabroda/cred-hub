"""Test keys and the Sandbox pipeline of the HubTech API (credtech#375, ADR 0007 section 11).

A test key (`is_test`, token `hub_test<id>_<secret>`) is a normal key that can only touch sandbox data:

- Sandbox pipeline: created once per company, when the admin creates the first test key, and flagged
  `credhub_sandbox` on CRM Pipeline. A test key always has exactly this pipeline (and a normal key never has it),
  so the CRM pipeline permissions already confine its cards to the sandbox.
- Sandbox records: cards of the Sandbox pipeline; records created in a test key's call, stamped with the indexed,
  read-only column `credhub_sandbox` (Contact, Organization, Lead, Note, Task, File); notes, tasks and files of
  Sandbox cards; Digital Folder records of sandbox organizations. For the technical user of a test key every list
  (permission query) and every document check (has_permission) is limited to them, whatever the route, and pipelines
  and stages to the Sandbox ones.
- No oracle on real data: in a test key's call a link to a real record fails exactly like a link to a missing one,
  lead conversion only matches sandbox contacts and organizations, and sandbox organizations live in their own name
  space (`<name> [sandbox]`), so a test key never learns that a real name, e-mail, phone or document exists.
- Writes (CRM Deal.validate): a test key only writes Sandbox cards; Sandbox cards only link sandbox records; real
  cards never move in and Sandbox cards never move out.
- No paid or outbound side effects for sandbox records: no paid queries (scopes refused, automatic credit check
  skipped), no ERP cedente, no protest, no collection reminders, no CNPJ/company/e-mail lookups, no Google Calendar.
- Webhooks: events of sandbox records go only to webhooks flagged `is_test`, and those get nothing else.
  Deliveries to them carry `X-HubTech-Test: 1` and `"test": true` in the body (`false` for every other webhook).
- Every answer to a test key carries `X-HubTech-Test: 1`.
- A test key never assigns nor @mentions a person (owner fields refused, mentions become plain text).
- The Sandbox pipeline stays while in use: nobody (admin included) deletes or renames it, nor deletes its stages,
  while a test key is not revoked or test data remains (then the admin may remove it). Automation flows,
  Frappe notifications/webhooks, assignment, distribution and the ERPNext sync of the crm skip sandbox records
  (crm.sandbox).
"""

import frappe
from frappe import _

KEY = "Cred Hub API Key"
PIPELINE_FLAG = "credhub_sandbox"
RECORD_FLAG = "credhub_sandbox"
PIPELINE_NAME = "Sandbox (API de teste)"
ORGANIZATION_SUFFIX = " [sandbox]"
TEST_HEADER = "X-HubTech-Test"
TEST_KEY_ID_PREFIX = "test"
# Scopes a test key never gets: paid bureau queries and company-wide field changes
TEST_FORBIDDEN_SCOPES = frozenset({"credit_paid_queries", "fields_admin"})
TEST_USERS_CACHE = "credhub_api_test_users"
TEST_USERS_CACHE_SECONDS = 600
# Records stamped with credhub_sandbox when created in a test key's call
STAMPED = ("Contact", "CRM Organization", "CRM Lead", "FCRM Note", "CRM Task", "File")
# Records of a card: sandbox when stamped or when their card is a Sandbox card (field with the doctype, field with
# the name)
CARD_LINKED = {
	"FCRM Note": ("reference_doctype", "reference_docname"),
	"CRM Task": ("reference_doctype", "reference_docname"),
	"File": ("attached_to_doctype", "attached_to_name"),
}
ORGANIZATION_LINKED = ("Cred Hub Checklist Item", "Cred Hub Visit Report", "Cred Hub Credit Report")
GUARDED_DOCTYPES = (*STAMPED, *ORGANIZATION_LINKED, "CRM Pipeline", "CRM Deal Status")


class SandboxViolation(frappe.PermissionError):
	"""Real data tried to enter the sandbox, or a sandbox card to leave it (403)."""


# ---------- Who and what is sandbox ----------


def test_users() -> frozenset:
	"""Technical users of every test key (revoked included). Cached in Redis and memoized for the request: it is read
	by every permission check of every user."""
	memo = getattr(frappe.local, "credhub_test_users", None)
	if memo is not None:
		return memo
	cached = frappe.cache.get_value(TEST_USERS_CACHE)
	if cached is None:
		cached = []
		if frappe.get_meta(KEY).has_field("is_test"):
			cached = [u for u in frappe.get_all(KEY, {"is_test": 1}, pluck="technical_user") if u]
		frappe.cache.set_value(TEST_USERS_CACHE, cached, expires_in_sec=TEST_USERS_CACHE_SECONDS)
	frappe.local.credhub_test_users = frozenset(cached)
	return frappe.local.credhub_test_users


def clear_test_users_cache():
	frappe.cache.delete_value(TEST_USERS_CACHE)
	frappe.local.credhub_test_users = None
	frappe.local.credhub_sandbox_pipeline = None


def is_test_user(user: str | None = None) -> bool:
	return (user or frappe.session.user) in test_users()


def sandbox_pipeline() -> str | None:
	memo = getattr(frappe.local, "credhub_sandbox_pipeline", None)
	if memo is not None:
		return memo or None
	name = None
	if frappe.get_meta("CRM Pipeline").has_field(PIPELINE_FLAG):
		name = frappe.db.get_value("CRM Pipeline", {PIPELINE_FLAG: 1}, "name", order_by="creation asc")
	frappe.local.credhub_sandbox_pipeline = name or ""
	return name


def is_sandbox_pipeline(pipeline: str | None) -> bool:
	return bool(pipeline) and pipeline == sandbox_pipeline()


def ensure_sandbox_pipeline() -> str:
	"""The company's Sandbox pipeline, created on first use (by the admin creating a test key)."""
	existing = sandbox_pipeline()
	if existing:
		return existing
	from crm.api.pipeline import save_pipeline

	name = save_pipeline(
		{
			"pipeline_name": PIPELINE_NAME,
			"stages": [
				{"deal_status": "Novo", "type": "Open"},
				{"deal_status": "Em andamento", "type": "Ongoing"},
				{"deal_status": "Ganho", "type": "Won"},
				{"deal_status": "Perdido", "type": "Lost"},
			],
		}
	)["name"]
	frappe.db.set_value("CRM Pipeline", name, PIPELINE_FLAG, 1, update_modified=False)
	frappe.local.credhub_sandbox_pipeline = name
	return name


def _flagged(doctype: str, name: str | None) -> bool:
	return bool(name) and bool(frappe.db.get_value(doctype, name, RECORD_FLAG))


def is_sandbox_record(doctype: str | None, name: str | None, doc=None) -> bool:
	"""A Sandbox card, a stamped record, a note/task/file of a Sandbox card, or Digital Folder data of a sandbox
	organization."""
	if not doctype or not name:
		return False
	if doctype == "CRM Deal":
		pipeline = doc.get("pipeline") if doc is not None else frappe.db.get_value("CRM Deal", name, "pipeline")
		return is_sandbox_pipeline(pipeline)
	if doctype in STAMPED:
		if (doc.get(RECORD_FLAG) if doc is not None else _flagged(doctype, name)):
			return True
		if doctype in CARD_LINKED:
			kind, ref = CARD_LINKED[doctype]
			values = doc if doc is not None else frappe.db.get_value(doctype, name, [kind, ref], as_dict=True) or {}
			return values.get(kind) == "CRM Deal" and is_sandbox_record("CRM Deal", values.get(ref))
		return False
	if doctype in ORGANIZATION_LINKED:
		org = doc.get("organization") if doc is not None else frappe.db.get_value(doctype, name, "organization")
		return _flagged("CRM Organization", org)
	return False


def same_space(doctype: str) -> dict:
	"""Filter of the lookups by CPF/CNPJ that reuse a record (imports, ERP sync, card from a lead): real data never
	links a sandbox record (a real collection card on a test contact would reach it), and a test key only its own."""
	if not frappe.get_meta(doctype).has_field(RECORD_FLAG):
		return {}
	return {RECORD_FLAG: 1 if is_test_user() else 0}


def skip_side_effects(doc) -> bool:
	"""Paid or outbound work (lookups, ERP, protest, calendar...) never runs for sandbox data."""
	return is_test_user() or is_sandbox_record(doc.get("doctype"), doc.get("name"), doc)


# ---------- Permissions (only for technical users of test keys) ----------


def query_conditions(user=None, doctype=None) -> str:
	"""permission_query_conditions of GUARDED_DOCTYPES: empty for everyone but a test key's technical user."""
	user = user or frappe.session.user
	if doctype not in GUARDED_DOCTYPES or user not in test_users():
		return ""
	table = f"`tab{doctype}`"
	sandbox = frappe.db.escape(sandbox_pipeline() or "")
	if doctype == "CRM Pipeline":
		return f"{table}.`name` = {sandbox}"
	if doctype == "CRM Deal Status":
		return f"{table}.`pipeline` = {sandbox}"
	if doctype in ORGANIZATION_LINKED:
		return f"{table}.`organization` in (select `name` from `tabCRM Organization` where `{RECORD_FLAG}` = 1)"
	flagged = f"{table}.`{RECORD_FLAG}` = 1"
	if doctype in CARD_LINKED:
		kind, ref = CARD_LINKED[doctype]
		return (
			f"({flagged} or ({table}.`{kind}` = 'CRM Deal' and {table}.`{ref}` in "
			f"(select `name` from `tabCRM Deal` where `pipeline` = {sandbox})))"
		)
	return flagged


def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
	"""Controller check of GUARDED_DOCTYPES: False only for a test key's technical user on a non-sandbox record.
	(A hook can only deny; True keeps the role permissions as they are.)"""
	user = user or frappe.session.user
	if doc.doctype not in GUARDED_DOCTYPES or user not in test_users():
		return True
	if doc.doctype == "CRM Pipeline":
		return is_sandbox_pipeline(doc.name)
	if doc.doctype == "CRM Deal Status":
		return is_sandbox_pipeline(doc.get("pipeline"))
	if ptype == "create" or doc.is_new() or not doc.name:
		# a document built from a dict is not flagged new until inserted: "create" or no name means new
		return _new_record_allowed(doc)
	return is_sandbox_record(doc.doctype, doc.name, doc)


def _new_record_allowed(doc) -> bool:
	if doc.doctype in ORGANIZATION_LINKED:
		return _flagged("CRM Organization", doc.get("organization"))
	if doc.doctype in CARD_LINKED:
		kind, ref = CARD_LINKED[doc.doctype]
		if doc.get(kind) and doc.get(ref):
			# a note, task or file goes only on a sandbox record (a Sandbox card, or a record created by a test key)
			return is_sandbox_record(doc.get(kind), doc.get(ref))
	return True


# ---------- Stamp and keep the flag ----------


def stamp(doc, method=None):
	"""before_insert of STAMPED: records created in a test key's call are sandbox records. Sandbox organizations get
	their own name space, so a test key never collides with (and so never learns of) a real organization."""
	if not doc.meta.has_field(RECORD_FLAG):
		return
	test = is_test_user()
	doc.set(RECORD_FLAG, 1 if test else 0)
	if test and doc.doctype == "CRM Organization":
		doc.organization_name = sandbox_organization_name(doc.organization_name)


def keep_flag(doc, method=None):
	"""validate of STAMPED: nobody sets or clears the flag by hand (it is read only for keys too, api_guard)."""
	if doc.is_new() or not doc.meta.has_field(RECORD_FLAG):
		return
	stored = frappe.db.get_value(doc.doctype, doc.name, RECORD_FLAG)
	doc.set(RECORD_FLAG, stored or 0)
	if stored and doc.doctype == "CRM Organization" and doc.has_value_changed("organization_name"):
		doc.organization_name = sandbox_organization_name(doc.organization_name)


def sandbox_organization_name(name: str | None) -> str | None:
	if not name or name.endswith(ORGANIZATION_SUFFIX):
		return name
	return f"{name}{ORGANIZATION_SUFFIX}"


# ---------- Writes ----------


def _not_found(label: str, name: str):
	"""In a test key's call a real record answers exactly like a missing one (no oracle)."""
	frappe.throw(_("Could not find {0}: {1}").format(_(label), name), frappe.LinkValidationError)


def _refuse_link(label: str, name: str, message: str):
	if is_test_user():
		_not_found(label, name)
	frappe.throw(message, SandboxViolation)


def validate_deal(doc, method=None):
	"""CRM Deal.validate: test keys write only Sandbox cards; Sandbox cards only link sandbox records and never leave
	the Sandbox; real cards never enter it."""
	sandbox = sandbox_pipeline()
	in_sandbox = bool(sandbox) and doc.get("pipeline") == sandbox
	test_session = is_test_user()
	if not doc.is_new() and doc.has_value_changed("pipeline"):
		before = doc.get_doc_before_save()
		if before is not None and is_sandbox_pipeline(before.get("pipeline")) and not in_sandbox:
			frappe.throw(_("A card of the Sandbox pipeline never moves to a real pipeline."), SandboxViolation)
	if test_session and not in_sandbox:
		frappe.throw(_("A test API key only works with cards of the Sandbox pipeline."), SandboxViolation)
	if not in_sandbox:
		return
	if (doc.is_new() or doc.has_value_changed("pipeline")) and not test_session:
		frappe.throw(
			_("Only a test API key creates cards in the Sandbox pipeline; real cards never move into it."),
			SandboxViolation,
		)
	message = _("A card of the Sandbox pipeline only links to records created by a test API key.")
	for field in ("organization", "second_organization"):
		if doc.get(field) and not _flagged("CRM Organization", doc.get(field)):
			_refuse_link("Organization", doc.get(field), message)
	for row in doc.get("contacts") or []:
		if row.get("contact") and not _flagged("Contact", row.get("contact")):
			_refuse_link("Contact", row.get("contact"), message)


def validate_contact(doc, method=None):
	"""Contact.validate: a test key's contact only links to a sandbox organization."""
	if is_test_user() and doc.get("company_name") and not _flagged("CRM Organization", doc.company_name):
		_not_found("Organization", doc.company_name)


def validate_lead(doc, method=None):
	"""CRM Lead.validate: a lead sent by a test key only becomes a card of the Sandbox pipeline."""
	if is_test_user() and doc.get("pipeline") and not is_sandbox_pipeline(doc.get("pipeline")):
		_not_found("Pipeline", doc.get("pipeline"))


def validate_organization_record(doc, method=None):
	"""Digital Folder records written in a test key's call: only for sandbox organizations."""
	if is_test_user() and not _flagged("CRM Organization", doc.get("organization")):
		_not_found("Organization", doc.get("organization") or "")


class SandboxLeadMixin:
	"""CRM Lead conversion in a test key's call only matches sandbox contacts and organizations: the global matches
	of the CRM (e-mail, phone, organization name) would link a real record, and refusing that would tell the key it
	exists."""

	def contact_exists(self, throw=True):
		if not is_test_user():
			return super().contact_exists(throw)
		for value, table, field in (
			(self.get("email"), "Contact Email", "email_id"),
			(self.get("phone"), "Contact Phone", "phone"),
			(self.get("mobile_no"), "Contact Phone", "phone"),
		):
			if not value:
				continue
			rows = frappe.db.sql(
				f"""select c.name from `tab{table}` t join `tabContact` c on c.name = t.parent
				where t.`{field}` = %s and c.`{RECORD_FLAG}` = 1 limit 1""",
				(value,),
			)
			if rows:
				return rows[0][0]
		return False

	def create_organization(self, existing_organization=None):
		if is_test_user() and self.get("organization") and not existing_organization:
			# look up and create only in the sandbox name space
			self.organization = sandbox_organization_name(self.organization)
		return super().create_organization(existing_organization)


# ---------- Side effects that must not run for sandbox data ----------


def erp_cedente_on_deal_update(doc, method=None):
	"""Wraps cred_hub.erp.cedente.on_deal_update: a Sandbox card never goes to the client's ERP."""
	if skip_side_effects(doc):
		return
	from cred_hub.erp import cedente

	return cedente.on_deal_update(doc, method)


# ---------- Nobody is assigned or mentioned by a test key ----------

# Fields that hand a record to a person: Frappe's assignment (ToDo) notifies and e-mails them
OWNER_FIELDS = {"CRM Deal": ("deal_owner",), "CRM Lead": ("lead_owner",), "CRM Task": ("assigned_to",)}
# Rich text where an @mention notifies the person (Frappe and crm notifications, e-mail)
MENTION_FIELDS = {"Comment": "content", "FCRM Note": "content", "CRM Task": "description"}


def refuse_assignment(doc, method=None):
	"""before_validate of CRM Deal, CRM Lead and CRM Task (before the crm assigns in validate/after_insert): a test
	key never hands a record to a person. The owner stays empty or the key's own technical user; `_assign` is never
	changed. Owners set by the team on the screen are kept."""
	if not is_test_user():
		return
	before = None if doc.is_new() else doc.get_doc_before_save()
	for field in OWNER_FIELDS.get(doc.doctype, ()):
		value = doc.get(field)
		changed = doc.is_new() or before is None or before.get(field) != value
		if changed and value and value != frappe.session.user:
			_refuse_people(field)
	assigned = doc.get("_assign")
	if (assigned and assigned != "[]") if doc.is_new() else (before is not None and before.get("_assign") != assigned):
		_refuse_people("_assign")


def _refuse_people(field: str):
	frappe.throw(
		_("A test API key cannot assign records to people ({0}): leave it empty.").format(field),
		frappe.ValidationError,
		title=_("Sandbox"),
	)


def strip_mentions(doc, method=None):
	"""validate of Comment, FCRM Note and CRM Task: an @mention written by a test key becomes plain text, so nobody is
	notified (Frappe's mention e-mail and the crm's notification both read the mention markup)."""
	field = MENTION_FIELDS.get(doc.doctype)
	if not field or not is_test_user() or not doc.get(field) or "mention" not in doc.get(field):
		return
	doc.set(field, plain_mentions(doc.get(field)))


def plain_mentions(html: str) -> str:
	from bs4 import BeautifulSoup

	soup = BeautifulSoup(html, "html.parser")
	found = soup.find_all(class_="mention") + soup.find_all(attrs={"data-type": "mention"})
	if not found:
		return html
	for mention in found:
		if mention.parent is not None:
			mention.replace_with(mention.get_text())
	return str(soup)


# ---------- The Sandbox pipeline stays while it is in use ----------


def _flagged_pipeline(name: str | None) -> bool:
	return (
		bool(name)
		and frappe.get_meta("CRM Pipeline").has_field(PIPELINE_FLAG)
		and bool(frappe.db.get_value("CRM Pipeline", name, PIPELINE_FLAG))
	)


def sandbox_in_use(pipeline: str) -> list[str]:
	"""What still uses the Sandbox pipeline: test keys not revoked, its cards and leads, records stamped as test
	data. Empty means the admin may delete or rename it (the next test key creates a new one)."""
	uses = []
	if frappe.get_meta(KEY).has_field("is_test"):
		keys = frappe.db.count(KEY, {"is_test": 1, "status": ("!=", "Revoked")})
		if keys:
			uses.append(_("{0} test API key(s) not revoked").format(keys))
	for doctype, label, filters in (
		("CRM Deal", _("card(s)"), {"pipeline": pipeline}),
		("CRM Lead", _("lead(s)"), {"pipeline": pipeline}),
	):
		count = frappe.db.count(doctype, filters)
		if count:
			uses.append(f"{count} {label}")
	for doctype in STAMPED:
		if frappe.get_meta(doctype).has_field(RECORD_FLAG):
			count = frappe.db.count(doctype, {RECORD_FLAG: 1})
			if count:
				uses.append(f"{count} {_(doctype)}")
	return uses


def _keep_pipeline(pipeline: str):
	uses = sandbox_in_use(pipeline)
	if not uses:
		return
	frappe.throw(
		_(
			"The {0} pipeline is still in use by the test API keys ({1}). To delete or rename it (or delete its "
			"stages), revoke the test keys and delete the test data first: the Sandbox cards, then the "
			"[sandbox] organizations, test contacts, leads, notes, tasks and files."
		).format(frappe.bold(PIPELINE_NAME), ", ".join(uses)),
		frappe.ValidationError,
		title=_("Sandbox pipeline"),
	)


def validate_pipeline(doc, method=None):
	"""CRM Pipeline.validate: the Sandbox pipeline keeps its name while in use (and its flag, always)."""
	if doc.is_new() or not _flagged_pipeline(doc.name):
		return
	if doc.has_value_changed("pipeline_name"):
		_keep_pipeline(doc.name)
	if doc.meta.has_field(PIPELINE_FLAG):
		doc.set(PIPELINE_FLAG, 1)


def protect_pipeline(doc, method=None, *args, **kwargs):
	"""CRM Pipeline.on_trash and before_rename: the Sandbox pipeline is not deleted nor renamed while in use. Once
	free, its deletion also drops it from the revoked test keys (their history stays)."""
	if not _flagged_pipeline(doc.name):
		return
	_keep_pipeline(doc.name)
	if method == "on_trash":
		frappe.db.delete("Cred Hub API Key Pipeline", {"parenttype": KEY, "pipeline": doc.name})
		frappe.local.credhub_sandbox_pipeline = None


def protect_stage(doc, method=None):
	"""CRM Deal Status.on_trash: no stage of the Sandbox pipeline is deleted while in use (deleting the pipeline
	also goes through here, as the crm deletes its stages first)."""
	if _flagged_pipeline(doc.get("pipeline")):
		_keep_pipeline(doc.get("pipeline"))


# ---------- Keys ----------


def check_test_scopes(values: dict):
	"""A test key never gets paid queries nor company-wide field changes."""
	forbidden = sorted(scope for scope in TEST_FORBIDDEN_SCOPES if values.get(scope))
	if forbidden:
		frappe.throw(_("A test API key cannot have these permissions: {0}").format(", ".join(forbidden)))


def check_not_sandbox(pipelines: list[str]):
	"""A real key never gets the Sandbox pipeline (its data must stay apart)."""
	sandbox = sandbox_pipeline()
	if sandbox and sandbox in pipelines:
		frappe.throw(_("The Sandbox pipeline is only for test API keys."))


def install_sandbox():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	flag = {
		"fieldname": RECORD_FLAG,
		"fieldtype": "Check",
		"label": "API Sandbox",
		"description": "Created by a test API key (credtech#375)",
		"read_only": 1,
		"hidden": 1,
		"no_copy": 1,
		"search_index": 1,
	}
	fields = {doctype: [dict(flag)] for doctype in STAMPED}
	fields["CRM Pipeline"] = [
		{**flag, "description": "Pipeline of the test API keys (credtech#375)", "insert_after": "is_active"}
	]
	create_custom_fields(fields, update=True)
