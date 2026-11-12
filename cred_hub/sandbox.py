"""Test keys and the Sandbox pipeline of the HubTech API (credtech#375, ADR 0007 section 11).

A test key (`is_test`, token `hub_test<id>_<secret>`) is a normal key that can only touch sandbox data:

- Sandbox pipeline: created once per company, when the admin creates the first test key, and flagged
  `credhub_sandbox` on CRM Pipeline. A test key always has exactly this pipeline (and a normal key never has it),
  so the CRM pipeline permissions already confine its cards to the sandbox.
- Sandbox records: cards of the Sandbox pipeline, and records created by a test key (owner = technical user of a
  test key). For the technical user of a test key, Contact, Organization, Lead, Note, Task, File and the Digital
  Folder records are filtered to sandbox records on every read (permission query) and every document check
  (has_permission): it never sees nor changes real data, whatever the route.
- Writes (CRM Deal.validate): a test key only writes cards of the Sandbox pipeline; a card of the Sandbox pipeline
  only links to sandbox organizations and contacts; a card only enters the Sandbox pipeline when a test key creates
  it there (people still open and move sandbox cards, which is how webhooks are tested).
- Never paid: a test key cannot have `credit_paid_queries` nor `fields_admin` (company-wide fields), the paid routes
  are refused at the route check, and the automatic credit check never runs for a sandbox card.
- Webhooks: events of sandbox records go only to webhooks flagged `is_test`, and those get nothing else.
- Every answer to a test key carries `X-HubTech-Test: 1`.
"""

import frappe
from frappe import _

KEY = "Cred Hub API Key"
PIPELINE_FLAG = "credhub_sandbox"
PIPELINE_NAME = "Sandbox (API de teste)"
TEST_HEADER = "X-HubTech-Test"
TEST_KEY_ID_PREFIX = "test"
# Scopes a test key never gets: paid bureau queries and company-wide field changes
TEST_FORBIDDEN_SCOPES = frozenset({"credit_paid_queries", "fields_admin"})
TEST_USERS_CACHE = "credhub_api_test_users"
TEST_USERS_CACHE_SECONDS = 600
# Records a test user only sees when they are sandbox records: created by a test key (owner), or tied to a card
# (notes, tasks and files of cards follow the card, which the CRM already limits to the Sandbox pipeline)
OWNED = ("Contact", "CRM Organization", "CRM Lead")
CARD_LINKED = {
	"FCRM Note": "reference_doctype",
	"CRM Task": "reference_doctype",
	"File": "attached_to_doctype",
}
ORGANIZATION_LINKED = ("Cred Hub Checklist Item", "Cred Hub Visit Report", "Cred Hub Credit Report")
GUARDED_DOCTYPES = (*OWNED, *CARD_LINKED, *ORGANIZATION_LINKED)


class SandboxViolation(frappe.PermissionError):
	"""A test key tried to reach real data, or real data tried to enter the sandbox (403)."""


# ---------- Who and what is sandbox ----------


def test_users() -> frozenset:
	"""Technical users of every test key (revoked included: what they created stays sandbox data). Cached."""
	cached = frappe.cache.get_value(TEST_USERS_CACHE)
	if cached is not None:
		return frozenset(cached)
	users = []
	if frappe.get_meta(KEY).has_field("is_test"):
		users = [u for u in frappe.get_all(KEY, {"is_test": 1}, pluck="technical_user") if u]
	frappe.cache.set_value(TEST_USERS_CACHE, users, expires_in_sec=TEST_USERS_CACHE_SECONDS)
	return frozenset(users)


def clear_test_users_cache():
	frappe.cache.delete_value(TEST_USERS_CACHE)


def is_test_user(user: str | None = None) -> bool:
	return (user or frappe.session.user) in test_users()


def sandbox_pipeline() -> str | None:
	if not frappe.get_meta("CRM Pipeline").has_field(PIPELINE_FLAG):
		return None
	return frappe.db.get_value("CRM Pipeline", {PIPELINE_FLAG: 1}, "name", order_by="creation asc")


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
	return name


def _owned_by_test(doctype: str, name: str | None) -> bool:
	if not name:
		return True
	owner = frappe.db.get_value(doctype, name, "owner")
	return owner is None or owner in test_users()


def is_sandbox_record(doctype: str | None, name: str | None, doc=None) -> bool:
	"""A card of the Sandbox pipeline, or a record created by a test key."""
	if not doctype or not name:
		return False
	if doctype == "CRM Deal":
		pipeline = doc.get("pipeline") if doc is not None else frappe.db.get_value("CRM Deal", name, "pipeline")
		return is_sandbox_pipeline(pipeline)
	owner = doc.get("owner") if doc is not None else frappe.db.get_value(doctype, name, "owner")
	return bool(owner) and owner in test_users()


# ---------- Permissions (only for technical users of test keys) ----------


def _escaped(values) -> str:
	return ", ".join(frappe.db.escape(v) for v in sorted(values)) or "''"


def query_conditions(user=None, doctype=None) -> str:
	"""permission_query_conditions of GUARDED_DOCTYPES: empty for everyone but a test key's technical user."""
	user = user or frappe.session.user
	users = test_users()
	if user not in users or doctype not in GUARDED_DOCTYPES:
		return ""
	table = f"`tab{doctype}`"
	owned = f"{table}.`owner` in ({_escaped(users)})"
	if doctype in CARD_LINKED:
		# a note, task or file of a card follows the card (CRM / api_guard rules); anything else only if sandbox
		return f"({owned} or {table}.`{CARD_LINKED[doctype]}` = 'CRM Deal')"
	if doctype in ORGANIZATION_LINKED:
		return f"{table}.`organization` in (select `name` from `tabCRM Organization` where `owner` in ({_escaped(users)}))"
	return owned


def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
	"""Controller check of GUARDED_DOCTYPES: False only for a test key's technical user on a non-sandbox record.
	(A hook can only deny; True keeps the role permissions as they are.)"""
	user = user or frappe.session.user
	if user not in test_users() or doc.doctype not in GUARDED_DOCTYPES:
		return True
	if doc.is_new():
		return _new_record_allowed(doc)
	if doc.doctype in CARD_LINKED:
		if doc.get(CARD_LINKED[doc.doctype]) == "CRM Deal":
			return True
		return doc.get("owner") in test_users()
	if doc.doctype in ORGANIZATION_LINKED:
		return _owned_by_test("CRM Organization", doc.get("organization")) and bool(doc.get("organization"))
	return doc.get("owner") in test_users()


def _new_record_allowed(doc) -> bool:
	if doc.doctype in ORGANIZATION_LINKED:
		return bool(doc.get("organization")) and _owned_by_test("CRM Organization", doc.get("organization"))
	return True


# ---------- Writes ----------


def _refuse(message: str):
	frappe.throw(message, SandboxViolation)


def validate_deal(doc, method=None):
	"""CRM Deal.validate: test keys write only sandbox cards, and sandbox cards only link to sandbox records."""
	sandbox = sandbox_pipeline()
	in_sandbox = bool(sandbox) and doc.get("pipeline") == sandbox
	test_session = is_test_user()
	if test_session and not in_sandbox:
		_refuse(_("A test API key only works with cards of the Sandbox pipeline."))
	if not in_sandbox:
		return
	entering = doc.is_new() or doc.has_value_changed("pipeline")
	if entering and not test_session:
		_refuse(_("Only a test API key creates cards in the Sandbox pipeline; real cards never move into it."))
	for field in ("organization", "second_organization"):
		if doc.get(field) and not _owned_by_test("CRM Organization", doc.get(field)):
			_refuse(_("A card of the Sandbox pipeline only links to organizations created by a test API key."))
	for row in doc.get("contacts") or []:
		if row.get("contact") and not _owned_by_test("Contact", row.get("contact")):
			_refuse(_("A card of the Sandbox pipeline only links to contacts created by a test API key."))


def validate_contact(doc, method=None):
	"""Contact.validate: a test key's contact only links to a sandbox organization."""
	if is_test_user() and doc.get("company_name") and not _owned_by_test("CRM Organization", doc.company_name):
		_refuse(_("A test API key only links contacts to organizations it created."))


def validate_lead(doc, method=None):
	"""CRM Lead.validate: a lead sent by a test key only becomes a card of the Sandbox pipeline."""
	if is_test_user() and doc.get("pipeline") and not is_sandbox_pipeline(doc.get("pipeline")):
		_refuse(_("A test API key only sends leads to the Sandbox pipeline."))


def validate_organization_record(doc, method=None):
	"""Digital Folder records written in a test key's call: only for sandbox organizations."""
	if is_test_user() and not (doc.get("organization") and _owned_by_test("CRM Organization", doc.organization)):
		_refuse(_("A test API key only works with organizations it created."))


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

	create_custom_fields(
		{
			"CRM Pipeline": [
				{
					"fieldname": PIPELINE_FLAG,
					"fieldtype": "Check",
					"label": "API Sandbox",
					"description": "Pipeline of the test API keys (credtech#375)",
					"read_only": 1,
					"hidden": 1,
					"insert_after": "is_active",
				}
			]
		},
		update=True,
	)
