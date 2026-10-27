"""E-mails do HubTech CRM (templates em cred_hub/templates/emails). Roda no bench:
`bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_emails`. Nada é enviado: renderiza como o
frappe.sendmail renderiza (template + layout + CSS em linha)."""

import frappe
from frappe.email.email_body import get_formatted_html
from frappe.tests import IntegrationTestCase
from frappe.utils.jinja import get_email_from_template

from cred_hub import site_setup

LOGO = "https://chat.credtechdigital.com.br/brand-assets/credchat-email-logo.png"


def render(template, args, wrapper="templates/emails/standard.html"):
	body, _text = get_email_from_template(template, args)
	return get_formatted_html("Assunto", body, with_container=True, wrapper=wrapper)


class TestEmails(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def assert_branded(self, html, link=None):
		self.assertIn(LOGO, html)
		self.assertIn("HubTech CRM", html)
		self.assertIn("contato@credtechdigital.com.br", html)
		self.assertIn("https://credtechdigital.com.br/termos/", html)
		self.assertIn("https://credtechdigital.com.br/privacidade/", html)
		for old in ("Cred Hub", "Frappe", "frappe.io", "frappeframework"):
			self.assertNotIn(old, html)
		if link:
			# botão e o mesmo link em texto
			self.assertGreaterEqual(html.count(link), 2)

	def test_invitation_says_who_invited_to_which_company(self):
		link = "https://acme.credtechdigital.com.br/api/method/crm.api.accept_invitation?key=abc"
		html = render(
			"crm_invitation",
			{
				"title": "HubTech CRM",
				"invite_link": link,
				"inviter": "Ana Souza",
				"company": "Viva Imóveis",
				"role": "Sales User",
				"email": "bruno@example.com",
				"days": 3,
			},
		)
		self.assert_branded(html, link)
		for text in ("Ana Souza", "Viva Imóveis", "bruno@example.com", "3"):
			self.assertIn(text, html)

	def test_invitation_from_an_older_crm_still_renders(self):
		link = "https://acme.credtechdigital.com.br/api/method/crm.api.accept_invitation?key=abc"
		self.assert_branded(render("crm_invitation", {"title": "HubTech CRM", "invite_link": link}), link)

	def test_frappe_welcome_and_password_reset(self):
		link = "https://acme.credtechdigital.com.br/update-password?key=xyz"
		args = {"first_name": "Bruno", "user": "bruno@example.com", "link": link, "site_url": "https://acme.test"}
		self.assert_branded(render("new_user", args, "templates/emails/auth_email.html"), link)
		html = render("password_reset", {**args, "expiry_minutes": 1440}, "templates/emails/auth_email.html")
		self.assert_branded(html, link)
		self.assertIn("24", html)

	def test_welcome_notification_and_trial_reminder(self):
		user = frappe._dict(first_name="Bruno", email="bruno@example.com", language="pt-BR")
		body = frappe.render_template(site_setup.WELCOME_MESSAGE, {"doc": user})
		self.assert_branded(get_formatted_html("Assunto", body), frappe.utils.get_url("/crm"))
		html = render(
			"hubtech_trial_reminder",
			{
				"last_day": False,
				"end": "09/11/2026",
				"pause_on": "10/11/2026",
				"free": 1,
				"price": "R$ 60,00",
				"excess": 2,
				"plan_url": "https://acme.test/crm",
			},
		)
		self.assert_branded(html, "https://acme.test/crm")
		self.assertIn("R$ 60,00", html)

	def test_signup_emails(self):
		link = "https://hub.credtechdigital.com.br/cadastro/confirmar?token=t"
		args = {"name": "Ana", "company": "Acme", "address": "https://acme.credtechdigital.com.br", "link": link, "hours": 24}
		self.assert_branded(render("hubtech_signup_confirm", args), link)
		self.assert_branded(render("hubtech_signup_existing", args), args["address"])
		failed = {"company": "Acme", "site": "acme.x", "email": "ana@example.com", "link": "https://hub.test/app/x"}
		self.assert_branded(render("hubtech_signup_failed", failed), failed["link"])

	def test_any_plain_message_gets_the_layout(self):
		# alertas do Frappe (ex.: senha alterada) e notificações configuradas na tela
		self.assert_branded(get_formatted_html("Assunto", "<p>Sua senha foi alterada.</p>"))

	def test_old_names_are_renamed_once(self):
		account = frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": site_setup.OLD_EMAIL_ACCOUNT,
				"email_id": "no-reply@example.com",
				"enable_outgoing": 0,
				"enable_incoming": 0,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Notification",
				"subject": "x",
				"document_type": "User",
				"event": "New",
				"channel": "Email",
				"message": "x",
				"recipients": [{"receiver_by_document_field": "email"}],
			}
		).insert(ignore_permissions=True, set_name=site_setup.OLD_WELCOME)
		site_setup._rename_old_names()
		site_setup._rename_old_names()  # pode repetir
		self.assertTrue(frappe.db.exists("Email Account", {"email_account_name": site_setup.EMAIL_ACCOUNT}))
		self.assertFalse(frappe.db.exists("Email Account", {"email_account_name": site_setup.OLD_EMAIL_ACCOUNT}))
		self.assertTrue(frappe.db.exists("Notification", site_setup.WELCOME))
		self.assertFalse(frappe.db.exists("Notification", site_setup.OLD_WELCOME))
		self.assertTrue(account.name)
