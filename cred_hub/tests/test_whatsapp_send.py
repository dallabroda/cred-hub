"""WhatsApp do card pelo SmartChat, com escolha da caixa (credtech#211). A API do SmartChat é simulada; nada sai do
servidor. Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import welcome, whatsapp_send

PHONE = "+5548988887777"
TEMPLATE = {
	"name": "retomar",
	"language": "pt_BR",
	"status": "APPROVED",
	"category": "UTILITY",
	"components": [{"type": "BODY", "text": "Olá {{1}}, podemos continuar?"}],
}
INBOXES = {
	"payload": [
		{"id": 7, "name": "Comercial", "channel_type": "Channel::Whatsapp", "phone_number": "+5548999990000", "message_templates": [TEMPLATE]},
		{"id": 9, "name": "Cobrança", "channel_type": "Channel::Whatsapp", "phone_number": "+5548999991111", "message_templates": []},
		{"id": 12, "name": "QR Vendas", "channel_type": "Channel::Api", "additional_attributes": {"connection_type": "evolution_qr"}},
		{"id": 8, "name": "Site", "channel_type": "Channel::WebWidget"},
	]
}


class FakeChat:
	"""API do SmartChat: responde por caminho e guarda o que foi enviado."""

	def __init__(self, role="agent", conversations=None):
		self.role = role
		self.conversations = conversations or []
		self.calls = []

	def __call__(self, method, path, token, **kwargs):
		self.calls.append((method, path, kwargs.get("json")))
		if path.endswith("/inboxes"):
			return INBOXES
		if path.endswith("/agents"):
			return [{"id": 41, "email": "Administrator@example.com", "role": self.role}, {"id": 42, "email": "outro@example.com"}]
		if "/inbox_members/" in path:
			# o vendedor é membro só da caixa Comercial (7) e da QR Vendas (12)
			return {"payload": [{"id": 41}] if path.endswith(("/7", "/12")) else [{"id": 42}]}
		if path.endswith("/contacts/search"):
			return {"payload": [{"id": 55, "phone_number": PHONE}]}
		if path.endswith("/contacts/55/conversations"):
			return {"payload": self.conversations}
		if path.endswith("/contacts"):
			return {"payload": {"contact": {"id": 55}}}
		if method == "POST" and path.endswith("/conversations"):
			return {"id": 900}
		if method == "POST" and (path.endswith("/messages") or path.endswith("/assignments")):
			return {"id": 1}
		raise AssertionError(path)


class TestWhatsappSend(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", "Administrator", "email", "administrator@example.com")
		from crm.api.pipeline import save_pipeline

		pipeline = save_pipeline({"pipeline_name": "WhatsApp teste", "stages": [{"deal_status": "Novo"}]})
		self.deal = frappe.get_doc(
			{"doctype": "CRM Deal", "pipeline": pipeline["name"], "status": pipeline["stages"][0]["name"], "first_name": "Cliente"}
		).insert(ignore_permissions=True).name
		doc = frappe.get_single(welcome.CONNECTION)
		doc.account_id = 3
		doc.access_token = "chave-boa"
		doc.flags.ignore_permissions = True
		doc.save()

	def tearDown(self):
		frappe.db.rollback()

	def chat(self, **kwargs):
		fake = FakeChat(**kwargs)
		patcher = patch.object(welcome, "_call", fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake

	def test_seller_sees_only_the_whatsapp_inboxes_they_belong_to(self):
		self.chat()
		options = whatsapp_send.get_whatsapp_options(self.deal, "(48) 98888-7777")
		self.assertEqual([i["id"] for i in options["inboxes"]], [7, 12])
		official, qr = options["inboxes"]
		self.assertEqual((official["kind"], official["can_reply"]), ("official", False))
		self.assertEqual([t["name"] for t in official["templates"]], ["retomar"])
		self.assertEqual((qr["kind"], qr["can_reply"]), ("qr", True))

	def test_smartchat_admin_sees_every_whatsapp_inbox(self):
		self.chat(role="administrator")
		options = whatsapp_send.get_whatsapp_options(self.deal, PHONE)
		self.assertEqual([i["id"] for i in options["inboxes"]], [7, 9, 12])

	def test_open_window_allows_free_text_in_the_same_conversation(self):
		fake = self.chat(conversations=[{"id": 300, "inbox_id": 7, "can_reply": True}])
		result = whatsapp_send.send_whatsapp(self.deal, PHONE, 7, text="Oi, tudo certo?")
		self.assertEqual(result["conversation_id"], 300)
		posts = [(p, j) for m, p, j in fake.calls if m == "POST"]
		self.assertEqual(posts[0], ("/api/v1/accounts/3/conversations/300/messages", {"content": "Oi, tudo certo?", "message_type": "outgoing"}))
		self.assertEqual(posts[1], ("/api/v1/accounts/3/conversations/300/assignments", {"assignee_id": 41}))
		comment = frappe.get_all(
			"Comment", filters={"reference_doctype": "CRM Deal", "reference_name": self.deal}, pluck="content"
		)
		self.assertTrue(any("Oi, tudo certo?" in c and "Comercial" in c for c in comment))

	def test_closed_window_refuses_free_text(self):
		self.chat(conversations=[{"id": 300, "inbox_id": 7, "can_reply": False}])
		with self.assertRaises(frappe.ValidationError):
			whatsapp_send.send_whatsapp(self.deal, PHONE, 7, text="Oi")

	def test_template_starts_a_new_conversation(self):
		fake = self.chat()
		result = whatsapp_send.send_whatsapp(
			self.deal, PHONE, 7, name="Cliente", template_name="retomar", template_language="pt_BR", variables=json.dumps({"1": "Ana"})
		)
		self.assertEqual(result["conversation_id"], 900)
		created = next(j for m, p, j in fake.calls if m == "POST" and p.endswith("/conversations"))
		self.assertEqual(created["inbox_id"], 7)
		self.assertEqual(created["message"]["content"], "Olá Ana, podemos continuar?")
		self.assertEqual(created["message"]["template_params"]["processed_params"], {"body": {"1": "Ana"}})

	def test_template_needs_every_variable(self):
		self.chat()
		with self.assertRaises(frappe.ValidationError):
			whatsapp_send.send_whatsapp(self.deal, PHONE, 7, template_name="retomar", template_language="pt_BR", variables="{}")

	def test_qr_inbox_takes_free_text_without_a_conversation(self):
		fake = self.chat()
		whatsapp_send.send_whatsapp(self.deal, PHONE, 12, text="Olá!")
		created = next(j for m, p, j in fake.calls if m == "POST" and p.endswith("/conversations"))
		self.assertEqual((created["inbox_id"], created["message"]), (12, {"content": "Olá!"}))

	def test_inbox_of_someone_else_is_refused(self):
		self.chat()
		with self.assertRaises(frappe.PermissionError):
			whatsapp_send.send_whatsapp(self.deal, PHONE, 9, text="Oi")

	def test_user_missing_in_smartchat_gets_a_clear_message(self):
		self.chat()
		frappe.db.set_value("User", "Administrator", "email", "ninguem@example.com")
		options = whatsapp_send.get_whatsapp_options(self.deal, PHONE)
		self.assertIn("ninguem@example.com", options["error"])
		self.assertEqual(options["inboxes"], [])

	def test_without_connection_nothing_is_sent(self):
		self.chat()
		frappe.db.set_single_value(welcome.CONNECTION, "account_id", None)
		self.assertFalse(whatsapp_send.get_whatsapp_options(self.deal, PHONE)["connected"])
		with self.assertRaises(frappe.ValidationError):
			whatsapp_send.send_whatsapp(self.deal, PHONE, 7, text="Oi")

	# credtech#258: o mesmo envio a partir da página do contato
	def contact(self, phone=PHONE):
		return (
			frappe.get_doc({"doctype": "Contact", "first_name": "Cliente", "phone_nos": [{"phone": phone, "is_primary_mobile_no": 1}]})
			.insert(ignore_permissions=True)
			.name
		)

	def test_contact_sends_through_the_inbox_and_keeps_a_note(self):
		fake = self.chat(conversations=[{"id": 300, "inbox_id": 7, "can_reply": True}])
		contact = self.contact()
		self.assertEqual([i["id"] for i in whatsapp_send.get_whatsapp_options(contact=contact, phone=PHONE)["inboxes"]], [7, 12])
		result = whatsapp_send.send_whatsapp(contact=contact, phone=PHONE, inbox_id=7, text="Oi pelo contato")
		self.assertEqual(result["conversation_id"], 300)
		self.assertIn(("POST", "/api/v1/accounts/3/conversations/300/assignments", {"assignee_id": 41}), fake.calls)
		notes = frappe.get_all(
			"FCRM Note", filters={"reference_doctype": "Contact", "reference_docname": contact}, pluck="content"
		)
		self.assertTrue(any("Oi pelo contato" in n and 'target="_blank"' in n for n in notes))

	def test_contact_only_sends_to_its_own_numbers(self):
		self.chat()
		contact = self.contact()
		options = whatsapp_send.get_whatsapp_options(contact=contact, phone="+5548911112222")
		self.assertEqual(options["inboxes"], [])
		self.assertIn("not one of the contact", options["error"])
		with self.assertRaises(frappe.ValidationError):
			whatsapp_send.send_whatsapp(contact=contact, phone="+5548911112222", inbox_id=12, text="Oi")

	def test_without_card_or_contact_nothing_is_sent(self):
		self.chat()
		with self.assertRaises(frappe.ValidationError):
			whatsapp_send.send_whatsapp(phone=PHONE, inbox_id=12, text="Oi")
