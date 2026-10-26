"""Cadastro self-service (credtech#25). Roda no bench: `bench --site <site> run-tests --app cred_hub`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import signup


class TestSignup(IntegrationTestCase):
	def setUp(self):
		frappe.local.conf.credhub_signup = 1
		self.domain = frappe.local.conf.pop("credhub_signup_domain", None)
		frappe.local.request_ip = "203.0.113.7"
		self.sent = []
		patcher = patch.object(signup, "_send_confirmation", lambda doc, token: self.sent.append((doc, token)))
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.local.conf.pop("credhub_signup", None)
		if self.domain:
			frappe.local.conf.credhub_signup_domain = self.domain
		frappe.db.rollback()

	def request(self, subdomain="acme-teste", email="ana@example.com"):
		signup.request_signup.__wrapped__("Acme Teste Ltda", subdomain, "Ana Souza", email, accepted_terms=1)
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

	def provision(self, doc, bench=None, ready=False, create=None):
		calls = []
		env = {"CREDHUB_PROVISIONER_DB_USER": "prov", "CREDHUB_PROVISIONER_DB_PASSWORD": "pw"}
		with (
			patch.dict("os.environ", env),
			patch.object(signup, "_bench", side_effect=bench or (lambda args, **kw: calls.append(args) or "")),
			patch.object(signup, "_site_ready", return_value=ready),
			patch.object(signup, "_site_exists", return_value=ready),
			patch.object(signup, "_create_site", side_effect=create or (lambda site: calls.append(["create", site]))),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
			patch.object(signup, "_alert_team") as alert,
		):
			signup.provision(doc.name)
		doc.reload()
		self.alerted = alert.called
		return calls

	def test_provision_runs_steps_and_activates(self):
		doc, token = self.request()
		doc.db_set("status", "Confirmed")
		calls = self.provision(doc)
		self.assertEqual(doc.status, "Active")
		self.assertEqual(doc.site_name, "acme-teste.credtechdigital.com.br")
		self.assertEqual(calls[0], ["create", "acme-teste.credtechdigital.com.br"])
		self.assertIn("cred_hub.site_setup.apply_from_env", calls[2])
		self.assertIn("cred_hub.signup.create_owner", calls[3])
		self.assertNotIn("pw", frappe.as_json(calls))  # nenhuma senha na linha de comando

	def test_failure_is_recorded_and_retry_resumes(self):
		doc, token = self.request()
		doc.db_set("status", "Confirmed")

		def setup_fails(args, **kw):
			if "cred_hub.site_setup.apply_from_env" in args:
				raise RuntimeError("bench --site x execute failed: smtp down")
			return ""

		self.provision(doc, bench=setup_fails)
		self.assertEqual(doc.status, "Failed")
		self.assertIn("smtp down", doc.error)
		self.assertTrue(self.alerted)  # equipe avisada
		self.assertIsNone(signup.signup_status.__wrapped__(token)["address"])
		self.assertEqual(signup.availability("acme-teste"), (False, "taken"))  # não libera um endereço criado

		with patch.object(frappe, "enqueue") as enqueue:
			signup.retry_provision(doc.name)
		enqueue.assert_called_once()
		doc.reload()
		calls = self.provision(doc, ready=True)  # site já criado: não cria de novo
		self.assertEqual(doc.status, "Active")
		self.assertNotIn("create", [c[0] for c in calls])

	def test_retry_only_failed(self):
		doc, token = self.request()
		with self.assertRaises(frappe.ValidationError):
			signup.retry_provision(doc.name)

	def test_create_owner_can_run_again(self):
		email = "dono.repetido@example.com"
		with patch.object(frappe.db, "commit"):
			signup.create_owner(email, "Dono Repetido")
			frappe.db.set_value("User", email, "enabled", 0)
			signup.create_owner(email, "Dono Repetido")
		self.assertTrue(frappe.db.get_value("User", email, "enabled"))
		self.assertIn("System Manager", frappe.get_roles(email))

	def test_terms_must_be_accepted_and_are_recorded(self):
		with self.assertRaises(frappe.ValidationError):
			signup.request_signup.__wrapped__("Acme", "acme-termos", "Ana", "ana@example.com")
		doc, token = self.request(subdomain="acme-termos")
		self.assertEqual(doc.terms_version, signup.TERMS_VERSION)
		self.assertTrue(doc.terms_accepted_on)

	def test_same_request_again_resends_a_new_link(self):
		doc, first = self.request()
		again, second = self.request()  # mesmo e-mail e endereço: não é "em uso"
		self.assertEqual(again.name, doc.name)
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.count("Cred Hub Signup", {"subdomain": "acme-teste"}), 1)
		with self.assertRaises(frappe.DoesNotExistError):
			signup.signup_status.__wrapped__(first)  # o link antigo deixa de valer
		self.assertEqual(signup.signup_status.__wrapped__(second)["status"], "Pending Confirmation")

	def test_resends_are_capped(self):
		for _n in range(signup.MAX_LINKS):
			self.request()
		self.assertEqual(frappe.db.get_value("Cred Hub Signup", {"subdomain": "acme-teste"}, "links_sent"), signup.MAX_LINKS)
		with self.assertRaises(frappe.RateLimitExceededError):
			self.request()
