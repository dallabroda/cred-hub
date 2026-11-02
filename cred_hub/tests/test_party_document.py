"""Testes das regras puras de CPF e tipo de documento, sem Frappe
(`python -m pytest cred_hub/tests/test_party_document.py`)."""

import unittest

from cred_hub.party.document import CNPJ, CPF, document_kind, format_cpf, is_valid_cpf

# Documentos fictícios com dígitos verificadores válidos
CPF_OK = "529.982.247-25"
CNPJ_OK = "11.222.333/0001-81"


class TestPartyDocument(unittest.TestCase):
	def test_valid_cpf(self):
		for value in (CPF_OK, "52998224725", "111.444.777-35"):
			self.assertTrue(is_valid_cpf(value), value)

	def test_invalid_cpf(self):
		for value in ("529.982.247-26", "111.111.111-11", "5299822472", "", None, CNPJ_OK):
			self.assertFalse(is_valid_cpf(value), value)

	def test_format_cpf(self):
		self.assertEqual(format_cpf("52998224725"), CPF_OK)
		self.assertEqual(format_cpf(CPF_OK), CPF_OK)
		self.assertEqual(format_cpf(" 123 "), "123")
		self.assertEqual(format_cpf(None), "")

	def test_document_kind(self):
		self.assertEqual(document_kind(CPF_OK), CPF)
		self.assertEqual(document_kind("52998224725"), CPF)
		self.assertEqual(document_kind(CNPJ_OK), CNPJ)
		self.assertEqual(document_kind("11222333000181"), CNPJ)
		for value in ("529.982.247-26", "11.222.333/0001-82", "", None, "abc"):
			self.assertEqual(document_kind(value), "", value)
