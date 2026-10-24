"""Testes de integração (rodam no bench: `bench --site <site> run-tests --app cred_hub`)."""

import copy
import json

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.chat import webhook
from cred_hub.chat.leads import handle_conversation
from cred_hub.chat.payload import parse_conversation
from cred_hub.tests.test_chat_payload import PAYLOAD

CHAT = "https://chat.credtechdigital.com.br"


def _conv(conversation_id=None, **sender_changes):
	payload = copy.deepcopy(PAYLOAD)
	if conversation_id:
		payload["id"] = conversation_id
	payload["meta"]["sender"].update(sender_changes)
	return parse_conversation(payload)


class TestChatLeads(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def test_creates_lead_with_link_and_source(self):
		result = handle_conversation(_conv(), CHAT)
		self.assertEqual(result["status"], "created")
		lead = frappe.get_doc("CRM Lead", result["name"])
		self.assertEqual((lead.first_name, lead.last_name), ("Maria", "da Silva"))
		self.assertEqual(lead.mobile_no, "+5548999887766")
		self.assertEqual(lead.source, "WhatsApp")
		self.assertEqual(lead.chat_conversation_url, f"{CHAT}/app/accounts/1/conversations/42")

	def test_same_phone_does_not_duplicate(self):
		first = handle_conversation(_conv(), CHAT)
		# Mesmo número sem o 9º dígito e outra conversa
		second = handle_conversation(_conv(conversation_id=43, phone_number="+554899887766"), CHAT)
		self.assertEqual(second, {"status": "updated", "doctype": "CRM Lead", "name": first["name"]})
		self.assertTrue(frappe.db.get_value("CRM Lead", first["name"], "chat_conversation_url").endswith("/43"))
		self.assertEqual(frappe.db.count("CRM Lead", {"mobile_no": ["like", "%887766"]}), 1)

	def test_existing_lead_typed_by_hand(self):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Maria", "mobile_no": "(48) 99988-7766"}).insert()
		result = handle_conversation(_conv(), CHAT)
		self.assertEqual(result["name"], lead.name)

	def test_contact_with_deal_gets_the_link(self):
		contact = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Maria", "phone_nos": [{"phone": "48 99988-7766", "is_primary_mobile_no": 1}]}
		).insert()
		deal = frappe.get_doc({"doctype": "CRM Deal", "contacts": [{"contact": contact.name, "is_primary": 1}]}).insert()
		result = handle_conversation(_conv(), CHAT)
		self.assertEqual(result, {"status": "updated", "doctype": "CRM Deal", "name": deal.name})

	def test_other_account_and_no_contact_info_are_ignored(self):
		self.assertEqual(handle_conversation(_conv(), CHAT, account_id=2)["status"], "ignored")
		self.assertEqual(handle_conversation(_conv(phone_number="", email=""), CHAT)["status"], "ignored")

	def test_webhook_checks_token(self):
		settings = frappe.get_single("Cred Hub Settings")
		self.assertTrue(settings.webhook_token)
		self.assertIn(settings.webhook_token, settings.webhook_url)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.AuthenticationError):
			webhook.conversation_created(token="wrong", **json.loads(json.dumps(PAYLOAD)))
		result = webhook.conversation_created(token=settings.webhook_token, **copy.deepcopy(PAYLOAD))
		self.assertEqual(result["status"], "created")
