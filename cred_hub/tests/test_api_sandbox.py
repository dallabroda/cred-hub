"""Test keys and the Sandbox pipeline (credtech#375, ADR 0007 section 11): a test key never sees nor changes real
data, never runs paid queries, and its events only reach test webhooks.
Run: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_api_sandbox`."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import Headers, MultiDict

from cred_hub import api_keys, sandbox, webhooks

DEAL = "CRM Deal"
SCOPES = [
	"cards_read",
	"cards_write",
	"contacts_read",
	"contacts_write",
	"organizations_read",
	"organizations_write",
	"leads_write",
	"pipelines_read",
	"allow_delete",
	"files_read",
	"files_write",
	"credit_read",
	"credit_write",
	"sync_deletes",
]


class TestApiSandbox(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		sandbox.clear_test_users_cache()
		webhooks.clear_active_cache()
		from crm.api.pipeline import save_pipeline

		self.real = save_pipeline({"pipeline_name": "Real", "stages": [{"deal_status": "Novo"}]})["name"]
		suffix = frappe.generate_hash(length=6)
		self.real_org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"Real {suffix}"}).insert()
		self.real_contact = frappe.get_doc({"doctype": "Contact", "first_name": f"Real {suffix}"}).insert()
		self.created = self.create_key(test=True)
		self.token = self.created["token"]
		self.sandbox = sandbox.sandbox_pipeline()
		frappe.local.response_headers = Headers()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.local.credhub_api_is_test = False
		frappe.db.rollback()
		sandbox.clear_test_users_cache()
		webhooks.clear_active_cache()

	def create_key(self, test=False, scopes=SCOPES, pipelines=None, label="ERP"):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key(label, list(scopes), pipelines or [self.real], is_test=int(test))

	def call(self, token=None, path="/api/resource/CRM Deal", method="GET", form=None):
		"""Authenticate as a real call; the session stays as the key's technical user (as in the handler)."""
		token = token or self.token
		request = MagicMock(path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}), files=MultiDict())
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		frappe.local.response_headers = Headers()
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, form or {}, clear=True),
		):
			frappe.local.request_ip = "203.0.113.9"
			api_keys.authenticate()
		return frappe.local.response_headers

	def as_test_key(self):
		self.call()
		return frappe.session.user

	def sandbox_org(self):
		self.as_test_key()
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"Teste {frappe.generate_hash(length=6)}"}).insert()

	# ---------- Keys ----------

	def test_test_key_is_bound_to_the_sandbox_pipeline(self):
		self.assertTrue(self.token.startswith("hub_test"))
		self.assertEqual(self.created["pipelines"], [self.sandbox])
		self.assertEqual(self.created["is_test"], 1)
		self.assertEqual(frappe.db.get_value("CRM Pipeline", self.sandbox, sandbox.PIPELINE_FLAG), 1)
		# a second test key shares the same sandbox, whatever pipelines it asks for
		second = self.create_key(test=True, pipelines=[self.real], label="Outra")
		self.assertEqual(second["pipelines"], [self.sandbox])
		self.assertEqual(frappe.db.count("CRM Pipeline", {sandbox.PIPELINE_FLAG: 1}), 1)
		updated = api_keys.update_api_key(second["name"], pipelines=[self.real])
		self.assertEqual(updated["pipelines"], [self.sandbox])

	def test_test_key_never_gets_paid_or_company_wide_scopes(self):
		for scope in sorted(sandbox.TEST_FORBIDDEN_SCOPES):
			with self.assertRaises(frappe.ValidationError):
				self.create_key(test=True, scopes=[*SCOPES, scope])
			with self.assertRaises(frappe.ValidationError):
				api_keys.update_api_key(self.created["name"], scopes=[*SCOPES, scope])

	def test_paid_route_refused_even_if_the_scope_slipped_in(self):
		frappe.db.set_value(api_keys.KEY, self.created["name"], "credit_paid_queries", 1)
		with self.assertRaises(api_keys.KeyNotAllowed):
			self.call(path="/api/method/cred_hub.credit_check.api.trigger_check", method="POST")

	def test_real_key_never_gets_the_sandbox_pipeline(self):
		with self.assertRaises(frappe.ValidationError):
			self.create_key(pipelines=[self.sandbox])
		real = self.create_key(label="Real")
		with self.assertRaises(frappe.ValidationError):
			api_keys.update_api_key(real["name"], pipelines=[self.real, self.sandbox])

	def test_answers_say_test_mode(self):
		self.assertEqual(self.call().get(sandbox.TEST_HEADER), "1")
		real = self.create_key(label="Real")
		self.assertIsNone(self.call(token=real["token"]).get(sandbox.TEST_HEADER))

	# ---------- Writes ----------

	def test_test_key_writes_only_sandbox_cards(self):
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		self.assertEqual(card.pipeline, self.sandbox)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": DEAL, "pipeline": self.real}).insert()

	def test_sandbox_card_only_links_sandbox_records(self):
		org = self.sandbox_org()
		frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox, "organization": org.name}).insert()
		with self.assertRaises(sandbox.SandboxViolation):
			frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox, "organization": self.real_org.name}).insert()
		with self.assertRaises(sandbox.SandboxViolation):
			frappe.get_doc(
				{"doctype": DEAL, "pipeline": self.sandbox, "contacts": [{"contact": self.real_contact.name}]}
			).insert()

	def test_real_cards_never_enter_the_sandbox(self):
		with self.assertRaises(sandbox.SandboxViolation):
			frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		real_card = frappe.get_doc({"doctype": DEAL, "pipeline": self.real}).insert()
		real_card.pipeline = self.sandbox
		real_card.status = frappe.get_all(
			"CRM Deal Status", {"pipeline": self.sandbox}, pluck="name", order_by="position asc"
		)[0]
		with self.assertRaises(sandbox.SandboxViolation):
			real_card.save()

	def test_people_can_still_move_sandbox_cards(self):
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		stages = frappe.get_all("CRM Deal Status", {"pipeline": self.sandbox}, pluck="name", order_by="position asc")
		card.reload()
		card.status = stages[1]
		card.save()
		self.assertEqual(frappe.db.get_value(DEAL, card.name, "status"), stages[1])

	def test_contact_and_lead_of_a_test_key_stay_in_the_sandbox(self):
		self.as_test_key()
		with self.assertRaises(sandbox.SandboxViolation):
			frappe.get_doc({"doctype": "Contact", "first_name": "T", "company_name": self.real_org.name}).insert()
		with self.assertRaises(sandbox.SandboxViolation):
			sandbox.validate_lead(frappe._dict(pipeline=self.real))
		sandbox.validate_lead(frappe._dict(pipeline=self.sandbox))

	# ---------- Reads ----------

	def test_test_key_never_sees_real_contacts_or_organizations(self):
		org = self.sandbox_org()
		mine = frappe.get_doc({"doctype": "Contact", "first_name": "Teste"}).insert()
		self.assertIn(org.name, frappe.get_list("CRM Organization", pluck="name"))
		self.assertNotIn(self.real_org.name, frappe.get_list("CRM Organization", pluck="name"))
		contacts = frappe.get_list("Contact", pluck="name")
		self.assertIn(mine.name, contacts)
		self.assertNotIn(self.real_contact.name, contacts)
		self.assertFalse(frappe.has_permission("Contact", "read", doc=self.real_contact.name))
		self.assertFalse(frappe.has_permission("CRM Organization", "write", doc=self.real_org.name))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Contact", self.real_contact.name).check_permission("read")

	def test_people_and_real_keys_are_unaffected(self):
		self.sandbox_org()
		frappe.set_user("Administrator")
		self.assertIn(self.real_org.name, frappe.get_list("CRM Organization", pluck="name"))
		self.assertEqual(sandbox.query_conditions("Administrator", "Contact"), "")
		real = self.create_key(label="Real")
		self.call(token=real["token"])
		self.assertEqual(sandbox.query_conditions(frappe.session.user, "Contact"), "")
		self.assertTrue(frappe.has_permission("Contact", "read", doc=self.real_contact.name))

	def test_files_and_digital_folder_of_real_records_are_hidden(self):
		real_file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "real.txt",
				"content": "x",
				"is_private": 1,
				"attached_to_doctype": "CRM Organization",
				"attached_to_name": self.real_org.name,
			}
		).insert(ignore_permissions=True)
		item = frappe.get_doc(
			{
				"doctype": "Cred Hub Checklist Item",
				"organization": self.real_org.name,
				"item_key": "contrato_social",
				"label": "Contrato Social",
			}
		).insert(ignore_permissions=True)
		self.as_test_key()
		self.assertNotIn(real_file.name, frappe.get_list("File", pluck="name"))
		self.assertFalse(frappe.has_permission("File", "read", doc=real_file.name))
		self.assertNotIn(item.name, frappe.get_list("Cred Hub Checklist Item", pluck="name"))
		self.assertFalse(frappe.has_permission("Cred Hub Checklist Item", "read", doc=item.name))
		with self.assertRaises(sandbox.SandboxViolation):
			frappe.get_doc(
				{"doctype": "Cred Hub Checklist Item", "organization": self.real_org.name, "item_key": "x", "label": "X"}
			).insert(ignore_permissions=True)

	def test_deleted_real_records_are_not_listed_to_a_test_key(self):
		from cred_hub.sync import get_deleted_records

		frappe.delete_doc("Contact", self.real_contact.name, ignore_permissions=True)
		self.as_test_key()
		mine = frappe.get_doc({"doctype": "Contact", "first_name": "Apagar"}).insert()
		frappe.delete_doc("Contact", mine.name, ignore_permissions=True)
		names = [row.deleted_name for row in get_deleted_records("Contact")]
		self.assertIn(mine.name, names)
		self.assertNotIn(self.real_contact.name, names)

	# ---------- Webhooks and paid checks ----------

	def webhook(self, test):
		return frappe.get_doc(
			{
				"doctype": webhooks.WEBHOOK_DOCTYPE,
				"label": "WH",
				"webhook_url": "https://hooks.example.com/hubtech",
				"secret": "segredo",
				"status": "Active",
				"is_test": int(test),
				**{field: 0 for field in webhooks.EVENT_FIELD_MAP.values()},
				"deal_created": 1,
				"contact_created": 1,
			}
		).insert(ignore_permissions=True)

	def outbox(self, webhook):
		return frappe.get_all(webhooks.OUTBOX_DOCTYPE, {"webhook": webhook}, pluck="event")

	def test_sandbox_events_only_reach_test_webhooks(self):
		test_hook, real_hook = self.webhook(True), self.webhook(False)
		frappe.get_doc({"doctype": DEAL, "pipeline": self.real}).insert()
		self.as_test_key()
		frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.get_doc({"doctype": "Contact", "first_name": "Teste"}).insert()
		frappe.set_user("Administrator")
		self.assertEqual(sorted(self.outbox(test_hook.name)), ["contact.created", "deal.created"])
		self.assertEqual(self.outbox(real_hook.name), ["deal.created"])

	def test_test_webhook_delivery_is_marked(self):
		hook = self.webhook(True)
		from cred_hub.email_company import safe_http

		session = MagicMock()
		session.post.return_value = MagicMock(status_code=200, text="ok")
		with patch.object(safe_http, "resolve_public"), patch.object(safe_http, "_session", return_value=session):
			webhooks.send_webhook(hook.name, "deal.created", {"name": "X"})
		self.assertEqual(session.post.call_args.kwargs["headers"].get(sandbox.TEST_HEADER), "1")

	def test_sandbox_card_never_triggers_the_paid_credit_check(self):
		from cred_hub.credit_check import events

		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		card.status = frappe.get_all("CRM Deal Status", {"pipeline": self.sandbox}, pluck="name", order_by="position asc")[1]
		settings = {"enabled": 1, "auto_trigger_stage": card.status}
		with (
			patch.object(events, "get_credit_settings", return_value=settings),
			patch.object(card, "has_value_changed", return_value=True),
			patch.object(frappe, "enqueue") as enqueue,
		):
			events.on_deal_update(card)
		enqueue.assert_not_called()

	def test_batch_of_a_test_key_stays_in_the_sandbox(self):
		from cred_hub.bulk import bulk_upsert

		self.call(path="/api/method/cred_hub.bulk.bulk_upsert", method="POST", form={"doctype": DEAL, "records": "[]"})
		result = bulk_upsert(DEAL, json.dumps([{"pipeline": self.sandbox}, {"pipeline": self.real}]))
		self.assertEqual([r["status"] for r in result["results"]], ["success", "error"])
