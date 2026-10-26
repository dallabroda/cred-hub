"""Painel do Cred Hub na conversa do Cred Chat (credtech#45). A API do Cred Chat é simulada; nada sai do servidor.
Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import chat_panel, welcome

SELLER = "crm.user1@example.com"
PHONE = "+55 (48) 99999-1234"


class FakeChat:
	"""Perfil, caixas e Dashboard Apps da conta 3; guarda as chamadas."""

	def __init__(self, role="administrator", apps=None):
		self.role, self.apps, self.calls = role, apps or [], []

	def __call__(self, method, path, token, **kwargs):
		self.calls.append((method, path, kwargs.get("json")))
		if path == "/api/v1/profile":
			return {"accounts": [{"id": i, "name": "Acme", "role": self.role} for i in (3, 4)]}
		if path.endswith("/inboxes"):
			return {"payload": []}
		if path.endswith("/dashboard_apps"):
			return self.apps if method == "GET" else {"id": 42}
		if method == "DELETE":
			return {}
		raise AssertionError(path)


class TestChatPanel(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		if not frappe.db.exists("User", SELLER):
			frappe.get_doc(
				{"doctype": "User", "email": SELLER, "first_name": "Vendedor", "roles": [{"role": "Sales User"}]}
			).insert(ignore_permissions=True)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{
				"pipeline_name": "Painel",
				"stages": [{"deal_status": "Novo"}, {"deal_status": "Proposta"}, {"deal_status": "Ganho", "type": "Won"}],
				"members": [{"user": SELLER, "role": "Member"}],
			}
		)["name"]
		self.stages = frappe.get_all("CRM Deal Status", {"pipeline": self.pipeline}, pluck="name", order_by="position asc")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def card(self, phone=None, email=None, stage=0, **fields):
		doc = frappe.get_doc(
			{"doctype": "CRM Deal", "pipeline": self.pipeline, "status": self.stages[stage], "lead_name": "Maria", **fields}
		).insert(ignore_permissions=True)
		# Telefone e e-mail do card são o espelho do contato principal, como o CRM grava
		if phone or email:
			frappe.db.set_value("CRM Deal", doc.name, {"mobile_no": phone, "email": email})
		return doc.name

	def test_finds_cards_by_national_number_open_first(self):
		with_55 = self.card(PHONE)
		other_area = self.card("(38) 99999-1234")
		won = self.card("48999991234", stage=2)
		contact = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Maria", "phone_nos": [{"phone": "48 99999-1234", "is_primary_mobile_no": 1}]}
		).insert(ignore_permissions=True)
		by_contact = self.card(contacts=[{"contact": contact.name, "is_primary": 1}])
		names = [c["name"] for c in chat_panel.find_cards(phone="+5548999991234")]
		self.assertEqual(set(names), {with_55, won, by_contact})
		self.assertNotIn(other_area, names)
		self.assertEqual(names[-1], won)
		self.assertEqual(chat_panel.find_cards(phone="123"), [])

	def test_finds_by_email(self):
		name = self.card(email="maria@acme.com")
		self.assertEqual([c["name"] for c in chat_panel.find_cards(email="Maria@Acme.com")], [name])

	def test_user_sees_only_cards_they_can_open(self):
		mine = self.card(PHONE, deal_owner=SELLER)
		self.card(PHONE)  # sem dono: o membro do funil não vê
		frappe.set_user(SELLER)
		self.assertEqual([c["name"] for c in chat_panel.find_cards(phone=PHONE)], [mine])
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			chat_panel.find_cards(phone=PHONE)

	def test_update_stage_and_value(self):
		name = self.card(PHONE)
		card = chat_panel.update_card(name, status=self.stages[1], deal_value="2500.5")
		self.assertEqual((card["status"], card["deal_value"]), (self.stages[1], 2500.5))
		self.assertEqual([s["value"] for s in card["stages"]], self.stages)

	def test_connect_installs_panel_and_disconnect_removes_it(self):
		chat = FakeChat()
		with patch.object(welcome, "_call", chat):
			result = welcome.connect_chat(3, "chave")
			self.assertTrue(result["panel_installed"])
			post = next(c for c in chat.calls if c[0] == "POST")
			self.assertEqual(post[2]["dashboard_app"]["content"][0]["url"], chat_panel.panel_url())
			result = welcome.disconnect_chat()
		self.assertFalse(result["panel_installed"])
		self.assertIn(("DELETE", "/api/v1/accounts/3/dashboard_apps/42", None), chat.calls)

	def test_existing_panel_is_reused_and_agent_key_only_warns(self):
		chat = FakeChat(apps=[{"id": 9, "content": [{"type": "frame", "url": chat_panel.panel_url()}]}])
		with patch.object(welcome, "_call", chat):
			welcome.connect_chat(3, "chave")
		self.assertEqual(frappe.get_single(welcome.CONNECTION).panel_app_id, 9)
		self.assertFalse(any(c[0] == "POST" for c in chat.calls))
		with patch.object(welcome, "_call", FakeChat(role="agent")):
			result = welcome.connect_chat(3, "chave")
		self.assertTrue(result["connected"])
		self.assertFalse(result["panel_installed"])
		self.assertIn("administrator", result["panel_error"])


	def test_value_from_products_is_read_only(self):
		name = self.card(PHONE, products=[{"product_name": "Plano", "qty": 2, "rate": 300}])
		card = chat_panel.update_card(name, deal_value="9999")
		self.assertTrue(card["value_from_products"])
		self.assertEqual(card["deal_value"], 600)

	def test_reconnecting_to_another_account_removes_the_old_panel(self):
		chat = FakeChat()
		with patch.object(welcome, "_call", chat):
			welcome.connect_chat(3, "chave")
			welcome.connect_chat(4, "chave")
		self.assertIn(("DELETE", "/api/v1/accounts/3/dashboard_apps/42", None), chat.calls)
		self.assertEqual(frappe.get_single(welcome.CONNECTION).account_id, 4)
