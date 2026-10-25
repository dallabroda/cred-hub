"""Padrões de cada site (#25). Roda no bench: `bench --site <site> run-tests --app cred_hub`.
Sem commit: o tearDown desfaz tudo."""

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import site_setup

AUTH0 = {"domain": "https://example.us.auth0.com/", "client_id": "cid", "client_secret": "secret"}


class TestSiteSetup(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_defaults_and_invite_only(self):
		frappe.db.set_single_value("Website Settings", "disable_signup", 0)
		site_setup.apply(commit=False)
		self.assertEqual(frappe.db.get_single_value("Website Settings", "disable_signup"), 1)
		self.assertEqual(frappe.db.get_single_value("System Settings", "allow_error_traceback"), 0)
		self.assertTrue(frappe.db.get_single_value("FCRM Settings", "currency"))
		self.assertEqual(
			frappe.db.get_value("DefaultValue", {"parent": "__default", "defkey": "time_zone"}, "defvalue"),
			frappe.db.get_single_value("System Settings", "time_zone") or site_setup.TIME_ZONE,
		)
		self.assertEqual(
			frappe.db.get_value("Translation", {"language": "pt-BR", "source_text": "Won"}, "translated_text"), "Ganho"
		)

	def test_social_login_buttons_never_create_users(self):
		site_setup.apply(auth0=AUTH0, commit=False)
		site_setup.apply(auth0=AUTH0, commit=False)  # pode repetir
		keys = frappe.get_all("Social Login Key", ["name", "provider_name", "sign_ups", "base_url", "auth_url_data"])
		by_name = {k.name: k for k in keys}
		self.assertEqual(set(by_name) & {"auth0", "microsoft", "github"}, {"auth0", "microsoft", "github"})
		for name in ("auth0", "microsoft", "github"):
			self.assertEqual(by_name[name].sign_ups, "Deny")
			self.assertEqual(by_name[name].base_url, "https://example.us.auth0.com")
		self.assertIn('"connection": "github"', by_name["github"].auth_url_data)
		self.assertEqual(
			frappe.utils.password.get_decrypted_password("Social Login Key", "github", "client_secret"), "secret"
		)

	def test_env_groups_are_optional(self):
		for var in ("SMTP_ADDRESS", "SMTP_PASSWORD", "AUTH0_DOMAIN", "AUTH0_CLIENT_SECRET"):
			self.assertNotIn(var, frappe.local.conf)
		self.assertEqual(len(site_setup.apply_from_env(commit=False)), 3)  # sem segredos: só os padrões
