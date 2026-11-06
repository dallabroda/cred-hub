"""Testes da Pasta Digital (Checklist de Documentos) e do Relatório de Visita Técnica/Comercial.
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_onboarding`."""

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.credit_check import api, onboarding, opinion

SETTINGS = "Cred Hub Credit Settings"


class TestCreditOnboarding(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		onboarding._kv_set(SETTINGS, onboarding.TEMPLATE_FIELD, "")
		self.org = frappe.get_doc(
			{
				"doctype": "CRM Organization",
				"organization_name": f"Cedente Securitizadora {frappe.generate_hash(length=6)}",
				"cnpj": "11.222.333/0001-81",
			}
		).insert(ignore_permissions=True)
		onboarding._store_set(f"onboarding::{self.org.name}", "")

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_default_checklist_template_and_custom_save(self):
		tmpl = api.get_checklist_template()
		self.assertTrue(tmpl["is_default"])
		keys = [item["key"] for item in tmpl["items"]]
		self.assertIn("contrato_social", keys)
		self.assertIn("doc_socios", keys)
		self.assertIn("faturamento_12m", keys)

		saved = api.save_checklist_template(
			[
				{"key": "contrato_social", "label": "Contrato Social Consolidado", "category": "Societário", "required": True, "enabled": True},
				{"key": "termo_cessao", "label": "Termo de Cessão Assinado", "category": "Lastro", "required": True, "enabled": True},
			]
		)
		self.assertFalse(saved["is_default"])
		self.assertEqual([item["key"] for item in saved["items"]], ["contrato_social", "termo_cessao"])

	def test_checklist_attach_verify_and_summary_progress(self):
		state = api.get_credit_onboarding(self.org.name)
		self.assertEqual(state["summary"]["verified_count"], 0)
		self.assertFalse(state["summary"]["all_required_verified"])

		# Upload a file -> automatically becomes 'attached'
		state = api.update_checklist_item(
			organization_name=self.org.name,
			item_key="contrato_social",
			file_url="/private/files/contrato.pdf",
			file_name="contrato.pdf",
		)
		cs = next(item for item in state["checklist"] if item["key"] == "contrato_social")
		self.assertEqual(cs["status"], "attached")
		self.assertEqual(cs["file_name"], "contrato.pdf")

		# Verify all required documents -> all_required_verified becomes True
		for item in state["checklist"]:
			if item["required"]:
				state = api.update_checklist_item(
					organization_name=self.org.name,
					item_key=item["key"],
					status="verified",
					notes="Conferido com QSA",
				)

		self.assertTrue(state["summary"]["all_required_verified"])
		self.assertEqual(state["summary"]["required_verified_count"], state["summary"]["required_total"])

	def test_visit_report_saved_and_exposed_to_ai_opinion(self):
		state = api.save_visit_report(
			organization_name=self.org.name,
			visit_data={
				"status": "completed",
				"visit_date": "2026-11-04",
				"visitor_name": "Analista Comercial",
				"visit_type": "presencial",
				"property_type": "proprio",
				"operational_status": "otimo",
				"inventory_level": "alto",
				"employees_observed": "42 funcionários na planta",
				"recommendation": "favoravel",
				"notes": "Parque fabril próprio, estoque organizado e carteira pulverizada.",
				"photos": [{"file_url": "/private/files/fachada.jpg", "file_name": "fachada.jpg"}],
			},
		)
		self.assertEqual(state["visit"]["status"], "completed")
		self.assertEqual(state["visit"]["recommendation"], "favoravel")
		self.assertEqual(len(state["visit"]["photos"]), 1)

		data, _pol, _alerts = opinion.build_input(self.org.name, None)
		self.assertIn("onboarding", data)
		self.assertEqual(data["onboarding"]["visit_report"]["status"], "completed")
		self.assertEqual(data["onboarding"]["visit_report"]["recommendation"], "favoravel")
		self.assertEqual(data["onboarding"]["visit_report"]["photos_count"], 1)

	def test_relational_sync_and_rejection_notification(self):
		state = onboarding.generate_portal_link(self.org.name)
		self.assertTrue(state["portal"]["active"])
		if onboarding._relational_tables_ready():
			self.assertTrue(
				frappe.db.exists("Cred Hub Portal Token", {"token": state["portal"]["token"]})
			)
		state = onboarding.update_checklist_item(
			organization_name=self.org.name,
			item_key="contrato_social",
			status="rejected",
			notes="Documento ilegível, reenviar PDF original.",
		)
		cs = next(item for item in state["checklist"] if item["key"] == "contrato_social")
		self.assertEqual(cs["status"], "rejected")
		if onboarding._relational_tables_ready():
			rel_status = frappe.db.get_value(
				"Cred Hub Checklist Item",
				{"organization": self.org.name, "item_key": "contrato_social"},
				"status",
			)
			self.assertEqual(rel_status, "rejected")

