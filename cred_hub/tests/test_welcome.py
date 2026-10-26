"""Boas-vindas pelo WhatsApp para o lead novo (credtech#53). A API do Cred Chat é simulada; nada sai do servidor.
Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import lead_intake, welcome

SELLER = "crm.user1@example.com"
TEMPLATE = {
	"name": "boas_vindas",
	"language": "pt_BR",
	"status": "APPROVED",
	"category": "MARKETING",
	"components": [{"type": "BODY", "text": "Olá {{1}}, recebemos seu interesse em {{2}}!"}],
}
INBOXES = {
	"payload": [
		{
			"id": 7,
			"name": "Comercial",
			"channel_type": "Channel::Whatsapp",
			"phone_number": "+5548999990000",
			"message_templates": [
				TEMPLATE,
				{**TEMPLATE, "name": "pendente", "status": "PENDING"},
				{**TEMPLATE, "name": "com_imagem", "components": [{"type": "HEADER", "format": "IMAGE"}, *TEMPLATE["components"]]},
			],
		},
		{"id": 8, "name": "Site", "channel_type": "Channel::WebWidget"},
	]
}


class FakeChat:
	"""API do Cred Chat: responde por caminho e guarda o que foi enviado."""

	def __init__(self, contacts=None):
		self.contacts = contacts or []
		self.sent = []

	def __call__(self, method, path, token, **kwargs):
		if token != "chave-boa":
			raise welcome.ChatError("A chave de acesso do Cred Chat não é válida.")
		if path == "/api/v1/profile":
			return {"accounts": [{"id": 3, "name": "Acme"}]}
		if path.endswith("/inboxes"):
			return INBOXES
		if path.endswith("/contacts/search"):
			return {"payload": self.contacts}
		if path.endswith("/contacts"):
			return {"payload": {"contact": {"id": 55}}}
		if path.endswith("/conversations"):
			self.sent.append(kwargs["json"])
			return {"id": 901}
		raise AssertionError(path)


class TestWelcome(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		self.chat = FakeChat()
		call = patch.object(welcome, "_call", self.chat)
		call.start()
		self.addCleanup(call.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{
				"pipeline_name": "Boas-vindas",
				"stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}],
				"members": [{"user": SELLER, "role": "Member"}],
			}
		)["name"]
		created = lead_intake.create_webhook_source("webhook", "Site Acme", self.pipeline)
		self.source = frappe.get_doc(lead_intake.SOURCE, created["name"])
		self.key = created["key"]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def connect_and_enable(self):
		welcome.connect_chat(3, "chave-boa")
		return welcome.save_welcome(
			self.source.name, enabled=1, inbox_id=7, template_name="boas_vindas", variables=json.dumps(["first_name", "ad_campaign_name"])
		)

	def post(self, body):
		request = MagicMock(headers={"X-CredHub-Key": self.key})
		request.get_data.return_value = json.dumps(body).encode()
		with patch.object(frappe, "request", request), patch.object(frappe, "enqueue") as enqueue:
			frappe.local.request_ip = "203.0.113.9"
			lead_intake.receive(self.source.name)
		# Só os envios de boas-vindas (o Cred Hub enfileira outras coisas ao criar o card)
		return [c for c in enqueue.call_args_list if c.args and c.args[0] == "cred_hub.welcome.send"]

	def event(self):
		return frappe.get_last_doc(welcome.EVENT, {"source": self.source.name})

	def test_connect_checks_key_and_lists_only_approved_text_templates(self):
		with self.assertRaises(frappe.ValidationError):
			welcome.connect_chat(3, "chave-errada")
		with self.assertRaises(frappe.ValidationError):
			welcome.connect_chat(4, "chave-boa")  # conta que a chave não alcança
		connection = welcome.connect_chat(3, "chave-boa")
		self.assertEqual((connection["connected"], connection["account_name"]), (True, "Acme"))
		self.assertEqual([i["id"] for i in connection["inboxes"]], [7])  # só WhatsApp
		templates = connection["inboxes"][0]["templates"]
		self.assertEqual([t["name"] for t in templates], ["boas_vindas"])
		self.assertEqual(templates[0]["variables"], ["1", "2"])
		self.assertNotIn("access_token", connection)
		stored = frappe.db.get_single_value(welcome.CONNECTION, "access_token")
		self.assertNotEqual(stored, "chave-boa")  # criptografada

	def test_enable_requires_a_field_for_each_variable(self):
		welcome.connect_chat(3, "chave-boa")
		with self.assertRaises(frappe.ValidationError):
			welcome.save_welcome(self.source.name, enabled=1, inbox_id=7, template_name="boas_vindas", variables='["first_name"]')
		with self.assertRaises(frappe.ValidationError):
			welcome.save_welcome(self.source.name, enabled=1, inbox_id=7, template_name="pendente", variables="[]")
		saved = self.connect_and_enable()
		self.assertTrue(saved["enabled"])
		self.assertEqual(saved["template_language"], "pt_BR")
		self.assertIn("first_name", [f["fieldname"] for f in saved["lead_fields"]])

	def test_new_lead_gets_the_template_after_commit(self):
		self.connect_and_enable()
		sends = self.post({"fields": {"first_name": "Rui", "mobile_no": "(48) 99876-5432"}, "ad_campaign_name": "x"})
		event = self.event()
		self.assertEqual((event.status, event.welcome), ("Created", "Queued"))
		self.assertTrue(sends[0].kwargs["enqueue_after_commit"])
		frappe.db.set_value("CRM Lead", event.lead, "ad_campaign_name", "Home Equity")
		welcome.send(event.name)
		event.reload()
		self.assertEqual((event.welcome, event.welcome_conversation), ("Sent", "901"))
		message = self.chat.sent[0]["message"]
		self.assertEqual(self.chat.sent[0]["inbox_id"], 7)
		self.assertEqual(message["template_params"]["processed_params"], {"body": {"1": "Rui", "2": "Home Equity"}})
		self.assertEqual(message["content"], "Olá Rui, recebemos seu interesse em Home Equity!")
		welcome.send(event.name)  # a fila rodou de novo: não manda em dobro
		self.assertEqual(len(self.chat.sent), 1)
		activity = frappe.call("cred_hub.meta_leads.get_lead_source_activity", source=self.source.name)
		self.assertEqual(activity[0]["welcome"], "Sent")
		self.assertTrue(activity[0]["welcome_url"].endswith("/app/accounts/3/conversations/901"))

	def test_failure_is_visible_and_not_retried(self):
		self.connect_and_enable()
		self.post({"fields": {"first_name": "Sem Celular"}})
		event = self.event()
		welcome.send(event.name)
		event.reload()
		self.assertEqual(event.welcome, "Failed")
		self.assertTrue(event.welcome_error)
		self.assertFalse(self.chat.sent)

	def test_disabled_or_duplicate_never_sends(self):
		self.assertFalse(self.post({"fields": {"first_name": "Ana", "mobile_no": "48999990001"}}))  # desligada (padrão)
		self.connect_and_enable()
		sends = self.post({"fields": {"first_name": "Ana", "mobile_no": "48999990001"}})
		self.assertEqual(self.event().status, "Duplicate")
		self.assertFalse(sends)

	def test_disconnect_turns_every_welcome_off(self):
		self.connect_and_enable()
		result = welcome.disconnect_chat()
		self.assertFalse(result["connected"])
		self.assertFalse(frappe.db.get_value(welcome.WELCOME, self.source.name, "enabled"))

	def test_salesperson_cannot_configure(self):
		frappe.set_user(SELLER)
		with self.assertRaises(frappe.PermissionError):
			welcome.get_chat_connection()
