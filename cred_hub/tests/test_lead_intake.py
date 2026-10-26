"""Leads por webhook: Google Ads, site, Typeform e genérico (credtech#51).
Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import lead_intake

SELLER = "crm.user1@example.com"


class TestLeadIntake(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{
				"pipeline_name": "Intake Webhook",
				"stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}],
				"members": [{"user": SELLER, "role": "Member"}],
			}
		)["name"]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def source(self, kind, **kw):
		created = lead_intake.create_webhook_source(kind, f"Teste {kind}", self.pipeline, **kw)
		return frappe.get_doc(lead_intake.SOURCE, created["name"]), created["key"]

	def post(self, source, body, headers=None):
		raw = json.dumps(body).encode()
		request = MagicMock(headers=headers or {})
		request.get_data.return_value = raw
		with patch.object(frappe, "request", request):
			frappe.local.request_ip = "203.0.113.9"
			return lead_intake.receive(source.name)

	def card(self):
		return frappe.get_doc("CRM Deal", {"pipeline": self.pipeline})

	def test_webhook_creates_assigned_card_with_utm(self):
		source, key = self.source("webhook")
		body = {
			"fields": {"first_name": "Rui", "email": "rui@example.com", "nao_existe": "x"},
			"answers": {"Valor do imóvel": "800 mil"},
			"utm_source": "newsletter",
			"utm_campaign": "outubro",
			"landing_page": "https://acme.test/home-equity",
		}
		self.assertTrue(self.post(source, body, {"X-CredHub-Key": key})["ok"])
		card = self.card()
		self.assertEqual((card.deal_owner, card.utm_campaign), (SELLER, "outubro"))
		self.assertIn("800 mil", frappe.get_all("FCRM Note", {"reference_docname": card.name}, pluck="content")[0])
		with self.assertRaises(frappe.AuthenticationError):
			self.post(source, body, {"X-CredHub-Key": "errada"})

	def test_key_shown_once_then_only_hint(self):
		source, key = self.source("webhook")
		listed = next(s for s in lead_intake.get_webhook_sources() if s.name == source.name)
		self.assertEqual(listed.key_hint, key[-4:])
		self.assertNotIn("key", listed)
		new_key = lead_intake.regenerate_webhook_key(source.name)["key"]
		with self.assertRaises(frappe.AuthenticationError):
			self.post(source, {"fields": {"first_name": "X"}}, {"X-CredHub-Key": key})
		self.assertTrue(self.post(source, {"fields": {"first_name": "X"}}, {"X-CredHub-Key": new_key})["ok"])

	def test_google_ads_lead(self):
		source, key = self.source("google_ads")
		body = {
			"lead_id": "g-1",
			"google_key": key,
			"campaign_id": 111,
			"adgroup_id": 222,
			"creative_id": 333,
			"gcl_id": "gclid-abc",
			"user_column_data": [
				{"column_id": "FULL_NAME", "string_value": "Maria Google"},
				{"column_id": "EMAIL", "string_value": "maria.g@example.com"},
				{"column_name": "Qual o prazo?", "string_value": "12 meses"},
			],
		}
		self.post(source, body)
		card = self.card()
		self.assertEqual((card.ad_platform, card.gclid, card.ad_campaign_id), ("Google Ads", "gclid-abc", "111"))
		self.post(source, body)  # o Google reenviou: não cria de novo
		self.assertEqual(frappe.db.count("CRM Deal", {"pipeline": self.pipeline}), 1)

	def test_typeform_signature(self):
		source, key = self.source("typeform")
		body = {
			"form_response": {
				"token": "tf-1",
				"definition": {"title": "Simulação", "fields": [{"id": "f1", "title": "Seu nome"}, {"id": "f2", "title": "E-mail"}]},
				"answers": [
					{"type": "text", "text": "Lia", "field": {"id": "f1"}},
					{"type": "email", "email": "lia@example.com", "field": {"id": "f2"}},
				],
				"hidden": {"utm_source": "instagram"},
			}
		}
		raw = json.dumps(body).encode()
		signature = "sha256=" + base64.b64encode(hmac.new(key.encode(), raw, hashlib.sha256).digest()).decode()
		self.post(source, body, {"Typeform-Signature": signature})
		card = self.card()
		self.assertEqual((card.email, card.utm_source), ("lia@example.com", "instagram"))
		with self.assertRaises(frappe.AuthenticationError):
			self.post(source, body, {"Typeform-Signature": "sha256=errada"})

	def test_website_only_from_registered_site_and_honeypot(self):
		source, key = self.source("website", allowed_origins="https://www.acme.test")
		body = {"key": key, "fields": {"first_name": "Leo", "email": "leo@example.com"}}
		with self.assertRaises(frappe.AuthenticationError):
			self.post(source, body, {"Origin": "https://outro.test"})
		self.post(source, {**body, "website": "spam"}, {"Origin": "https://www.acme.test"})  # isca preenchida
		self.assertFalse(frappe.db.exists("CRM Deal", {"pipeline": self.pipeline}))
		self.post(source, body, {"Origin": "https://www.acme.test"})
		self.assertTrue(frappe.db.exists("CRM Deal", {"pipeline": self.pipeline}))

	def test_website_rate_limit_per_visitor(self):
		source, key = self.source("website", allowed_origins="https://www.acme.test")
		frappe.cache.delete_value(f"credhub_intake:{source.name}:203.0.113.9")
		for n in range(lead_intake.WEBSITE_LIMIT + 2):
			self.post(source, {"key": key, "fields": {"first_name": f"P{n}", "email": f"p{n}@example.com"}}, {"Origin": "https://www.acme.test"})
		self.assertEqual(frappe.db.count("CRM Deal", {"pipeline": self.pipeline}), lead_intake.WEBSITE_LIMIT)
		frappe.cache.delete_value(f"credhub_intake:{source.name}:203.0.113.9")

	def test_activity_lists_all_sources_with_label(self):
		from cred_hub.meta_leads import get_lead_source_activity

		source, key = self.source("webhook")
		self.post(source, {"fields": {"first_name": "Ana"}}, {"X-CredHub-Key": key})
		activity = get_lead_source_activity()
		self.assertEqual(activity[0]["source_label"], "Teste webhook")
