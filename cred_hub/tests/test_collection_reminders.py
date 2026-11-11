"""Régua de cobrança (credtech#367): which títulos get today's step, once, and what the SmartChat receives. The
SmartChat is faked. Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_collection_reminders`."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from cred_hub import welcome
from cred_hub.collection import reminders

TEMPLATE = {
	"name": "cobranca_d5",
	"language": "pt_BR",
	"category": "UTILITY",
	"body": "Olá {{1}}, o título {{2}} venceu em {{3}}. Saldo: {{4}}.",
	"variables": ["1", "2", "3", "4"],
}
INBOX = {"id": 7, "name": "Cobrança", "phone": "+5511999990000", "templates": [TEMPLATE]}


class FakeChat:
	def __init__(self, fail=False):
		self.fail, self.calls = fail, []

	def call(self, method, path, token, **kwargs):
		if self.fail:
			raise welcome.ChatError("Cred Chat refused the request: 131026")
		self.calls.append((method, path, kwargs.get("json")))
		return {"id": 900 + len(self.calls)}


class TestCollectionReminders(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()
		self.addCleanup(self.commit.stop)
		self.pipeline = frappe.get_doc({"doctype": "CRM Pipeline", "pipeline_name": f"Cobrança régua {frappe.generate_hash(length=6)}"}).insert()
		settings = frappe.get_single("Cred Hub Provider Settings")
		frappe.db.set_single_value("Cred Hub Provider Settings", "collection_pipeline", self.pipeline.name)
		self.addCleanup(frappe.db.set_single_value, "Cred Hub Provider Settings", "collection_pipeline", settings.collection_pipeline)
		frappe.clear_cache(doctype="Cred Hub Provider Settings")
		self.sacado = frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"Sacado Régua {frappe.generate_hash(length=6)}"}).insert()
		self.contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": "Maria",
				"last_name": "Pagadora",
				"company_name": self.sacado.name,
				"phone_nos": [{"phone": "+55 11 98765-4321", "is_primary_mobile_no": 1}],
			}
		).insert()
		frappe.db.set_value("Contact", self.contact.name, "mobile_no", "+5511987654321")
		self.connection = patch.object(welcome, "_connection", return_value=(frappe._dict(account_id=3), "token"))
		self.connection.start()
		self.addCleanup(self.connection.stop)
		self.inboxes = patch.object(welcome, "_inboxes", return_value=[INBOX])
		self.inboxes.start()
		self.addCleanup(self.inboxes.stop)
		self.contact_api = patch.object(welcome, "_contact", return_value=55)
		self.contact_api.start()
		self.addCleanup(self.contact_api.stop)
		doc = frappe.get_single(reminders.SETTINGS)
		doc.enabled, doc.inbox_id = 1, 7
		doc.set("steps", [])
		doc.append("steps", {"days": 5, "template_name": "cobranca_d5", "variables": json.dumps(["sacado", "title_number", "due_date", "balance"])})
		doc.save()

	def _deal(self, **title):
		row = {"title_number": "NF-1", "due_date": add_days(today(), -5), "original_amount": 1000, "sacado": self.sacado.name}
		row.update(title)
		return frappe.get_doc(
			{"doctype": "CRM Deal", "pipeline": self.pipeline.name, "organization": self.sacado.name, "titles": [row]}
		).insert(ignore_permissions=True)

	def _run(self, chat=None):
		chat = chat or FakeChat()
		with patch.object(welcome, "_call", side_effect=chat.call):
			stats = reminders.run()
		return chat, stats

	def test_sends_the_step_of_the_day_once_with_the_titulo_values(self):
		deal = self._deal()
		chat, stats = self._run()
		self.assertEqual(stats["Sent"], 1)
		_, path, body = chat.calls[0]
		self.assertEqual(path, "/api/v1/accounts/3/conversations")
		self.assertEqual(body["inbox_id"], 7)
		self.assertNotIn("assignee_id", body)  # the SmartChat AI agent takes unassigned conversations
		params = body["message"]["template_params"]["processed_params"]["body"]
		self.assertEqual(params["1"], self.sacado.organization_name)
		self.assertEqual(params["2"], "NF-1")
		self.assertEqual(params["3"], getdate(add_days(today(), -5)).strftime("%d/%m/%Y"))
		self.assertIn("1.000,00", params["4"])
		log = frappe.get_doc(reminders.LOG, f"{deal.titles[0].name}:5")
		self.assertEqual((log.status, log.phone, log.conversation), ("Sent", "+5511987654321", "901"))
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": deal.name, "content": ("like", "%D+5%")}))
		# The same step never goes twice
		chat, stats = self._run()
		self.assertEqual(chat.calls, [])

	def test_never_charges_protested_paid_promised_or_closed_cards(self):
		self._deal(sent_at=frappe.utils.now(), protest_status="COLETADO")
		self._deal(updated_amount=0, paid_amount=1000)
		self._deal(promise_date=add_days(today(), 3), promise_amount=1000)
		lost = self._deal()
		frappe.db.set_value("CRM Deal", lost.name, "outcome", "Lost")
		chat, _ = self._run()
		self.assertEqual(chat.calls, [])

	def test_import_text_in_protest_status_is_not_a_protest(self):
		self._deal(protest_status="Pendente")
		chat, _ = self._run()
		self.assertEqual(len(chat.calls), 1)

	def test_other_days_are_not_sent(self):
		self._deal(due_date=add_days(today(), -4))
		self._deal(due_date=add_days(today(), -200))
		chat, _ = self._run()
		self.assertEqual(chat.calls, [])

	def test_no_mobile_is_recorded_and_not_sent(self):
		frappe.db.set_value("Contact", self.contact.name, {"mobile_no": None, "phone": "+551133334444"})
		deal = self._deal()
		chat, _ = self._run()
		self.assertEqual(chat.calls, [])
		self.assertEqual(frappe.db.get_value(reminders.LOG, f"{deal.titles[0].name}:5", "status"), "Not sent")

	def test_a_smartchat_refusal_is_recorded_as_failed(self):
		deal = self._deal()
		_, stats = self._run(FakeChat(fail=True))
		self.assertEqual(stats["Failed"], 1)
		log = frappe.get_doc(reminders.LOG, f"{deal.titles[0].name}:5")
		self.assertEqual(log.status, "Failed")
		self.assertIn("131026", log.error)

	def test_turned_off_sends_nothing(self):
		frappe.db.set_single_value(reminders.SETTINGS, "enabled", 0)
		self._deal()
		chat, stats = self._run()
		self.assertEqual((chat.calls, stats), ([], {}))

	def test_settings_need_a_variable_for_each_template_variable(self):
		doc = frappe.get_single(reminders.SETTINGS)
		doc.steps[0].variables = json.dumps(["sacado"])
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_settings_refuse_two_steps_on_the_same_day(self):
		doc = frappe.get_single(reminders.SETTINGS)
		doc.append("steps", {"days": 5, "template_name": "cobranca_d5", "variables": "[]"})
		self.assertRaises(frappe.ValidationError, doc.save)

	def test_estimate_counts_the_next_30_days(self):
		self._deal(due_date=add_days(today(), -5))  # D+5 today
		self._deal(due_date=add_days(today(), 10))  # D+5 in 15 days
		self._deal(due_date=add_days(today(), 40))  # D+5 in 45 days: out
		self.assertEqual(reminders.estimate([5]), 2)


class TestReminderRules(IntegrationTestCase):
	def test_step_days(self):
		self.assertEqual(reminders.step_days("2026-11-10", "2026-11-08"), -2)
		self.assertEqual(reminders.step_days("2026-11-10", "2026-11-15"), 5)

	def test_mobile_only(self):
		self.assertTrue(reminders.is_mobile("+5511987654321"))
		self.assertFalse(reminders.is_mobile("+551133334444"))
