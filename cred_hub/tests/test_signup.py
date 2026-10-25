"""Cadastro self-service (credtech#25). Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import signup


class TestSignup(IntegrationTestCase):
	def setUp(self):
		frappe.local.conf.credhub_signup = 1
		frappe.local.request_ip = "203.0.113.7"
		self.sent = []
		patcher = patch.object(signup, "_send_confirmation", lambda doc, token: self.sent.append((doc, token)))
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.local.conf.pop("credhub_signup", None)
		frappe.db.rollback()

	def request(self, subdomain="acme-teste", email="ana@example.com"):
		signup.request_signup.__wrapped__("Acme Teste Ltda", subdomain, "Ana Souza", email)
		return self.sent[-1]

	def test_only_on_central_site(self):
		frappe.local.conf.pop("credhub_signup", None)
		with self.assertRaises(frappe.PermissionError):
			signup.check_subdomain.__wrapped__("acme")

	def test_subdomain_rules(self):
		self.assertEqual(signup.availability("ab"), (False, "invalid"))
		self.assertEqual(signup.availability("-acme"), (False, "invalid"))
		self.assertEqual(signup.availability("xn--acme"), (False, "invalid"))
		self.assertEqual(signup.availability("chat"), (False, "reserved"))
		self.assertEqual(signup.availability("credtech"), (False, "reserved"))
		self.assertEqual(signup.availability("acme-teste"), (True, None))
		with patch.object(signup, "_site_exists", return_value=True):
			self.assertEqual(signup.availability("acme-teste"), (False, "taken"))

	def test_suggestion_from_company_name(self):
		self.assertEqual(signup.slugify("Padaria São João Ltda."), "padaria-sao-joao")
		self.assertEqual(signup.slugify("ACME S/A"), "acme")
		with patch.object(signup, "_site_exists", side_effect=lambda site: site.startswith("padaria-sao-joao.")):
			self.assertEqual(signup.suggest_subdomain.__wrapped__("Padaria São João")["subdomain"], "padaria-sao-joao-2")

	def test_request_keeps_only_token_hash_and_holds_the_address(self):
		doc, token = self.request()
		self.assertEqual(doc.status, "Pending Confirmation")
		self.assertNotIn(token, frappe.as_json(doc.as_dict()))
		self.assertEqual(doc.token_hash, signup._hash(token))
		self.assertEqual(signup.availability("acme-teste"), (False, "taken"))
		with self.assertRaises(frappe.ValidationError):
			self.request(email="outra@example.com")

	def test_expired_link_frees_the_address(self):
		doc, token = self.request()
		doc.db_set("expires_on", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1))
		self.assertEqual(signup.availability("acme-teste"), (True, None))
		self.assertEqual(signup.confirm_signup.__wrapped__(token)["status"], "Expired")

	def test_confirm_queues_provisioning_once(self):
		doc, token = self.request()
		with patch.object(frappe, "enqueue") as enqueue:
			self.assertEqual(signup.confirm_signup.__wrapped__(token)["status"], "Confirmed")
			signup.confirm_signup.__wrapped__(token)  # segundo clique não cria outra empresa
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["queue"], signup.PROVISION_QUEUE)
		self.assertEqual(enqueue.call_args.kwargs["signup"], doc.name)

	def test_unknown_token(self):
		with self.assertRaises(frappe.DoesNotExistError):
			signup.signup_status.__wrapped__("nope")

	def test_too_many_requests_per_email(self):
		for n in range(3):
			self.request(subdomain=f"acme-{n}a")
		with self.assertRaises(frappe.RateLimitExceededError):
			self.request(subdomain="acme-9a")

	def test_provision_runs_bench_steps_and_activates(self):
		doc, token = self.request()
		doc.db_set("status", "Confirmed")
		calls = []
		env = {"CREDHUB_PROVISIONER_DB_USER": "prov", "CREDHUB_PROVISIONER_DB_PASSWORD": "pw"}
		with (
			patch.dict("os.environ", env),
			patch.object(signup, "_bench", side_effect=lambda args, **kw: calls.append(args) or ""),
			patch.object(frappe.db, "commit"),
		):
			signup.provision(doc.name)
		doc.reload()
		self.assertEqual(doc.status, "Active")
		self.assertEqual(doc.site_name, "acme-teste.credtechdigital.com.br")
		self.assertEqual(calls[0][:2], ["new-site", "acme-teste.credtechdigital.com.br"])
		self.assertIn("cred_hub.site_setup.apply_from_env", calls[2])
		self.assertIn("cred_hub.signup.create_owner", calls[3])

	def test_provision_failure_is_recorded(self):
		doc, token = self.request()
		doc.db_set("status", "Confirmed")
		with (
			patch.dict("os.environ", {"CREDHUB_PROVISIONER_DB_USER": "prov", "CREDHUB_PROVISIONER_DB_PASSWORD": "pw"}),
			patch.object(signup, "_bench", side_effect=RuntimeError("bench new-site failed: boom")),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			signup.provision(doc.name)
		doc.reload()
		self.assertEqual(doc.status, "Failed")
		self.assertIn("boom", doc.error)
		self.assertEqual(signup.signup_status.__wrapped__(token)["address"], None)
