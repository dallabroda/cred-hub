"""Test keys and the Sandbox pipeline (credtech#375, ADR 0007 section 11): a test key never sees nor changes real
data, never runs paid queries, and its events only reach test webhooks.
Run: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_api_sandbox`."""

import json
import re
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
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox, "organization": self.real_org.name}).insert()
		with self.assertRaises(frappe.LinkValidationError):
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
		with self.assertRaises(frappe.LinkValidationError):
			frappe.get_doc({"doctype": "Contact", "first_name": "T", "company_name": self.real_org.name}).insert()
		with self.assertRaises(frappe.LinkValidationError):
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
		with self.assertRaises(frappe.LinkValidationError):
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
		"""Test webhooks: header `X-HubTech-Test: 1` and `"test": true` in the body; every other delivery says
		`"test": false` and has no header (credtech#389, decision 3-A)."""
		from cred_hub.email_company import safe_http

		for test in (True, False):
			hook = self.webhook(test)
			session = MagicMock()
			session.post.return_value = MagicMock(status_code=200, text="ok")
			with patch.object(safe_http, "resolve_public"), patch.object(safe_http, "_session", return_value=session):
				webhooks.send_webhook(hook.name, "deal.created", {"name": "X"})
			kwargs = session.post.call_args.kwargs
			self.assertEqual(kwargs["headers"].get(sandbox.TEST_HEADER), "1" if test else None)
			body = json.loads(kwargs["data"])
			self.assertIs(body["test"], test)
			# signed as sent: the flag is covered by the HMAC
			expected = webhooks.sign("segredo", kwargs["data"], int(kwargs["headers"]["X-HubTech-Timestamp"]))
			self.assertEqual(kwargs["headers"]["X-HubTech-Signature"], expected["X-HubTech-Signature"])

	def test_sandbox_event_reaches_the_test_webhook_with_the_flag_in_the_body(self):
		"""End to end: a test key creates a Sandbox card, the outbox delivers it to the test webhook as test."""
		from cred_hub.email_company import safe_http

		hook = self.webhook(True)
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		row = frappe.get_last_doc(webhooks.OUTBOX_DOCTYPE, filters={"webhook": hook.name, "event": "deal.created"})
		session = MagicMock()
		session.post.return_value = MagicMock(status_code=200, text="ok")
		with patch.object(safe_http, "resolve_public"), patch.object(safe_http, "_session", return_value=session):
			webhooks.deliver(row.name)
		body = json.loads(session.post.call_args.kwargs["data"])
		self.assertEqual((body["event"], body["data"]["name"], body["test"]), ("deal.created", card.name, True))

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

	# ---------- Review of #110: notes and tasks of real cards (1) ----------

	def real_card_with_note_and_task(self):
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.real}).insert()
		note = frappe.get_doc(
			{"doctype": "FCRM Note", "title": "Real", "reference_doctype": DEAL, "reference_docname": card.name}
		).insert(ignore_permissions=True)
		task = frappe.get_doc(
			{"doctype": "CRM Task", "title": "Real", "reference_doctype": DEAL, "reference_docname": card.name}
		).insert(ignore_permissions=True)
		return card, note, task

	def test_notes_and_tasks_of_real_cards_are_hidden(self):
		card, note, task = self.real_card_with_note_and_task()
		self.as_test_key()
		for doctype, doc in (("FCRM Note", note), ("CRM Task", task)):
			self.assertNotIn(doc.name, frappe.get_list(doctype, pluck="name"), doctype)
			self.assertFalse(frappe.has_permission(doctype, "read", doc=doc.name), doctype)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(doctype, doc.name).check_permission("read")
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": "FCRM Note", "title": "x", "reference_doctype": DEAL, "reference_docname": card.name}
			).insert()

	def test_notes_of_sandbox_cards_are_visible_even_when_written_by_people(self):
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		note = frappe.get_doc(
			{"doctype": "FCRM Note", "title": "Equipe", "reference_doctype": DEAL, "reference_docname": card.name}
		).insert()
		self.as_test_key()
		self.assertIn(note.name, frappe.get_list("FCRM Note", pluck="name"))
		mine = frappe.get_doc(
			{"doctype": "FCRM Note", "title": "API", "reference_doctype": DEAL, "reference_docname": card.name}
		).insert()
		self.assertEqual(frappe.db.get_value("FCRM Note", mine.name, sandbox.RECORD_FLAG), 1)

	# ---------- (2) no oracle on real names, documents, e-mails, phones ----------

	def test_test_key_organization_with_a_real_name_lives_in_its_own_name_space(self):
		self.as_test_key()
		org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": self.real_org.organization_name}).insert()
		self.assertEqual(org.name, self.real_org.name + sandbox.ORGANIZATION_SUFFIX)
		self.assertEqual(org.get(sandbox.RECORD_FLAG), 1)
		# renaming keeps it in the sandbox name space
		org.organization_name = "Outro nome"
		org.save()
		self.assertTrue(frappe.db.get_value("CRM Organization", org.name, "organization_name").endswith("[sandbox]"))

	def test_link_to_a_real_record_answers_like_a_missing_one(self):
		self.as_test_key()
		errors = []
		for organization in (self.real_org.name, "Nao Existe Ltda 123"):
			with self.assertRaises(frappe.LinkValidationError) as ctx:
				frappe.get_doc({"doctype": "Contact", "first_name": "T", "company_name": organization}).insert()
			errors.append(str(ctx.exception).replace(organization, "<org>"))
		self.assertEqual(errors[0], errors[1])

	def test_lead_conversion_never_matches_real_contacts(self):
		frappe.db.set_value("Contact", self.real_contact.name, "email_id", "real@cliente.com.br")
		frappe.get_doc({"doctype": "Contact Email", "parent": self.real_contact.name, "parenttype": "Contact",
			"parentfield": "email_ids", "email_id": "real@cliente.com.br"}).insert(ignore_permissions=True)
		self.as_test_key()
		lead = frappe.new_doc("CRM Lead")
		lead.update({"first_name": "Teste", "email": "real@cliente.com.br", "organization": self.real_org.name})
		self.assertFalse(lead.contact_exists(throw=True))
		mine = frappe.get_doc({"doctype": "Contact", "first_name": "Meu"}).insert()
		frappe.get_doc({"doctype": "Contact Email", "parent": mine.name, "parenttype": "Contact",
			"parentfield": "email_ids", "email_id": "teste@exemplo.com"}).insert(ignore_permissions=True)
		lead.email = "teste@exemplo.com"
		self.assertEqual(lead.contact_exists(throw=True), mine.name)
		lead.organization = self.real_org.name
		created = lead.create_organization()
		self.assertNotEqual(created, self.real_org.name)
		self.assertEqual(frappe.db.get_value("CRM Organization", created, sandbox.RECORD_FLAG), 1)

	def test_batch_does_not_reveal_real_records(self):
		from cred_hub.bulk import bulk_upsert

		self.call(path="/api/method/cred_hub.bulk.bulk_upsert", method="POST", form={"doctype": "CRM Organization", "records": "[]"})
		result = bulk_upsert(
			"CRM Organization", json.dumps([{"name": self.real_org.name, "organization_name": "Lote Teste"}])
		)
		self.assertEqual(result["results"][0].get("action"), "created", result)
		self.assertNotEqual(result["results"][0]["name"], self.real_org.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("CRM Organization", self.real_org.name, "organization_name"), self.real_org.organization_name)

	def test_nobody_sets_the_flag_by_hand(self):
		self.real_contact.set(sandbox.RECORD_FLAG, 1)
		self.real_contact.save()
		self.assertEqual(frappe.db.get_value("Contact", self.real_contact.name, sandbox.RECORD_FLAG), 0)

	# ---------- (3) Sandbox cards never leave ----------

	def test_sandbox_cards_never_move_to_a_real_pipeline(self):
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		card.reload()
		card.pipeline = self.real
		card.status = frappe.get_all("CRM Deal Status", {"pipeline": self.real}, pluck="name")[0]
		with self.assertRaises(sandbox.SandboxViolation):
			card.save()

	# ---------- (4) no paid or outbound side effects ----------

	def test_no_outbound_side_effects_for_sandbox_records(self):
		from cred_hub.cnpj import events as cnpj_events
		from cred_hub.collection import reminders
		from cred_hub.email_company import finder
		from cred_hub.integrations import google_calendar

		org = self.sandbox_org()
		org.cnpj = "11.222.333/0001-81"
		with patch.object(frappe, "enqueue") as enqueue, patch.object(finder, "enabled", return_value=True):
			cnpj_events.on_update(org)
			lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "T", "email": "a@empresa.com.br"}).insert()
			self.assertEqual(lead.get(sandbox.RECORD_FLAG), 1)
			finder.on_lead_insert(lead)
		enqueue.assert_not_called()
		frappe.set_user("Administrator")
		with patch.object(google_calendar, "get_valid_access_token") as token:
			google_calendar.sync_task_to_google_calendar(
				frappe._dict(doctype="CRM Task", name="T", assigned_to="Administrator", credhub_sandbox=1)
			)
		token.assert_not_called()
		with patch.object(reminders, "collection_settings", return_value={"collection_pipeline": self.sandbox}):
			self.assertEqual(reminders._open_rows(["2026-11-12"]), [])

	def test_sandbox_card_never_goes_to_the_erp(self):
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		with patch("cred_hub.erp.cedente.on_deal_update") as cedente:
			sandbox.erp_cedente_on_deal_update(card)
		cedente.assert_not_called()
		real_card = frappe.get_doc({"doctype": DEAL, "pipeline": self.real}).insert()
		with patch("cred_hub.erp.cedente.on_deal_update") as cedente:
			sandbox.erp_cedente_on_deal_update(real_card)
		cedente.assert_called_once()
		self.assertIn("cred_hub.sandbox.erp_cedente_on_deal_update", frappe.get_hooks("doc_events")[DEAL]["on_update"])
		self.assertNotIn("cred_hub.erp.cedente.on_deal_update", frappe.get_hooks("doc_events")[DEAL]["on_update"])

	# ---------- (5) performance ----------

	def test_test_users_are_read_once_per_request(self):
		sandbox.clear_test_users_cache()
		with patch.object(frappe.cache, "get_value", wraps=frappe.cache.get_value) as get_value:
			for _i in range(20):
				sandbox.is_test_user("Administrator")
		self.assertEqual(len([c for c in get_value.call_args_list if c.args[0] == sandbox.TEST_USERS_CACHE]), 1)

	def test_lists_filter_on_the_indexed_flag(self):
		self.as_test_key()
		self.assertIn("`credhub_sandbox` = 1", sandbox.query_conditions(frappe.session.user, "Contact"))
		self.assertNotIn("owner", sandbox.query_conditions(frappe.session.user, "Contact"))
		indexes = frappe.db.sql("show index from `tabContact` where Column_name = 'credhub_sandbox'")
		self.assertTrue(indexes)

	# ---------- (6) matrix: a test key against real data on every route ----------

	def test_matrix_test_key_never_reaches_real_records(self):
		card, note, task = self.real_card_with_note_and_task()
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Real"}).insert(ignore_permissions=True)
		real_file = frappe.get_doc(
			{"doctype": "File", "file_name": "r.txt", "content": "x", "is_private": 1,
			 "attached_to_doctype": DEAL, "attached_to_name": card.name}
		).insert(ignore_permissions=True)
		item = frappe.get_doc(
			{"doctype": "Cred Hub Checklist Item", "organization": self.real_org.name, "item_key": "k", "label": "K"}
		).insert(ignore_permissions=True)
		visit = frappe.get_doc({"doctype": "Cred Hub Visit Report", "organization": self.real_org.name}).insert(
			ignore_permissions=True
		)
		report = frappe.get_doc(
			{"doctype": "Cred Hub Credit Report", "organization": self.real_org.name, "verdict": "Warning"}
		).insert(ignore_permissions=True)
		real = {
			"CRM Organization": self.real_org.name,
			"Contact": self.real_contact.name,
			"CRM Lead": lead.name,
			DEAL: card.name,
			"FCRM Note": note.name,
			"CRM Task": task.name,
			"File": real_file.name,
			"Cred Hub Checklist Item": item.name,
			"Cred Hub Visit Report": visit.name,
			"Cred Hub Credit Report": report.name,
			"CRM Pipeline": self.real,
			"CRM Deal Status": frappe.get_all("CRM Deal Status", {"pipeline": self.real}, pluck="name")[0],
		}
		self.assertTrue(set(api_keys.RESOURCES) <= set(real), set(api_keys.RESOURCES) - set(real))
		self.as_test_key()
		for doctype, name in real.items():
			self.assertNotIn(name, frappe.get_list(doctype, pluck="name", limit_page_length=0), doctype)
			self.assertFalse(frappe.has_permission(doctype, "read", doc=name), doctype)
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				frappe.get_doc(doctype, name).check_permission("read")
			if doctype not in ("CRM Pipeline", "CRM Deal Status", "Cred Hub Credit Report"):
				self.assertFalse(frappe.has_permission(doctype, "write", doc=name), doctype)
		# routes that resolve records themselves
		from cred_hub.api_keys import lookup_by_document
		from cred_hub.credit_check import api as credit_api
		from cred_hub.timeline import get_comments

		frappe.db.set_value("CRM Organization", self.real_org.name, "cnpj", "11.222.333/0001-81")
		found = lookup_by_document("11222333000181")
		self.assertIsNone(found["organization"])
		self.assertEqual(found["cards"], [])
		for call in (
			lambda: credit_api.get_document_checklist(organization=self.real_org.name),
			lambda: credit_api.get_credit_onboarding(organization=self.real_org.name),
			lambda: credit_api.get_credit_committee(organization=self.real_org.name),
			lambda: credit_api.get_report_details(report.name),
			lambda: credit_api.save_visit_report(organization=self.real_org.name, visit_data="{}"),
			lambda: credit_api.update_checklist_item(organization=self.real_org.name, item_key="k", status="verified"),
			lambda: get_comments(DEAL, card.name),
			lambda: get_comments("CRM Organization", self.real_org.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_deleted_real_organization_opens_no_hole(self):
		item = frappe.get_doc(
			{"doctype": "Cred Hub Checklist Item", "organization": self.real_org.name, "item_key": "k", "label": "K"}
		).insert(ignore_permissions=True)
		frappe.db.delete("CRM Organization", {"name": self.real_org.name})  # dangling link, no cascade
		self.as_test_key()
		self.assertNotIn(item.name, frappe.get_list("Cred Hub Checklist Item", pluck="name"))
		self.assertFalse(frappe.has_permission("Cred Hub Checklist Item", "read", doc=item.name))
		from cred_hub.sync import get_deleted_records

		self.call(path="/api/method/cred_hub.sync.get_deleted_records")
		for doctype in ("Contact", "CRM Organization", "File", DEAL):
			names = [row.deleted_name for row in get_deleted_records(doctype)]
			self.assertNotIn(self.real_org.name, names)

	# ---------- The Sandbox pipeline stays ----------

	def test_nobody_deletes_or_renames_the_sandbox_pipeline_or_deletes_its_stages(self):
		from crm.api.pipeline import get_pipeline_config, save_pipeline

		frappe.set_user("Administrator")
		stages = get_pipeline_config(self.sandbox)["stages"]
		kept = [{"name": s["name"], "deal_status": s["deal_status"]} for s in stages[:-1]]
		for attempt in (
			lambda: save_pipeline({"name": self.sandbox, "pipeline_name": "Outro nome"}),
			lambda: save_pipeline({"name": self.sandbox, "stages": kept}),
			lambda: frappe.delete_doc("CRM Deal Status", stages[0]["name"], ignore_permissions=True, force=True),
			lambda: frappe.delete_doc("CRM Pipeline", self.sandbox, ignore_permissions=True, force=True),
			lambda: frappe.rename_doc("CRM Pipeline", self.sandbox, "sandbox-renamed", force=True),
		):
			frappe.db.savepoint("sandbox_attempt")
			with self.assertRaisesRegex(frappe.ValidationError, re.escape(sandbox.PIPELINE_NAME) + ".*still in use"):
				attempt()
			frappe.db.rollback(save_point="sandbox_attempt")
		self.assertTrue(frappe.db.exists("CRM Pipeline", self.sandbox))
		self.assertEqual(len(get_pipeline_config(self.sandbox)["stages"]), len(stages))
		self.assertEqual(frappe.db.get_value("CRM Pipeline", self.sandbox, "pipeline_name"), sandbox.PIPELINE_NAME)
		# other changes to the Sandbox and every change to real pipelines still work
		save_pipeline({"name": self.sandbox, "color": "blue"})
		self.assertEqual(frappe.db.get_value("CRM Pipeline", self.sandbox, sandbox.PIPELINE_FLAG), 1)
		save_pipeline({"name": self.real, "pipeline_name": "Real renomeado"})
		frappe.delete_doc("CRM Pipeline", self.real, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists("CRM Pipeline", self.real))

	def test_sandbox_data_gets_no_rejection_email_and_real_lookups_never_reuse_it(self):
		from cred_hub.collection import importer
		from cred_hub.credit_check import onboarding
		from cred_hub.party import resolve

		org = self.sandbox_org()
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		for name, mailed in ((org.name, False), (self.real_org.name, True)):
			with (
				patch.object(onboarding, "generate_portal_link", return_value={"portal": {"url": "https://x.example"}}),
				patch.object(onboarding, "_resolve_rejection_recipient_email", return_value="pessoa@example.com"),
				patch.object(frappe, "sendmail") as sendmail,
			):
				onboarding._notify_document_rejected(name, None, "contrato_social", "Contrato", "ilegível")
			self.assertEqual(sendmail.called, mailed, name)
		# an ERP/import debtor with the CNPJ of a test organization never lands on it
		frappe.db.set_value("CRM Organization", org.name, "cnpj", "11.222.333/0001-81")
		self.assertIsNone(importer._existing_org("11222333000181"))
		self.assertIsNone(importer._existing_debtor("11222333000181", "cnpj"))
		self.assertNotEqual(resolve.organization_for_cnpj("11222333000181", "Devedora"), org.name)
		# and a test key only finds its own
		self.as_test_key()
		self.assertEqual(sandbox.same_space("CRM Organization"), {sandbox.RECORD_FLAG: 1})

	def test_sandbox_pipeline_can_go_once_no_test_key_nor_test_data_remains(self):
		from crm.api.pipeline import save_pipeline

		org = self.sandbox_org()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		uses = ", ".join(sandbox.sandbox_in_use(self.sandbox))
		for expected in ("1 test API key(s) not revoked", "1 card(s)", "1 CRM Organization"):
			self.assertIn(expected, uses)
		api_keys.revoke_api_key(self.created["name"])
		frappe.delete_doc(DEAL, card.name, ignore_permissions=True, force=True)
		# revoked and no card, but a test organization is still there: the message says what remains
		frappe.db.savepoint("sandbox_attempt")
		with self.assertRaisesRegex(frappe.ValidationError, "still in use.*1 CRM Organization"):
			frappe.delete_doc("CRM Pipeline", self.sandbox, ignore_permissions=True)
		frappe.db.rollback(save_point="sandbox_attempt")
		frappe.delete_doc("CRM Organization", org.name, ignore_permissions=True, force=True)
		self.assertEqual(sandbox.sandbox_in_use(self.sandbox), [])
		# free: the admin renames and deletes it (no force: the revoked key's link goes with it, the key stays)
		save_pipeline({"name": self.sandbox, "pipeline_name": "Sandbox antigo"})
		frappe.delete_doc("CRM Pipeline", self.sandbox, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("CRM Pipeline", self.sandbox))
		self.assertFalse(frappe.db.exists("Cred Hub API Key Pipeline", {"pipeline": self.sandbox}))
		self.assertTrue(frappe.db.exists(api_keys.KEY, self.created["name"]))
		# the next test key gets a new Sandbox
		sandbox.clear_test_users_cache()
		new = self.create_key(test=True, label="Nova")
		self.assertNotEqual(new["pipelines"], [self.sandbox])
		self.assertEqual(frappe.db.get_value("CRM Pipeline", new["pipelines"][0], sandbox.PIPELINE_FLAG), 1)

	# ---------- A test key never assigns nor mentions a person ----------

	def test_matrix_test_key_never_assigns_or_mentions_people(self):
		from frappe.desk.form.utils import add_comment

		person = "Administrator"
		mention = (
			f'<p><span class="mention" data-type="mention" data-id="{person}" data-label="Admin">@Admin</span> veja</p>'
		)
		# every writable resource with an owner field is guarded
		for doctype, (_read, write) in api_keys.RESOURCES.items():
			if not write:
				continue
			fields = {"deal_owner", "lead_owner", "assigned_to"} & {df.fieldname for df in frappe.get_meta(doctype).fields}
			self.assertTrue(fields <= set(sandbox.OWNER_FIELDS.get(doctype, ())), (doctype, fields))
		self.as_test_key()
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		for doc in (
			{"doctype": DEAL, "pipeline": self.sandbox, "deal_owner": person},
			{"doctype": "CRM Lead", "first_name": "Teste", "pipeline": self.sandbox, "lead_owner": person},
			{"doctype": "CRM Task", "title": "t", "reference_doctype": DEAL, "reference_docname": card.name, "assigned_to": person},
		):
			frappe.db.savepoint("assign_attempt")
			with self.assertRaisesRegex(frappe.ValidationError, "cannot assign records to people"):
				frappe.get_doc(doc).insert()
			frappe.db.rollback(save_point="assign_attempt")
		card.reload()
		card._assign = json.dumps([person])
		with self.assertRaisesRegex(frappe.ValidationError, "_assign"):
			card.save()
		# an owner set by the team stays, and the key can keep working on the card
		frappe.set_user("Administrator")
		frappe.db.set_value(DEAL, card.name, "deal_owner", person)
		self.as_test_key()
		card = frappe.get_doc(DEAL, card.name)
		card.website = "https://teste.example"
		card.save()
		card.deal_owner = None
		card.save()
		# @mentions written by a test key become plain text (no Frappe or crm notification, no e-mail)
		from crm.api.comment import extract_mentions as crm_mentions
		from frappe.desk.notifications import extract_mentions

		comment = add_comment(DEAL, card.name, mention, frappe.session.user, "API")
		note = frappe.get_doc(
			{"doctype": "FCRM Note", "title": "n", "content": mention, "reference_doctype": DEAL, "reference_docname": card.name}
		).insert()
		task = frappe.get_doc(
			{"doctype": "CRM Task", "title": "t", "description": mention, "reference_doctype": DEAL, "reference_docname": card.name}
		).insert()
		for doctype, name, field in (("Comment", comment.name, "content"), ("FCRM Note", note.name, "content"), ("CRM Task", task.name, "description")):
			stored = frappe.db.get_value(doctype, name, field)
			self.assertIn("@Admin", stored, doctype)
			self.assertEqual(extract_mentions(stored), [], doctype)
			self.assertEqual(crm_mentions(stored), [], doctype)
		# people's mentions are untouched
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		self.assertEqual(sandbox.plain_mentions("<p>sem menção</p>"), "<p>sem menção</p>")
		staff = frappe.get_doc(
			{"doctype": "FCRM Note", "title": "n", "content": mention, "reference_doctype": DEAL, "reference_docname": card.name}
		).insert()
		self.assertIn('class="mention"', frappe.db.get_value("FCRM Note", staff.name, "content"))

	def test_technical_user_never_gets_mail_nor_notifications(self):
		from frappe.desk.form.assign_to import add as assign_to

		queued = frappe.db.count("Email Queue")
		tech = self.as_test_key()
		self.assertTrue(tech.endswith(".invalid"))
		card = frappe.get_doc({"doctype": DEAL, "pipeline": self.sandbox}).insert()
		# a test key assigning a task to itself (allowed) queues no e-mail
		task = frappe.get_doc(
			{"doctype": "CRM Task", "title": "t", "reference_doctype": DEAL, "reference_docname": card.name, "assigned_to": tech}
		).insert()
		self.assertTrue(frappe.db.exists("ToDo", {"reference_name": task.name, "allocated_to": tech}))
		# nor does a person assigning a record to it, or any e-mail addressed to it
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		other = frappe.get_doc({"doctype": "CRM Task", "title": "t2"}).insert(ignore_permissions=True)
		assign_to({"assign_to": [tech], "doctype": "CRM Task", "name": other.name})
		frappe.sendmail(recipients=[tech], subject="x", message="x", now=False)
		self.assertEqual(frappe.db.count("Email Queue"), queued)
		self.assertFalse(frappe.db.exists("Email Queue Recipient", {"recipient": tech}))
		self.assertFalse(frappe.db.exists("Notification Log", {"for_user": tech}))
		# the preferences that would send mail are off, and muting again changes nothing
		self.assertTrue(frappe.db.exists("Email Unsubscribe", {"email": tech, "global_unsubscribe": 1}))
		self.assertEqual(frappe.db.get_value("Notification Settings", tech, "enabled"), 0)
		for field in api_keys.QUIET_USER_FIELDS:
			self.assertEqual(frappe.db.get_value("User", tech, field), 0, field)
		api_keys.mute_technical_user(tech)
		self.assertEqual(frappe.db.count("Email Unsubscribe", {"email": tech}), 1)
