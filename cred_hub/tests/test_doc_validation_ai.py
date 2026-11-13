"""Testes da Validação IA de Documentos e Fluxo de Recusa do Operador."""

import unittest
from unittest.mock import patch
import frappe

from cred_hub.credit_check import committee, onboarding
from cred_hub.credit_check.clients.vertex import VertexResult


class FakeVertex:
	def __init__(self, *answers):
		self.answers = list(answers)
		self.calls = []

	def generate_json(self, model, system, user_text, schema, max_output_tokens=2048):
		self.calls.append({"model": model, "system": system, "user": user_text, "schema": schema})
		answer = self.answers.pop(0)
		result = VertexResult("{}", "STOP", 1200, 300, 0, model, 1.2)
		if isinstance(answer, Exception):
			answer.result = result
			raise answer
		return answer, result


class TestDocValidationAI(unittest.TestCase):
	def setUp(self):
		super().setUp()
		self.org_name = "ORG-TEST-AI-DOC"
		if not frappe.db.exists("CRM Organization", self.org_name):
			org = frappe.get_doc(
				{
					"doctype": "CRM Organization",
					"organization_name": self.org_name,
					"cnpj": "12.345.678/0001-90",
					"email": "contato@empresa-teste.com.br",
				}
			)
			org.insert(ignore_permissions=True)
		self.org = frappe.get_doc("CRM Organization", self.org_name)

	def tearDown(self):
		super().tearDown()

	def test_ai_validation_not_configured_fallback(self):
		"""Quando o Vertex AI não está configurado, retorna None e usa fallback heurístico sem quebrar."""
		with patch("cred_hub.credit_check.opinion.is_configured", return_value=False):
			res = committee.validate_document_with_ai(
				organization_name=self.org_name,
				item_key="contrato_social",
				label="Contrato Social Consolidado",
				file_name="contrato.pdf",
				text="Texto de teste do contrato social da empresa 12.345.678/0001-90",
			)
			self.assertIsNone(res)

	def test_ai_validation_detects_rejection_risk(self):
		"""Quando a IA detecta documento incompatível ou CNPJ divergente, sugere recusa com o motivo."""
		fake_ai_output = {
			"is_valid_type": False,
			"detected_type": "Comprovante de Energia Elétrica",
			"cnpj_matches": False,
			"detected_cnpj": "98.765.432/0001-11",
			"is_signed": False,
			"is_expired": False,
			"expiration_date": "",
			"rejection_risk": True,
			"rejection_reasons": [
				"Documento enviado é Comprovante de Residência em vez do Contrato Social solicitado",
				"CNPJ 98.765.432/0001-11 diverge do cadastro 12.345.678/0001-90",
			],
			"extracted_metrics": {},
			"summary": "Documento incorreto e de outra titularidade. Necessário reenviar o Contrato Social.",
		}
		fake_client = FakeVertex(fake_ai_output)

		with patch("cred_hub.credit_check.opinion.is_configured", return_value=True), patch(
			"cred_hub.credit_check.opinion.vertex_client", return_value=fake_client
		):
			res = committee.validate_document_with_ai(
				organization_name=self.org_name,
				item_key="contrato_social",
				label="Contrato Social Consolidado",
				file_name="conta_luz.pdf",
				text="Companhia de Eletricidade - Fatura de Energia - CNPJ 98.765.432/0001-11",
			)
			self.assertIsNotNone(res)
			self.assertTrue(res["ai_validated"])
			self.assertTrue(res["rejection_risk"])
			self.assertEqual(res["suggested_action"], "reject")
			self.assertIn("Documento enviado é Comprovante de Residência", res["suggested_reason"])

	def test_ai_validation_valid_document(self):
		"""Quando o documento está em conformidade, a IA sugere aprovação (verify)."""
		fake_ai_output = {
			"is_valid_type": True,
			"detected_type": "Contrato Social Consolidado",
			"cnpj_matches": True,
			"detected_cnpj": "12.345.678/0001-90",
			"is_signed": True,
			"is_expired": False,
			"expiration_date": "",
			"rejection_risk": False,
			"rejection_reasons": [],
			"extracted_metrics": {
				"capital_social": 250000.0,
				"administrador": "Carlos Silva",
				"regime_assinatura": "Isoladamente",
			},
			"summary": "12ª Alteração Contratual com carimbo da Junta Comercial. Administração isolada por Carlos Silva.",
		}
		fake_client = FakeVertex(fake_ai_output)

		with patch("cred_hub.credit_check.opinion.is_configured", return_value=True), patch(
			"cred_hub.credit_check.opinion.vertex_client", return_value=fake_client
		):
			res = committee.validate_document_with_ai(
				organization_name=self.org_name,
				item_key="contrato_social",
				label="Contrato Social Consolidado",
				file_name="contrato_social_12.pdf",
				text="Junta Comercial do Estado - 12a Alteracao Contratual - CNPJ 12.345.678/0001-90 - Capital Social R$ 250.000,00",
			)
			self.assertIsNotNone(res)
			self.assertTrue(res["ai_validated"])
			self.assertFalse(res["rejection_risk"])
			self.assertEqual(res["suggested_action"], "verify")
			self.assertEqual(res["extracted_metrics"]["capital_social"], 250000.0)

	def test_operator_reject_flow_and_public_portal_visibility(self):
		"""Operador rejeita documento com motivo e o portal público expõe 'rejection_reason' para o cedente."""
		portal_state = onboarding.generate_portal_link(self.org_name)
		token = portal_state["portal"]["token"]

		# 1. Simula anexo do documento
		onboarding.update_checklist_item(
			organization_name=self.org_name,
			item_key="balanco_dre",
			file_url="/private/files/balanco_rascunho.pdf",
			file_name="balanco_rascunho.pdf",
			status="attached",
		)

		# 2. Operador confere e recusa com motivo
		reason_text = "Falta assinatura do contador com CRC ativo e notas explicativas."
		updated_state = onboarding.update_checklist_item(
			organization_name=self.org_name,
			item_key="balanco_dre",
			status="rejected",
			notes=reason_text,
		)
		balanco_item = next(i for i in updated_state["checklist"] if i["key"] == "balanco_dre")
		self.assertEqual(balanco_item["status"], "rejected")
		self.assertEqual(balanco_item["notes"], reason_text)

		# 3. Portal público do cedente acessa e confere se a tarja de rejeição com motivo está disponível
		portal_view = onboarding.public_verify_and_list(token=token, cnpj="12.345.678/0001-90")
		portal_balanco = next(i for i in portal_view["checklist"] if i["key"] == "balanco_dre")
		self.assertEqual(portal_balanco["status"], "rejected")
		self.assertEqual(portal_balanco["rejection_reason"], reason_text)
		self.assertFalse(portal_balanco["locked"])
