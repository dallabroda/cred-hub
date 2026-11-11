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

	def __init__(self, payment_status="CONFIRMED", subscription_status="ACTIVE", upgrade_status=None, on_delete=None):
		self.calls = []
		self.payment_status = payment_status
		self.subscription_status = subscription_status
		# Status das cobranças avulsas (diferença de aumento); padrão: o mesmo dos outros pagamentos
		self.upgrade_status = upgrade_status
		self.upgrades = 0
		self.on_delete = on_delete

	def __call__(self, method, path, body=None, allow_missing=False):
		self.calls.append((method, path, body))
		if method == "DELETE" and self.on_delete:
			self.on_delete(path)
		if path == "/customers":
			return {"id": "cus_1"}
		if method == "POST" and path == "/subscriptions":
			return {"id": "sub_1", "nextDueDate": (body or {}).get("nextDueDate") or "2026-10-26"}
		if method == "POST" and path == "/payments":
			self.upgrades += 1
			pay_id = "pay_up" if self.upgrades == 1 else f"pay_up{self.upgrades}"
			return {"id": pay_id, "invoiceUrl": f"https://asaas.test/i/{pay_id}"}
		if path.endswith("/payments"):
			return {"data": [{"id": "pay_1", "invoiceUrl": "https://asaas.test/i/pay_1"}]}
		if path.startswith("/payments/pay_up"):
			pay_id = path.rsplit("/", 1)[1]
			status = self.upgrade_status or self.payment_status
			return {"id": pay_id, "status": status, "value": 30, "invoiceUrl": f"https://asaas.test/i/{pay_id}"}
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
		frappe.local.conf.credhub_billing = 1
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_billing", None))
		# apply_asaas_event grava (commit) no fim: no teste tudo fica na transação que o tearDown desfaz
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		# Convite manda e-mail na hora; aqui não há conta de envio
		mail = patch.object(frappe, "sendmail")
		self.mail = mail.start()
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
				"applied_upgrade_payment_id": None,
				"upgrade_base_users": 0,
				"upgrade_due_date": None,
				"scheduled_extra_users": 0,
				"scheduled_from": None,
				"grace_until": None,
				"payer_name": None,
				"payer_email": None,
				"next_due_date": None,
				# Teste grátis já acabou: os testes de cobrança valem para depois dele
				"trial_ends_on": self.days(-1),
			}
		)
		plan.save(ignore_permissions=True)

	def days(self, n):
		return frappe.utils.add_days(frappe.utils.getdate(), n)

	def paused_notice(self):
		"""Argumentos do último e-mail de usuários pausados (falha se não houve)."""
		calls = [c.kwargs for c in self.mail.call_args_list if c.kwargs.get("template") == "hubtech_users_paused"]
		self.assertTrue(calls, "nenhum e-mail de usuários pausados")
		return calls[-1]

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

	def test_switched_off_billing_has_no_limit_and_no_screen(self):
		frappe.local.conf.credhub_billing = 0
		for n in range(1, 5):
			self.user(n)  # sem limite
		self.assertEqual(billing.get_plan(), {"enabled": False})
		with patch.object(billing, "_asaas") as asaas:
			with self.assertRaises(frappe.ValidationError):
				billing.request_extra_users(1, PAYER)
			billing.daily_check()
			frappe.local.conf.credhub_signup = 1
			try:
				with patch.dict("os.environ", {"ASAAS_API_KEY": "x"}):
					self.assertEqual(billing.ensure_asaas_webhook(), "webhook do Asaas: pulado")
			finally:
				frappe.local.conf.pop("credhub_signup", None)
		asaas.assert_not_called()

	def test_one_free_user_then_blocked(self):
		self.user(1)
		with self.assertRaises(billing.UserLimitError):
			self.user(2)

	def test_pending_invitation_takes_a_seat(self):
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
			self.assertEqual(billing.user_limit(), 1)  # ainda não pagou
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		self.assertEqual(billing.user_limit(), 3)
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
		self.assertEqual(billing.user_limit(), 1)

	def test_overdue_has_grace_then_pauses_newest_and_payment_restores(self):
		self.active_plan(2)
		users = [self.user(n).name for n in range(1, 4)]
		frappe.get_doc("User", users[0]).add_roles("System Manager")
		with patch.object(billing, "_asaas", FakeAsaas(payment_status="OVERDUE")):
			billing.apply_asaas_event("PAYMENT_OVERDUE", "pay_1", "sub_1")
			billing.daily_check()
		self.assertEqual(billing.active_users(), users)  # tolerância: ninguém pausado ainda
		self.assertFalse([c for c in self.mail.call_args_list if c.kwargs.get("template") == "hubtech_users_paused"])
		frappe.db.set_single_value(billing.PLAN, "grace_until", frappe.utils.add_days(frappe.utils.getdate(), -1))
		with patch.object(billing, "_asaas", FakeAsaas(payment_status="OVERDUE")):
			billing.daily_check()
		self.assertEqual(billing.active_users(), users[:1])  # o mais antigo continua
		self.assertEqual(sorted(u.name for u in billing.get_plan()["paused_users"]), sorted(users[1:]))
		notice = self.paused_notice()
		self.assertEqual(notice["recipients"], users[:1])
		self.assertEqual(notice["args"]["reason"], "past_due")
		self.assertEqual(sorted(notice["args"]["users"]), ["Plano 2", "Plano 3"])
		self.assertEqual(notice["args"]["pay_url"], self.plan().invoice_url)
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		self.assertEqual(billing.active_users(), users)
		self.assertFalse(frappe.get_all("User", filters={"credhub_paused": 1}))

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
		frappe.db.set_single_value(billing.PLAN, "next_due_date", frappe.utils.add_days(frappe.utils.getdate(), 15))

	def plan(self):
		return frappe.get_single(billing.PLAN)

	# Aumentar

	def test_increase_releases_now_and_charges_prorated_difference(self):
		self.active_plan(1)
		fake = FakeAsaas(upgrade_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			result = billing.request_extra_users(3)  # sem dados de pagador: o cliente já existe
		self.assertEqual(result["invoice_url"], "https://asaas.test/i/pay_up")
		self.assertEqual(billing.user_limit(), 4)  # liberado na hora
		charge = next(body for method, path, body in fake.calls if method == "POST" and path == "/payments")
		self.assertTrue(billing.MIN_CHARGE <= charge["value"] < 2 * billing.PRICE_PER_USER)  # 2 extras, ~metade do mês
		self.assertEqual(charge["dueDate"], str(frappe.utils.add_days(frappe.utils.getdate(), 3)))
		self.assertFalse([c for c in fake.calls if c[0] == "PUT"])  # mensalidade sobe só quando pagar
		paid = FakeAsaas()
		with patch.object(billing, "_asaas", paid):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_up", None)
		raise_value = next(body for method, path, body in paid.calls if method == "PUT")
		self.assertEqual(raise_value["value"], 3 * billing.PRICE_PER_USER)
		self.assertEqual((self.plan().extra_users, self.plan().upgrade_payment_id), (3, None))

	def test_unpaid_increase_pauses_only_the_new_users_until_paid(self):
		self.active_plan(1)
		old = [self.user(n).name for n in range(1, 3)]  # 1 grátis + 1 extra pago
		frappe.get_doc("User", old[0]).add_roles("System Manager")
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="PENDING")):
			billing.request_extra_users(3)
		new = [self.user(n).name for n in range(3, 5)]
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="OVERDUE")):
			billing.apply_asaas_event("PAYMENT_OVERDUE", "pay_up", None)
		self.assertEqual(billing.active_users(), old)
		notice = self.paused_notice()
		self.assertEqual((notice["recipients"], notice["args"]["reason"]), (old[:1], "upgrade_unpaid"))
		# o link é o da cobrança do aumento, mesmo com outra fatura do plano em aberto
		self.assertEqual(notice["args"]["pay_url"], "https://asaas.test/i/pay_up")
		self.assertTrue(billing.get_plan()["upgrade"]["overdue"])
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_up", None)
		self.assertEqual(billing.active_users(), old + new)

	def test_daily_check_catches_an_unpaid_increase_without_webhook(self):
		self.active_plan(1)
		old = [self.user(n).name for n in range(1, 3)]
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="PENDING")):
			billing.request_extra_users(2)
		self.user(3)
		frappe.db.set_single_value(billing.PLAN, "upgrade_due_date", frappe.utils.add_days(frappe.utils.getdate(), -1))
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="OVERDUE")):
			billing.daily_check()
		self.assertEqual(billing.active_users(), old)

	def test_tiny_difference_is_not_charged_and_goes_to_next_invoice(self):
		self.active_plan(1)
		frappe.db.set_single_value(billing.PLAN, "next_due_date", frappe.utils.add_days(frappe.utils.getdate(), 1))
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			result = billing.request_extra_users(2)  # 1 extra por 1 dia: ~R$ 2, abaixo do mínimo
		self.assertEqual((result["invoice_url"], result["amount"]), (None, 0))
		self.assertFalse([c for c in fake.calls if c[0] == "POST" and c[1] == "/payments"])
		update = next(body for method, path, body in fake.calls if method == "PUT")
		self.assertEqual((update["value"], update["updatePendingPayments"]), (2 * billing.PRICE_PER_USER, True))
		self.assertEqual(billing.user_limit(), 3)

	def test_second_increase_cancels_the_first_charge(self):
		self.active_plan(1)
		fake = FakeAsaas(upgrade_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(3)
			result = billing.request_extra_users(4)
		self.assertIn(("DELETE", "/payments/pay_up", None), fake.calls)
		self.assertEqual(result["invoice_url"], "https://asaas.test/i/pay_up2")
		second = [body for method, path, body in fake.calls if method == "POST" and path == "/payments"][1]
		self.assertTrue(second["value"] < 3 * billing.PRICE_PER_USER)  # 3 a mais que o pago (1), não 3 + 2
		self.assertEqual((self.plan().upgrade_payment_id, self.plan().upgrade_base_users), ("pay_up2", 1))

	def test_increase_already_paid_is_not_cancelled(self):
		self.active_plan(1)
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="PENDING")):
			billing.request_extra_users(3)
		paid = FakeAsaas(upgrade_status="CONFIRMED")
		with patch.object(billing, "_asaas", paid):
			result = billing.request_extra_users(4)
		self.assertFalse(result["ok"])
		self.assertNotIn(("DELETE", "/payments/pay_up", None), paid.calls)

	def test_confirmed_then_received_applies_once(self):
		self.active_plan(1)
		fake = FakeAsaas(upgrade_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(3)
		paid = FakeAsaas()
		errors = frappe.db.count("Error Log", {"method": "Asaas: pagamento sem aumento pendente"})
		with patch.object(billing, "_asaas", paid):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_up", None)
			billing.apply_asaas_event("PAYMENT_RECEIVED", "pay_up", None)
		self.assertEqual(len([c for c in paid.calls if c[0] == "PUT"]), 1)
		self.assertEqual(frappe.db.count("Error Log", {"method": "Asaas: pagamento sem aumento pendente"}), errors)

	def test_renewal_during_an_open_increase_keeps_it_open(self):
		self.active_plan(1)
		with patch.object(billing, "_asaas", FakeAsaas(upgrade_status="PENDING")):
			billing.request_extra_users(3)
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")  # mensalidade do mês paga
		plan = self.plan()
		self.assertEqual((plan.extra_users, plan.upgrade_payment_id, plan.upgrade_base_users), (3, "pay_up", 1))

	# Diminuir

	def test_decrease_keeps_paid_seats_until_next_due_date(self):
		self.active_plan(3)
		users = [self.user(n).name for n in range(1, 5)]  # 1 grátis + 3 pagos, todos em uso
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			result = billing.request_extra_users(1)  # sem pedir para desativar ninguém
		self.assertTrue(result["ok"])
		update = next(body for method, path, body in fake.calls if method == "PUT")
		self.assertEqual((update["value"], update["updatePendingPayments"]), (billing.PRICE_PER_USER, True))
		self.assertEqual(billing.user_limit(), 4)  # vagas pagas continuam
		self.assertEqual(billing.get_plan()["scheduled_extras"], 1)
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()  # antes da virada: nada muda
		self.assertEqual(billing.active_users(), users)
		frappe.db.set_single_value(billing.PLAN, "scheduled_from", frappe.utils.getdate())
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()
		self.assertEqual(billing.user_limit(), 2)
		self.assertEqual(billing.active_users(), users[:2])  # pausa os mais recentes
		self.assertIsNone(billing.get_plan()["scheduled_extras"])

	def test_cancel_keeps_paid_seats_and_deletes_subscription_on_the_eve(self):
		self.active_plan(1)
		fake = FakeAsaas()
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(0)
		self.assertNotIn(("DELETE", "/subscriptions/sub_1", None), fake.calls)
		self.assertEqual(billing.user_limit(), 2)
		frappe.db.set_single_value(billing.PLAN, "scheduled_from", frappe.utils.add_days(frappe.utils.getdate(), 1))
		with patch.object(billing, "_asaas", fake):
			billing.daily_check()  # véspera: a assinatura sai, as vagas ficam até amanhã
		self.assertIn(("DELETE", "/subscriptions/sub_1", None), fake.calls)
		self.assertEqual(billing.user_limit(), 2)
		frappe.db.set_single_value(billing.PLAN, "scheduled_from", frappe.utils.getdate())
		with patch.object(billing, "_asaas", fake):
			billing.daily_check()
		self.assertEqual((self.plan().status, billing.user_limit()), ("Free", 1))

	def test_increase_after_a_scheduled_decrease_cancels_the_schedule(self):
		self.active_plan(2)
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1)
		fake = FakeAsaas(upgrade_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(2)
		restore = next(body for method, path, body in fake.calls if method == "PUT")
		self.assertEqual(restore["value"], 2 * billing.PRICE_PER_USER)  # a mensalidade volta ao que está pago
		self.assertFalse([c for c in fake.calls if c[0] == "POST" and c[1] == "/payments"])  # nada a cobrar
		self.assertIsNone(billing.get_plan()["scheduled_extras"])

	def test_decrease_cancels_a_pending_increase(self):
		self.active_plan(2)
		fake = FakeAsaas(upgrade_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(3)
			billing.request_extra_users(1)
		self.assertIn(("DELETE", "/payments/pay_up", None), fake.calls)
		plan = self.plan()
		self.assertEqual((plan.upgrade_payment_id, plan.extra_users, plan.scheduled_extra_users), (None, 2, 1))
		# A fatura antiga paga mesmo assim (corrida): não mexe na assinatura e fica no log para estorno
		late = FakeAsaas()
		errors = frappe.db.count("Error Log", {"method": "Asaas: pagamento sem aumento pendente"})
		with patch.object(billing, "_asaas", late):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_up", None)
		self.assertFalse([c for c in late.calls if c[0] == "PUT"])
		self.assertEqual(frappe.db.count("Error Log", {"method": "Asaas: pagamento sem aumento pendente"}), errors + 1)

	# Corrida com o webhook e outras empresas

	def test_old_subscription_is_forgotten_before_it_is_deleted(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1, PAYER)  # aguardando pagamento, assinatura sub_1
		seen = []

		def on_delete(path):
			if path.startswith("/subscriptions/"):
				seen.append(frappe.db.get_single_value(billing.PLAN, "asaas_subscription_id"))

		with patch.object(billing, "_asaas", FakeAsaas(on_delete=on_delete)):
			billing.request_extra_users(2)
		# Quando o DELETE saiu, o plano já não apontava para a assinatura: o SUBSCRIPTION_DELETED é ignorado
		self.assertEqual(len(seen), 1)
		self.assertFalse(seen[0])
		self.assertEqual(self.plan().asaas_subscription_id, "sub_1")

	def test_refused_delete_keeps_the_subscription(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1, PAYER)

		def refuse(path):
			if path.startswith("/subscriptions/"):
				raise frappe.ValidationError("recusado")

		with patch.object(billing, "_asaas", FakeAsaas(on_delete=refuse)), self.assertRaises(frappe.ValidationError):
			billing.request_extra_users(0)
		self.assertEqual(self.plan().asaas_subscription_id, "sub_1")

	def test_payment_of_another_subscription_is_ignored(self):
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.request_extra_users(1, PAYER)
		other = FakeAsaas()
		with patch.object(billing, "_asaas", lambda *a, **k: {**other(*a, **k), "subscription": "sub_outra"}):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_9", "sub_1")
		self.assertEqual(self.plan().status, "Awaiting Payment")

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

	# Teste grátis de 14 dias (regra do dono de 27/10/2026)

	def set_trial(self, last_day):
		frappe.db.set_single_value(billing.PLAN, "trial_ends_on", last_day)

	def test_trial_has_no_user_limit(self):
		self.set_trial(self.days(5))
		for n in range(1, 5):
			self.user(n)
		frappe.get_doc({"doctype": "CRM Invitation", "email": "convite@example.com", "role": "Sales User"}).insert(
			ignore_permissions=True
		)
		plan = billing.get_plan()
		self.assertEqual((plan["in_trial"], plan["trial_days_left"], plan["free"]), (True, 6, 1))
		self.assertEqual(plan["trial_ends_on"], self.days(5))

	def test_trial_end_pauses_the_newest_and_keeps_the_creator(self):
		self.set_trial(frappe.utils.getdate())
		users = [self.user(n).name for n in range(1, 4)]
		for name in users[::2]:
			frappe.get_doc("User", name).add_roles("System Manager")
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()  # último dia do teste: ninguém é pausado
		self.assertEqual(billing.active_users(), users)
		self.assertFalse([c for c in self.mail.call_args_list if c.kwargs.get("template") == "hubtech_users_paused"])
		self.set_trial(self.days(-1))
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()
		self.assertEqual(billing.active_users(), users[:1])
		notice = self.paused_notice()
		self.assertEqual(notice["recipients"], users[:1])  # o admin pausado não recebe
		self.assertEqual(notice["args"]["reason"], "trial")
		self.assertEqual(notice["args"]["users"], ["Plano 3", "Plano 2"])
		self.assertIsNone(notice["args"]["pay_url"])
		self.assertIn("2", notice["subject"])
		self.assertEqual(sorted(u.name for u in billing.get_plan()["paused_users"]), sorted(users[1:]))
		self.assertFalse(billing.get_plan()["in_trial"])

	def test_extras_contracted_in_trial_are_billed_after_it_and_keep_seats(self):
		self.set_trial(self.days(2))
		users = [self.user(n).name for n in range(1, 4)]
		fake = FakeAsaas(payment_status="PENDING")
		with patch.object(billing, "_asaas", fake):
			billing.request_extra_users(2, PAYER)
		subscription = next(body for method, path, body in fake.calls if method == "POST" and path == "/subscriptions")
		self.assertEqual(subscription["nextDueDate"], str(self.days(3)))  # nada cobrado durante o teste
		# O teste acabou ontem e a 1ª fatura vence hoje: as vagas contratadas valem durante a tolerância
		self.set_trial(self.days(-1))
		frappe.db.set_single_value(billing.PLAN, "next_due_date", frappe.utils.getdate())
		with patch.object(billing, "_asaas", fake):
			billing.daily_check()
		self.assertEqual(billing.user_limit(), 3)
		self.assertEqual(billing.active_users(), users)
		# A tolerância acabou sem pagamento: fica só o grátis; pagando, eles voltam
		self.set_trial(self.days(-5))
		frappe.db.set_single_value(billing.PLAN, "next_due_date", self.days(-4))
		with patch.object(billing, "_asaas", fake):
			billing.daily_check()
		self.assertEqual(billing.active_users(), users[:1])
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.apply_asaas_event("PAYMENT_CONFIRMED", "pay_1", "sub_1")
		self.assertEqual(billing.active_users(), users)

	def test_trial_end_is_set_once(self):
		frappe.db.set_single_value(billing.PLAN, "trial_ends_on", None)
		billing.ensure_trial()
		created = frappe.db.get_value("User", "Administrator", "creation")
		self.assertEqual(frappe.utils.getdate(self.plan().trial_ends_on), frappe.utils.add_days(frappe.utils.getdate(created), 13))
		self.set_trial(self.days(-30))
		billing.ensure_trial()  # rodar de novo (deploy) não recomeça o teste
		self.assertEqual(frappe.utils.getdate(self.plan().trial_ends_on), self.days(-30))

	def check(self, last_day):
		self.set_trial(last_day)
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()

	def test_trial_reminder_to_every_admin_3_days_before_and_on_the_last_day_once(self):
		self.set_trial(self.days(5))
		admins = [self.user(1), self.user(2)]
		for admin in admins:
			admin.add_roles("System Manager")
		self.user(3)
		self.check(self.days(5))  # fora dos marcos
		self.mail.assert_not_called()
		self.check(self.days(3))
		self.assertEqual(sorted(self.mail.call_args.kwargs["recipients"]), sorted(a.name for a in admins))
		self.assertEqual(self.mail.call_args.kwargs["template"], "hubtech_trial_reminder")
		self.assertIn(str(billing.PRICE_PER_USER), self.mail.call_args.kwargs["args"]["price"])
		self.check(self.days(3))  # mesmo marco de novo (outra rodada do agendador): não repete
		self.assertEqual(self.mail.call_count, 1)
		self.check(frappe.utils.getdate())  # último dia
		self.assertEqual(self.mail.call_count, 2)
		self.assertEqual(self.plan().trial_reminders_sent, "0,3")

	def test_no_trial_reminder_when_nobody_will_be_paused_or_when_switched_off(self):
		self.set_trial(self.days(5))
		self.user(1).add_roles("System Manager")
		self.check(self.days(3))  # 1 usuário cabe no grátis
		self.mail.assert_not_called()
		self.user(2)
		frappe.local.conf.credhub_trial_reminders = 0
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_trial_reminders", None))
		self.check(frappe.utils.getdate())
		self.mail.assert_not_called()
		frappe.local.conf.credhub_trial_reminders = 1
		with patch.object(billing, "_asaas", FakeAsaas(payment_status="PENDING")):
			billing.request_extra_users(1, PAYER)  # assinatura cobre os 2 ativos
		self.check(frappe.utils.getdate())
		self.mail.assert_not_called()

	def test_trial_end_expires_invitations_over_the_limit(self):
		self.set_trial(self.days(1))
		self.user(1)
		for email in ("convite1@example.com", "convite2@example.com"):
			frappe.get_doc({"doctype": "CRM Invitation", "email": email, "role": "Sales User"}).insert(
				ignore_permissions=True
			)
		self.check(self.days(-1))
		self.assertEqual(frappe.get_all("CRM Invitation", filters={"status": "Pending"}), [])
		self.assertEqual(frappe.db.count("CRM Invitation", {"status": "Expired", "email": ("like", "convite%")}), 2)

	def test_admin_swaps_who_stays_after_the_trial(self):
		self.set_trial(self.days(-1))
		frappe.local.conf.credhub_billing = 0  # usuários criados antes da cobrança ligar
		users = [self.user(n) for n in range(1, 3)]
		frappe.local.conf.credhub_billing = 1
		with patch.object(billing, "_asaas", FakeAsaas()):
			billing.daily_check()
		paused = frappe.get_doc("User", users[1].name)
		self.assertTrue(paused.credhub_paused)
		with self.assertRaises(billing.UserLimitError):
			paused.enabled = 1
			paused.save(ignore_permissions=True)  # sem vaga
		first = frappe.get_doc("User", users[0].name)
		first.enabled = 0
		first.save(ignore_permissions=True)
		paused.reload()
		paused.enabled = 1
		paused.save(ignore_permissions=True)
		self.assertEqual(billing.active_users(), [paused.name])
		self.assertFalse(frappe.db.get_value("User", paused.name, "credhub_paused"))
