"""Testes de integração do documento certo em cada cadastro (credtech#315): CPF é Contato, CNPJ é Organização.

Rodam no bench: `bench --site <site> run-tests --module cred_hub.tests.test_party_events`.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.cnpj import card
from cred_hub.party.resolve import party_for_document
from cred_hub.tests.test_party_document import CNPJ_OK, CPF_OK


class FakeCard(frappe._dict):
	def add_comment(self, comment_type, text):
		self.setdefault("comments", []).append(text)


class TestPartyEvents(IntegrationTestCase):
	def setUp(self):
		# A consulta à Receita não roda nos testes
		self.enqueue = patch("frappe.enqueue")
		self.enqueue.start()

	def tearDown(self):
		self.enqueue.stop()
		frappe.db.rollback()

	def _org(self, name, cnpj):
		return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, "cnpj": cnpj}).insert()

	def _contact(self, first_name, cpf=None):
		return frappe.get_doc({"doctype": "Contact", "first_name": first_name, "cpf": cpf}).insert()

	# Organização

	def test_organization_rejects_cpf(self):
		with self.assertRaises(frappe.ValidationError):
			self._org("Pessoa Fisica Teste 315", CPF_OK)
		with self.assertRaises(frappe.ValidationError):
			self._org("Pessoa Fisica Teste 315 B", "52998224725")

	def test_organization_with_cnpj_saves(self):
		org = self._org("Empresa Teste 315", "11222333000181")
		self.assertEqual(org.cnpj, CNPJ_OK)

	def test_organization_with_mistyped_cnpj_only_warns(self):
		# decisão do dono (24/10/2026): CNPJ com dígito errado não bloqueia
		org = self._org("Empresa Digito Errado 315", "11.222.333/0001-82")
		self.assertTrue(frappe.db.exists("CRM Organization", org.name))

	def test_old_organization_with_cpf_stays_editable(self):
		org = self._org("Empresa Antiga 315", CNPJ_OK)
		frappe.db.set_value("CRM Organization", org.name, "cnpj", CPF_OK)
		org.reload()
		org.website = "https://exemplo.com.br"
		org.save()
		self.assertEqual(frappe.db.get_value("CRM Organization", org.name, "website"), "https://exemplo.com.br")
		org.cnpj = "111.444.777-35"
		with self.assertRaises(frappe.ValidationError):
			org.save()

	# Contato

	def test_contact_rejects_cnpj(self):
		with self.assertRaises(frappe.ValidationError):
			self._contact("Empresa no Contato", CNPJ_OK)

	def test_contact_cpf_is_formatted(self):
		contact = self._contact("Ana Teste 315", "52998224725")
		self.assertEqual(contact.cpf, CPF_OK)

	def test_contact_with_mistyped_cpf_only_warns(self):
		contact = self._contact("Bruno Teste 315", "529.982.247-26")
		self.assertEqual(frappe.db.get_value("Contact", contact.name, "cpf"), "529.982.247-26")

	# Achar ou criar pelo documento (importação)

	def test_party_for_cpf_creates_contact_once(self):
		doctype, name = party_for_document("52998224725", "MARIA DA SILVA TESTE")
		self.assertEqual(doctype, "Contact")
		contact = frappe.get_doc("Contact", name)
		self.assertEqual(contact.cpf, CPF_OK)
		self.assertEqual(contact.first_name, "Maria da Silva Teste")
		self.assertEqual(party_for_document(CPF_OK, "Outro Nome"), ("Contact", name))
		self.assertFalse(frappe.db.exists("CRM Organization", {"cnpj": ("in", (CPF_OK, "52998224725"))}))

	def test_party_for_cnpj_creates_organization_once(self):
		doctype, name = party_for_document("11222333000181", "Empresa Importada 315")
		self.assertEqual(doctype, "CRM Organization")
		self.assertEqual(frappe.db.get_value("CRM Organization", name, "cnpj"), CNPJ_OK)
		self.assertEqual(party_for_document(CNPJ_OK, "Nome Diferente"), ("CRM Organization", name))

	def test_party_for_invalid_document_fails(self):
		for value in ("529.982.247-26", "123", ""):
			with self.assertRaises(frappe.ValidationError):
				party_for_document(value, "Qualquer")

	# Card criado de lead com CPF

	def test_lead_cpf_goes_to_the_card_contact(self):
		contact = self._contact("Carla Teste 315")
		deal = FakeCard(contacts=[frappe._dict(contact=contact.name, is_primary=1)])
		card._cpf_to_contact(deal, "52998224725")
		self.assertEqual(frappe.db.get_value("Contact", contact.name, "cpf"), CPF_OK)
		self.assertFalse(deal.get("comments"))

	def test_lead_cpf_never_overwrites_another_cpf(self):
		contact = self._contact("Daniel Teste 315", "111.444.777-35")
		deal = FakeCard(contacts=[frappe._dict(contact=contact.name, is_primary=1)])
		card._cpf_to_contact(deal, CPF_OK)
		self.assertEqual(frappe.db.get_value("Contact", contact.name, "cpf"), "111.444.777-35")
		self.assertEqual(len(deal.comments), 1)
		self.assertIn(CPF_OK, deal.comments[0])

	def test_lead_cpf_without_contact_only_comments(self):
		deal = FakeCard(contacts=[])
		card._cpf_to_contact(deal, CPF_OK)
		self.assertEqual(len(deal.comments), 1)
