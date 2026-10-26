"""Limite de usuários e cobrança pelo Asaas (credtech#25). Roda no bench: `bench --site <site> run-tests --app cred_hub`.
O Asaas é simulado (nenhuma chamada sai do servidor)."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub import billing

PAYER = {
	"name": "Ana Souza",
	"tax_id": "11.222.333/0001-81",
	"email": "ana@example.com",
	"phone": "(48) 99999-0000",
	"postal_code": "88000-000",
}


class FakeAsaas:
	"""Respostas mínimas da API do Asaas, guardando as chamadas."""

	def __init__(self, payment_status="CONFIRMED", subscription_status="ACTIVE"):
		self.calls = []
		self.payment_status = payment_status
		self.subscription_status = subscription_status

	def __call__(self, method, path, body=None, allow_missing=False):
		self.calls.append((method, path, body))
		if path == "/customers":
			return {"id": "cus_1"}
		if method == "POST" and path == "/subscriptions":
			return {"id": "sub_1", "nextDueDate": "2026-10-26"}
		if method == "POST" and path == "/payments":
			return {"id": "pay_up", "invoiceUrl": "https://asaas.test/i/pay_up"}
		if path.endswith("/payments"):
			return {"data": [{"id": "pay_1", "invoiceUrl": "https://asaas.test/i/pay_1"}]}
		if path == "/payments/pay_up":
			return {"id": "pay_up", "status": self.payment_status}
		if path.startswith("/payments/"):
			return {
				"id": "pay_1",
				"status": self.payment_status,
				"subscription": "sub_1",
				"invoiceUrl": "https://asaas.test/i/pay_1",
			}
		if method == "GET" and path.startswith("/subscriptions/"):
			return {"id": "sub_1", "status": self.subscription_status, "nextDueDate": "2026-11-26"}
		return {}


class TestBilling(IntegrationTestCase):
	def setUp(self):
		frappe.flags.credhub_enforce_user_limit = True
		# apply_asaas_event grava (commit) no fim: no teste tudo fica na transação que o tearDown desfaz
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		# Convite manda e-mail na hora; aqui não há conta de envio
		mail = patch.object(frappe, "sendmail")
		mail.start()
		self.addCleanup(mail.stop)
		frappe.set_user("Administrator")
		# Empresa vazia: só os usuários deste teste contam
		for name in billing.active_users():
			frappe.db.set_value("User", name, "enabled", 0, update_modified=False)
		frappe.db.delete("CRM Invitation", {"status": "Pending"})
		plan = frappe.get_single(billing.PLAN)
		plan.update(
			{
				"status": "Free",
				"extra_users": 0,
				"pending_extra_users": 0,
				"asaas_subscription_id": None,
				"asaas_customer_id": None,
				"upgrade_payment_id": None,
				"payer_name": None,
				"payer_email": None,
				"next_due_date": None,
			}
		)
		plan.save(ignore_permissions=True)

	def tearDown(self):
		frappe.flags.credhub_enforce_user_limit = False
		frappe.db.rollback()

	def user(self, n):
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": f"plano{n}-{frappe.generate_hash(length=6)}@example.com",
				"first_name": f"Plano {n}",
				"send_welcome_email": 0,
				"roles": [{"role": "Sales User"}],
			}
		).insert(ignore_permissions=True)

	def test_two_free_users_then_blocked(self):
		self.user(1)
		self.user(2)
		with self.assertRaises(billing.UserLimitError):
			self.user(3)

	def test_pending_invitation_takes_a_seat(self):
		self.user(1)
		frappe.get_doc({"doctype": "CRM Invitation", "email": "convite@example.com", "role": "Sales User"}).insert(
			ignore_permissions=True
		)
		with self.assertRaises(billing.UserLimitError):
			frappe.get_doc({"doctype": "CRM Invitation", "email": "outro@example.com", "role": "Sales User"}).insert(
				ignore_permissions=True
			)

	def test_payer_is_validated_per_field(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			result = billing.request_extra_users(1, {**PAYER, "tax_id": "123"})
		self.assertEqual((result["ok"], result["field"]), (False, "tax_id"))

	def test_contract_then_payment_releases_extra_users(self):
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			result = billing.request_extra_users(2, PAYER)
			self.assertEqual(result["invoice_url"], "https://asaas.test/i/pay_1")
			self.assertEqual(billing.user_limit(), 2)  # ainda não pagou
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		self.assertEqual(billing.user_limit(), 4)
		subscription = next(body for method, path, body in fake.calls if method == "POST" and path == "/subscriptions")
		self.assertEqual(subscription["value"], 120)
		self.assertEqual(subscription["billingType"], "CREDIT_CARD")
		self.assertEqual(subscription["externalReference"], frappe.local.site)

	def test_forged_event_is_ignored(self):
		with patch.object(billing, "_asaas", FakeAsaas(payment_status="PENDING")):
			billing.request_extra_users(1, PAYER)
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")  # a API diz que não foi pago
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_outra")  # não é a assinatura desta empresa
		self.assertEqual(frappe.get_single(billing.PLAN).status, "Awaiting Payment")
		self.assertEqual(billing.user_limit(), 2)

	def test_overdue_pauses_newest_and_payment_restores(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(2, PAYER)
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		users = [self.user(n).name for n in range(1, 5)]
		with patch.object(billing, "_asaas", FakeAsaas(payment_status="OVERDUE")):
			billing.apply_asaas_event("PAYMENT_OVERDUE", "pay_1", "sub_1")
		self.assertEqual(billing.active_users(), users[:2])  # os 2 mais antigos continuam
		self.assertEqual(sorted(u.name for u in billing.get_plan()["paused_users"]), sorted(users[2:]))
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		self.assertEqual(billing.active_users(), users)
		self.assertFalse(frappe.get_all("User", filters={"credhub_paused": 1}))

	def test_reduce_needs_deactivation_first(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1, PAYER)
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
			for n in range(1, 4):
				self.user(n)
			result = billing.request_extra_users(0)
		self.assertEqual((result["ok"], result["deactivate"]), (False, 1))

	def test_webhook_needs_token(self):
		frappe.local.conf.credhub_signup = 1
		try:
			with patch.dict("os.environ", {"CREDHUB_ASAAS_WEBHOOK_TOKEN": "segredo"}), patch.object(
				frappe, "get_request_header", return_value="errado"
			):
				with self.assertRaises(frappe.AuthenticationError):
					billing.asaas_webhook()
		finally:
			frappe.local.conf.pop("credhub_signup", None)

	def active_plan(self, extras=1):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(extras, PAYER)
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")

	def test_increase_charges_prorated_difference_before_releasing(self):
		self.active_plan(1)
		frappe.db.set_single_value(billing.PLAN, "next_due_date", frappe.utils.add_days(frappe.utils.getdate(), 15))
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			result = billing.request_extra_users(3)  # sem dados de pagador: o cliente já existe
			self.assertEqual(result["invoice_url"], "https://asaas.test/i/pay_up")
			self.assertEqual(billing.user_limit(), 3)  # ainda não pagou a diferença
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_up", None)
		self.assertEqual(billing.user_limit(), 5)
		charge = next(body for method, path, body in fake.calls if method == "POST" and path == "/payments")
		self.assertTrue(billing.MIN_CHARGE <= charge["value"] < 2 * billing.PRICE_PER_USER)  # 2 extras, ~metade do mês
		raise_value = next(body for method, path, body in fake.calls if method == "PUT")
		self.assertEqual(raise_value["value"], 3 * billing.PRICE_PER_USER)

	def test_decrease_changes_price_from_next_charge(self):
		self.active_plan(2)
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(1)
		update = next(body for method, path, body in fake.calls if method == "PUT")
		self.assertEqual((update["value"], update["updatePendingPayments"]), (billing.PRICE_PER_USER, False))
		self.assertEqual(billing.user_limit(), 3)

	def test_payment_of_another_subscription_is_ignored(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1, PAYER)
		other = FakeAsaas()
		with patch.object(billing, "_asaas", lambda *a, **k: {**other(*a, **k), "subscription": "sub_outra"}):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_9", "sub_1")
		self.assertEqual(frappe.get_single(billing.PLAN).status, "Awaiting Payment")

	def test_credtech_site_has_no_limit(self):
		frappe.local.conf.credhub_unlimited_users = 1
		try:
			for n in range(1, 5):
				self.user(n)
			self.assertTrue(billing.get_plan()["unlimited"])
		finally:
			frappe.local.conf.pop("credhub_unlimited_users", None)

	def test_payer_asked_only_once(self):
		self.assertTrue(billing.get_plan()["needs_payer"])
		self.active_plan(1)
		plan = billing.get_plan()
		self.assertFalse(plan["needs_payer"])
		self.assertEqual(plan["payer"]["email"], PAYER["email"])
