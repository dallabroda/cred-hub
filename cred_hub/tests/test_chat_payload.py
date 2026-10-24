"""Testes sem Frappe: chave de telefone e leitura do webhook do Cred Chat."""

import unittest

from cred_hub.chat.payload import conversation_url, parse_conversation, phone_key, split_name

# Recorte de um `conversation_created` real do Cred Chat (Conversations::EventDataPresenter#webhook_data)
PAYLOAD = {
	"event": "conversation_created",
	"id": 42,
	"account": {"id": 1, "name": "CredTech"},
	"inbox_id": 12,
	"channel": "Channel::Whatsapp",
	"meta": {"sender": {"id": 7, "name": "Maria da Silva", "phone_number": "+5548999887766", "email": None}},
	"messages": [{"content": "Oi, quero saber sobre crédito"}],
}


class TestPhoneKey(unittest.TestCase):
	def test_same_brazilian_number_in_any_format(self):
		key = phone_key("+5548999887766")
		for raw in ("+55 (48) 99988-7766", "48999887766", "554899887766", "(48) 9988-7766", "5548999887766"):
			self.assertEqual(phone_key(raw), key, raw)

	def test_different_numbers(self):
		self.assertNotEqual(phone_key("+5548999887766"), phone_key("+5511999887766"))
		self.assertNotEqual(phone_key("+5548999887766"), phone_key("+5548999887765"))

	def test_foreign_and_empty(self):
		self.assertEqual(phone_key("+1 (415) 555-0100"), "14155550100")
		self.assertEqual(phone_key(None), "")


class TestParseConversation(unittest.TestCase):
	def test_parse(self):
		conv = parse_conversation(PAYLOAD)
		self.assertEqual((conv.conversation_id, conv.account_id), (42, 1))
		self.assertEqual(conv.channel, "Channel::Whatsapp")
		self.assertEqual((conv.name, conv.phone, conv.email), ("Maria da Silva", "+5548999887766", ""))
		self.assertEqual(conv.first_message, "Oi, quero saber sobre crédito")

	def test_parse_missing_parts(self):
		conv = parse_conversation({"id": "x"})
		self.assertIsNone(conv.conversation_id)
		self.assertEqual((conv.name, conv.phone, conv.first_message), ("", "", ""))

	def test_helpers(self):
		self.assertEqual(split_name("Maria da Silva"), ("Maria", "da Silva"))
		self.assertEqual(split_name(""), ("", ""))
		self.assertEqual(
			conversation_url("https://chat.credtechdigital.com.br/", 1, 42),
			"https://chat.credtechdigital.com.br/app/accounts/1/conversations/42",
		)
