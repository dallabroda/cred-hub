"""Pedido de integração com outra telefonia (credtech#259).
Roda no bench: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_telephony_request`."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import telephony_request

CONF = {"url": "https://credtech.example.com/api/method/cred_hub.lead_intake.receive", "source": "abc123", "key": "segredo"}


class TestTelephonyRequest(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.conf["credhub_telephony_request"] = CONF
		self.addCleanup(frappe.local.conf.pop, "credhub_telephony_request", None)

	def _answer(self, payload, ok=True):
		response = MagicMock(ok=ok)
		response.json.return_value = payload
		return response

	def test_sends_the_request_to_the_credtech_lead_source(self):
		with patch.object(telephony_request.requests, "post", return_value=self._answer({"message": {"ok": True}})) as post:
			result = telephony_request.request_integration(" Zenvia ", "https://docs.zenvia.com", "Ligar do card")

		self.assertEqual(result, {"ok": True})
		url = post.call_args.args[0]
		kwargs = post.call_args.kwargs
		self.assertEqual(url, f"{CONF['url']}?source=abc123")
		self.assertEqual(kwargs["headers"], {"X-CredHub-Key": "segredo"})
		answers = kwargs["json"]["answers"]
		self.assertEqual(answers["Telefonia"], "Zenvia")
		self.assertEqual(answers["Documentação da API"], "https://docs.zenvia.com")
		self.assertEqual(answers["Como usa"], "Ligar do card")
		self.assertEqual(kwargs["json"]["fields"]["email"], frappe.db.get_value("User", "Administrator", "email"))

	def test_needs_the_telephony_company(self):
		with patch.object(telephony_request.requests, "post") as post:
			self.assertRaises(frappe.ValidationError, telephony_request.request_integration, "  ")
		post.assert_not_called()

	def test_tells_the_customer_when_it_is_not_configured_or_did_not_arrive(self):
		frappe.local.conf.pop("credhub_telephony_request")
		self.assertRaises(frappe.ValidationError, telephony_request.request_integration, "Zenvia")

		frappe.local.conf["credhub_telephony_request"] = CONF
		with patch.object(telephony_request.requests, "post", return_value=self._answer({"message": {"ok": False}})):
			self.assertRaises(frappe.ValidationError, telephony_request.request_integration, "Zenvia")
