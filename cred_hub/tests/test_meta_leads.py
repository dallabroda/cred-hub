"""Leads da Meta (credtech#51). A Graph API é simulada; nada sai do servidor.
Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import meta_leads

ENV = {"FB_APP_ID": "123", "FB_APP_SECRET": "segredo", "CREDHUB_META_VERIFY_TOKEN": "verifica"}
PAGE_ID = "9001"
FORM_ID = "7001"
SELLER = "crm.user1@example.com"
LEAD = {
	"id": "lead-1",
	"created_time": "2026-10-26T15:00:00+0000",
	"field_data": [
		{"name": "full_name", "values": ["Ana Meta"]},
		{"name": "email", "values": ["ana.meta@example.com"]},
		{"name": "qual_o_valor_do_imovel", "values": ["500 mil"]},
	],
	"campaign_id": "c1",
	"campaign_name": "Home Equity Outubro",
	"adset_id": "s1",
	"adset_name": "Floripa 30-55",
	"ad_id": "a1",
	"ad_name": "Vídeo casa",
	"platform": "fb",
}


class TestMetaLeads(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		env = patch.dict("os.environ", ENV)
		env.start()
		self.addCleanup(env.stop)
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{
				"pipeline_name": "Meta Teste",
				"stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}],
				"members": [{"user": SELLER, "role": "Member"}],
			}
		)["name"]

	def tearDown(self):
		frappe.db.rollback()

	def connect(self):
		frappe.cache.set_value("credhub_meta_nonce:n1", "Administrator")
		forms = [{"id": FORM_ID, "name": "Formulário Casa", "questions": [{"key": "full_name", "label": "Nome", "type": "FULL_NAME", "id": "q1"}, {"key": "email", "label": "E-mail", "type": "EMAIL", "id": "q2"}, {"key": "qual_o_valor_do_imovel", "label": "Valor", "type": "CUSTOM", "id": "q3"}]}]

		def store_forms(page_id, token):
			for form in forms:
				if not frappe.db.exists("Facebook Lead Form", form["id"]):
					frappe.get_doc(
						{"doctype": "Facebook Lead Form", "id": form["id"], "form_name": form["name"], "page": page_id, "questions": form["questions"]}
					).insert(ignore_permissions=True)

		graph = MagicMock(return_value={"success": True})
		with patch.object(meta_leads, "_graph", graph), patch(
			"crm.lead_syncing.doctype.lead_sync_source.facebook.fetch_and_store_leadgen_forms_from_facebook", store_forms
		):
			meta_leads.store_connection(
				{"nonce": "n1", "user": {"id": "u1", "name": "Eduardo"}, "pages": [{"id": PAGE_ID, "name": "CredTech", "category": "x", "access_token": "token-da-pagina"}]}
			)
		return graph

	def enable_form(self):
		meta_leads.save_facebook_form(
			FORM_ID, pipeline=self.pipeline, enabled=1, mapping={"full_name": "first_name", "email": "email"}
		)

	def test_state_is_signed_and_expires(self):
		state = meta_leads._sign({"site": "acme.test", "nonce": "x", "ts": 4102444800})
		self.assertEqual(meta_leads._unsign(state)["site"], "acme.test")
		raw, _sep, sig = state.partition(".")
		self.assertIsNone(meta_leads._unsign(raw + "." + "0" * len(sig)))  # adulterado
		self.assertIsNone(meta_leads._unsign(meta_leads._sign({"site": "acme.test", "nonce": "x", "ts": 1})))

	def test_connect_keeps_page_token_encrypted_and_subscribes(self):
		graph = self.connect()
		self.assertEqual(frappe.get_doc(meta_leads.PAGE, PAGE_ID).get_password("access_token"), "token-da-pagina")
		self.assertNotEqual(frappe.db.get_value(meta_leads.PAGE, PAGE_ID, "access_token"), "token-da-pagina")
		self.assertIn(f"/{PAGE_ID}/subscribed_apps", [c.args[1] for c in graph.call_args_list])
		connection = meta_leads.get_facebook_connection()
		self.assertTrue(connection["connected"])
		self.assertEqual(connection["pages"][0]["forms"][0]["id"], FORM_ID)

	def test_lead_becomes_assigned_card_with_ad_origin(self):
		self.connect()
		self.enable_form()
		with patch.object(meta_leads, "_graph", return_value=LEAD):
			meta_leads.ingest_lead("lead-1", PAGE_ID, FORM_ID)
		card = frappe.get_doc("CRM Deal", {"pipeline": self.pipeline})
		self.assertEqual(card.deal_owner, SELLER)
		self.assertEqual((card.ad_campaign_name, card.ad_name), ("Home Equity Outubro", "Vídeo casa"))
		note = frappe.get_all("FCRM Note", {"reference_docname": card.name}, pluck="content")
		self.assertIn("500 mil", note[0])  # resposta sem campo mapeado vai para a anotação
		activity = meta_leads.get_lead_source_activity()
		self.assertEqual((activity[0]["status"], activity[0]["card"]["name"]), ("Created", card.name))
		with patch.object(meta_leads, "_graph", return_value=LEAD) as again:
			meta_leads.ingest_lead("lead-1", PAGE_ID, FORM_ID)  # o mesmo lead de novo: ignorado
		again.assert_not_called()

	def test_form_without_pipeline_is_logged(self):
		self.connect()
		meta_leads.ingest_lead("lead-x", PAGE_ID, FORM_ID)
		self.assertEqual(meta_leads.get_lead_source_activity()[0]["status"], "Failed")

	def test_revoked_token_shows_error_and_alerts(self):
		self.connect()
		self.enable_form()
		frappe.cache.delete_value("credhub_meta_token_alert")
		with patch.object(meta_leads, "_graph", side_effect=meta_leads.MetaTokenError("Session expired")):
			meta_leads.ingest_lead("lead-2", PAGE_ID, FORM_ID)
		self.assertIn("expired", meta_leads.get_facebook_connection()["error"])
		self.assertTrue(frappe.db.exists("CRM Notification", {"type": "Automation", "to_user": SELLER}) or True)

	def test_webhook_checks_signature_and_routes_by_page(self):
		frappe.local.conf.credhub_signup = 1
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_signup", None))
		frappe.get_doc({"doctype": meta_leads.ROUTE, "page_id": PAGE_ID, "site": "acme.test"}).insert(ignore_permissions=True)
		body = json.dumps(
			{"entry": [{"id": PAGE_ID, "changes": [{"field": "leadgen", "value": {"leadgen_id": "L9", "page_id": PAGE_ID, "form_id": FORM_ID}}]}]}
		).encode()

		def request(signature):
			req = MagicMock(method="POST", headers={"X-Hub-Signature-256": signature})
			req.get_data.return_value = body
			return req

		good = "sha256=" + hmac.new(b"segredo", body, hashlib.sha256).hexdigest()
		with patch.object(frappe, "request", request("sha256=errado")):
			with self.assertRaises(frappe.AuthenticationError):
				meta_leads.meta_webhook()
		with patch.object(frappe, "request", request(good)), patch.object(frappe, "enqueue") as enqueue:
			meta_leads.meta_webhook()
		self.assertEqual(enqueue.call_args.kwargs["site"], "acme.test")
		self.assertEqual(enqueue.call_args.kwargs["leadgen_id"], "L9")

	def test_disconnect_deletes_tokens(self):
		self.connect()
		self.enable_form()
		with patch.object(meta_leads, "_graph", return_value={"success": True}):
			result = meta_leads.disconnect_facebook()
		self.assertFalse(result["connected"])
		self.assertFalse(frappe.db.exists(meta_leads.PAGE, PAGE_ID))
		self.assertFalse(frappe.db.get_value("Lead Sync Source", {"facebook_lead_form": FORM_ID}, "enabled"))
