"""Limite de usuários e cobrança de usuários extras pelo Asaas (credtech#25).

Regras do dono (25, 26 e 27/10/2026): cada empresa tem 14 dias de teste com usuários ilimitados, contados da criação
da empresa e uma vez só; depois, 1 usuário grátis e, do 2º em diante, R$ 60 por usuário a mais por mês, no cartão,
pelo Asaas de produção (a mesma conta do Cred Chat). Padrão de mercado, e na dúvida não prejudicar o cliente: nunca cobrar duas vezes a mesma coisa, nunca tirar vaga já paga, toda falha de cobrança tem prazo de
tolerância antes de pausar, e pausar começa pelo usuário mais recente, sem apagar nada.

- O plano fica no doctype único "Cred Hub Plan" de cada empresa. Só administradores (System Manager) veem e mudam.
- Aumentar com a assinatura ativa: libera na hora e cobra a diferença proporcional por link, com 3 dias de prazo;
  sem pagamento, só os usuários novos são pausados até pagar. Diminuir: as vagas pagas valem até o próximo
  vencimento; a fatura seguinte vem com o valor menor e, na virada, quem passar do limite é pausado.
- Mensalidade vencida: 3 dias de tolerância; depois, pausa acima do grátis até pagar.
- Fim do teste: quem passar do limite é pausado no dia seguinte (daily_check), do mais recente para o mais antigo.
  Extras contratados durante o teste têm a 1ª fatura no dia seguinte ao fim do teste e valem até a tolerância dela.
  Aviso por e-mail aos administradores 3 dias antes e no último dia do teste, só com `credhub_trial_reminders`.
- Interruptor `credhub_billing` (common_site_config), desligado por padrão: desligado, não há limite, a tela
  "Usuários e plano" some, o Asaas não é chamado e o webhook dele não é cadastrado. Liga quando a tela estiver
  pronta e o dono mandar.
- A empresa da própria CredTech não tem limite (`credhub_unlimited_users` no site_config, gravado pelo deploy).
- Todo usuário de sistema conta, inclusive os de integração (decisão do dono).
- O cartão é digitado na página da fatura do Asaas (invoice_url), nunca no Cred Hub.
- O Asaas avisa por webhook no site central (asaas_webhook); o site central descobre de qual empresa é a assinatura
  (externalReference = endereço do site) e aplica o evento no site dela (apply_asaas_event), que confere o estado
  direto na API do Asaas antes de mudar qualquer coisa.
Variáveis de ambiente: ASAAS_API_KEY, ASAAS_ENVIRONMENT (production/sandbox), CREDHUB_ASAAS_WEBHOOK_TOKEN.
"""

import hmac
import json
import os
import re

import frappe
import requests
from frappe import _
from frappe.utils import add_days, cint, date_diff, format_date, getdate, now_datetime

from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits

FREE_USERS = 1
PRICE_PER_USER = 60
# Teste grátis com usuários ilimitados, contado da criação da empresa (o dia da criação é o 1º)
TRIAL_DAYS = 14
# Avisos do fim do teste: dias antes do último dia (0 = no último dia)
TRIAL_REMINDER_DAYS = (3, 0)
PLAN = "Cred Hub Plan"
WALLET = "Cred Hub Credit Wallet"
# Recarga mínima de créditos de bureaus Premium (mesmo piso do Asaas que MIN_CHARGE, decisão do dono 30/10/2026)
MIN_CREDIT_TOPUP = 10
UNLIMITED = 1_000_000
# Menor cobrança avulsa aceita pelo Asaas no cartão; diferença menor não é cobrada (entra na próxima fatura)
MIN_CHARGE = 5
# Prazo para pagar a diferença de um aumento (os usuários novos já entram na hora)
UPGRADE_DUE_DAYS = 3
# Tolerância depois de uma mensalidade vencida antes de pausar alguém
GRACE_DAYS = 3
# Por que alguém foi pausado (texto do e-mail aos administradores, templates/emails/hubtech_users_paused.html)
PAUSE_REASONS = ("trial", "past_due", "upgrade_unpaid", "downgrade", "canceled")
HIDDEN_USERS = ("Administrator", "Guest")
CONFIRMED = ("CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH")
WEBHOOK_EVENTS = [
	"PAYMENT_CONFIRMED",
	"PAYMENT_RECEIVED",
	"PAYMENT_OVERDUE",
	"SUBSCRIPTION_DELETED",
	"SUBSCRIPTION_INACTIVATED",
]
# Avisos de falha de entrega dos webhooks do Asaas (contato de segurança da CredTech)
WEBHOOK_ALERT_EMAIL = "eduardo_dallabrida@credtechdigital.com.br"


class UserLimitError(frappe.ValidationError):
	"""Sem vaga no plano para mais um usuário (exc_type na resposta da API)."""


class InsufficientCreditsError(frappe.ValidationError):
	"""Saldo de créditos insuficiente pra pagar um bureau Premium (exc_type na resposta da API)."""


# Plano e uso


def _plan():
	return frappe.get_single(PLAN)


def _wallet():
	return frappe.get_single(WALLET)


def billing_enabled():
	return bool(cint(frappe.conf.get("credhub_billing")))


def trial_ends_on(plan=None):
	"""Último dia do teste grátis. Gravado uma vez no plano (ensure_trial); antes disso, vem da criação do site."""
	plan = plan or _plan()
	if plan.trial_ends_on:
		return getdate(plan.trial_ends_on)
	return _trial_end_from_site_creation()


def _trial_end_from_site_creation():
	created = frappe.db.get_value("User", "Administrator", "creation") or now_datetime()
	return add_days(getdate(created), TRIAL_DAYS - 1)


def in_trial(plan=None):
	return getdate() <= trial_ends_on(plan)


def ensure_trial():
	"""site_setup (deploy e cadastro de empresa nova): grava o fim do teste uma única vez; nunca recomeça."""
	plan = _plan()
	if not plan.trial_ends_on:
		_save_plan(plan, trial_ends_on=_trial_end_from_site_creation())
	return f"teste grátis até {plan.trial_ends_on}"


def _trial_contract(plan):
	"""Extras contratados durante o teste, com a 1ª fatura (no dia seguinte ao fim do teste) ainda não paga."""
	return bool(cint(plan.contracted_in_trial) and plan.next_due_date and cint(plan.pending_extra_users))


def user_limit(plan=None):
	if not billing_enabled() or frappe.conf.get("credhub_unlimited_users"):
		return UNLIMITED
	plan = plan or _plan()
	if in_trial(plan):
		return UNLIMITED
	# Extras valem com a assinatura em dia e, vencida, durante a tolerância
	in_grace = plan.status == "Past Due" and plan.grace_until and getdate(plan.grace_until) >= getdate()
	extras = cint(plan.extra_users) if plan.status == "Active" or in_grace else 0
	if _trial_contract(plan) and (
		in_grace
		or (plan.status == "Awaiting Payment" and getdate() <= getdate(add_days(plan.next_due_date, GRACE_DAYS)))
	):
		# Contratados no teste: valem até a tolerância da 1ª fatura, para ninguém ser pausado na virada do teste
		extras = cint(plan.pending_extra_users)
	return FREE_USERS + extras


def active_users(exclude=None):
	# Usuários técnicos das chaves da API não ocupam vaga (credtech#175)
	filters = {
		"enabled": 1,
		"user_type": "System User",
		"name": ("not in", (*HIDDEN_USERS, exclude or "")),
		"credhub_api_key": ("is", "not set"),
	}
	return frappe.get_all("User", filters=filters, pluck="name", order_by="creation asc")


def pending_invitations():
	emails = frappe.get_all("CRM Invitation", filters={"status": "Pending"}, pluck="email")
	enabled = set(active_users())
	return [email for email in set(emails) if email not in enabled]


def _limit_message(limit):
	return _("Your plan allows {0} users. Contract extra users in Users and Plan.").format(limit)


def _enforced():
	# Testes de outros apps criam muitos usuários: o limite só vale neles quando o teste pede
	return not frappe.flags.in_test or frappe.flags.credhub_enforce_user_limit


def validate_user(doc, method=None):
	"""doc_event User.validate: criar, reativar ou promover um usuário de sistema precisa de vaga no plano."""
	if not _enforced() or doc.name in HIDDEN_USERS or frappe.flags.credhub_billing_sync or doc.get("credhub_api_key"):
		return
	if not doc.enabled or doc.user_type != "System User":
		return
	if not (doc.is_new() or doc.has_value_changed("enabled") or doc.has_value_changed("user_type")):
		return
	limit = user_limit()
	if len(active_users(exclude=doc.name)) >= limit:
		frappe.throw(_limit_message(limit), UserLimitError)
	# O admin trocou quem fica (desativou outro e reativou este): deixa de constar como pausado
	doc.credhub_paused = 0


def validate_invitation(doc, method=None):
	"""doc_event CRM Invitation.before_insert: convite pendente também ocupa vaga."""
	if not _enforced() or doc.email in active_users():
		return
	limit = user_limit()
	if len(active_users()) + len(pending_invitations()) >= limit:
		frappe.throw(_limit_message(limit), UserLimitError)


@frappe.whitelist(methods=["GET"])
def get_plan():
	frappe.only_for("System Manager")
	if not billing_enabled():
		# Cobrança desligada: a tela não aparece
		return {"enabled": False}
	plan = _plan()
	paused = frappe.get_all(
		"User", filters={"credhub_paused": 1}, fields=["name", "full_name"], order_by="creation asc"
	)
	trial_end = trial_ends_on(plan)
	return {
		"limit": user_limit(plan),
		"free": FREE_USERS,
		# Teste grátis com usuários ilimitados até trial_ends_on (inclusive)
		"in_trial": in_trial(plan),
		"trial_ends_on": trial_end,
		"trial_days_left": max(date_diff(trial_end, getdate()) + 1, 0),
		"extras": cint(plan.extra_users),
		# 1ª assinatura: extras que entram quando o 1º pagamento for confirmado
		"pending_extras": cint(plan.pending_extra_users) if plan.status == "Awaiting Payment" else 0,
		# Diminuição agendada: a quantidade nova vale a partir de scheduled_from (vencimento)
		"scheduled_extras": cint(plan.scheduled_extra_users) if plan.scheduled_from else None,
		"scheduled_from": plan.scheduled_from,
		# Aumento já liberado, com a diferença a pagar até due_date (overdue: os novos estão pausados)
		"upgrade": {
			"extras": cint(plan.pending_extra_users),
			"paid_extras": cint(plan.upgrade_base_users),
			"due_date": plan.upgrade_due_date,
			"overdue": cint(plan.extra_users) < cint(plan.pending_extra_users),
			"invoice_url": plan.invoice_url,
		}
		if plan.upgrade_payment_id
		else None,
		"grace_until": plan.grace_until if plan.status == "Past Due" else None,
		"used_active": len(active_users()),
		"used_invited": len(pending_invitations()),
		"status": plan.status,
		"next_due": plan.next_due_date,
		"amount": plan.monthly_amount or 0,
		"price_per_user": PRICE_PER_USER,
		"currency": "BRL",
		"invoice_url": plan.invoice_url,
		"paused_users": paused,
		"can_manage": True,
		"enabled": True,
		"unlimited": bool(frappe.conf.get("credhub_unlimited_users")),
		# Sem cliente no Asaas a tela pede os dados de quem paga; depois disso, só mostra o resumo
		"needs_payer": not plan.asaas_customer_id,
		"payer": {"name": plan.payer_name, "email": plan.payer_email} if plan.asaas_customer_id else None,
	}


# Contratar, mudar ou cancelar extras


def _field_error(field, message):
	return {"ok": False, "field": field, "message": message}


def _is_valid_cpf(digits):
	if len(digits) != 11 or digits == digits[0] * 11:
		return False
	for size in (9, 10):
		total = sum(int(d) * (size + 1 - i) for i, d in enumerate(digits[:size]))
		if digits[size] != str((total * 10 % 11) % 10):
			return False
	return True


def _clean_payer(payer):
	payer = frappe.parse_json(payer) if isinstance(payer, str) else (payer or {})
	clean = {
		"name": (payer.get("name") or "").strip(),
		"tax_id": only_digits(payer.get("tax_id")),
		"email": (payer.get("email") or "").strip().lower(),
		"phone": only_digits(payer.get("phone")),
		"postal_code": only_digits(payer.get("postal_code")),
	}
	if len(clean["name"]) < 3:
		return None, _field_error("name", _("Enter the name of who pays."))
	tax_id = clean["tax_id"]
	if not (_is_valid_cpf(tax_id) if len(tax_id) == 11 else is_valid_cnpj(tax_id)):
		return None, _field_error("tax_id", _("Enter a valid CPF or CNPJ."))
	if not frappe.utils.validate_email_address(clean["email"]):
		return None, _field_error("email", _("Enter a valid e-mail address."))
	if len(clean["phone"]) not in (10, 11):
		return None, _field_error("phone", _("Enter a phone number with area code."))
	if len(clean["postal_code"]) != 8:
		return None, _field_error("postal_code", _("Enter a valid CEP."))
	return clean, None


@frappe.whitelist(methods=["POST"])
def request_extra_users(quantity: int, payer=None):
	"""Quantos usuários extras a empresa quer (0 = só os grátis). Devolve {ok, invoice_url, status} ou {ok: False, ...}."""
	frappe.only_for("System Manager")
	if not billing_enabled():
		frappe.throw(_("Billing is not available right now. Please try again later."))
	quantity = cint(quantity)
	if quantity < 0 or quantity > 500:
		return _field_error("quantity", _("Choose between 0 and 500 extra users."))
	plan = _plan()
	if plan.status == "Active" and plan.asaas_subscription_id:
		return _change_active_plan(plan, quantity)

	if quantity == 0:
		# Sem assinatura paga não há vaga paga a preservar: volta a só os grátis
		_forget_subscription(plan)
		_save_plan(plan, status="Free", monthly_amount=0, invoice_url=None, next_due_date=None, grace_until=None, **_cleared())
		pause_over_limit("downgrade")
		return {"ok": True, "invoice_url": None, "status": "Free"}

	clean = None
	if not plan.asaas_customer_id:
		clean, error = _clean_payer(payer)
		if error:
			return error
	# Assinatura antiga sem pagamento (aguardando, em atraso ou cancelada): começa de novo
	_forget_subscription(plan)
	customer = plan.asaas_customer_id or _create_customer(clean)
	value = PRICE_PER_USER * quantity
	subscription = _asaas(
		"POST",
		"/subscriptions",
		{
			"customer": customer,
			"billingType": "CREDIT_CARD",
			"value": value,
			# Contratado no teste, nada é cobrado antes de ele acabar
			"nextDueDate": str(add_days(trial_ends_on(plan), 1) if in_trial(plan) else getdate()),
			"cycle": "MONTHLY",
			"description": _description(quantity),
			"externalReference": frappe.local.site,
		},
	)
	payments = _asaas("GET", f"/subscriptions/{subscription['id']}/payments").get("data") or []
	invoice_url = payments[0].get("invoiceUrl") if payments else None
	_save_plan(
		plan,
		status="Awaiting Payment",
		contracted_in_trial=in_trial(plan),
		monthly_amount=value,
		asaas_customer_id=customer,
		payer_name=clean["name"] if clean else plan.payer_name,
		payer_email=clean["email"] if clean else plan.payer_email,
		asaas_subscription_id=subscription["id"],
		invoice_url=invoice_url,
		next_due_date=subscription.get("nextDueDate"),
		grace_until=None,
		# A 1ª assinatura libera os extras quando o 1º pagamento é confirmado
		**{**_cleared(), "pending_extra_users": quantity},
	)
	return {"ok": True, "invoice_url": invoice_url, "status": "Awaiting Payment"}


def _description(quantity):
	return f"HubTech CRM - {quantity} usuário(s) a mais - {frappe.local.site}"


def _cleared():
	"""Campos de aumento e diminuição em andamento, zerados."""
	return {
		"pending_extra_users": 0,
		"upgrade_payment_id": None,
		"upgrade_base_users": 0,
		"upgrade_due_date": None,
		"scheduled_extra_users": 0,
		"scheduled_from": None,
	}


def _change_active_plan(plan, quantity):
	"""Assinatura ativa. Padrão de mercado, sem prejudicar o cliente (decisão do dono, 26/10/2026):
	- aumentar libera na hora e cobra a diferença proporcional por link, com 3 dias de prazo (a conta do Asaas não
	  guarda o cartão para cobrar sozinha); sem pagamento no prazo, só os usuários novos são pausados até pagar;
	- diminuir: as vagas já pagas continuam até o próximo vencimento; a fatura seguinte já vem com o valor menor e,
	  na virada, quem passar do limite novo é pausado, do mais recente para o mais antigo."""
	error = _drop_upgrade_payment(plan)
	if error:
		return error
	paid = cint(plan.extra_users)
	if plan.scheduled_from:
		# Mudou de ideia antes da virada: a mensalidade volta ao que está pago e o agendamento sai
		_asaas(
			"PUT",
			f"/subscriptions/{plan.asaas_subscription_id}",
			{"value": PRICE_PER_USER * paid, "description": _description(paid), "updatePendingPayments": True},
		)
		_save_plan(plan, scheduled_extra_users=0, scheduled_from=None, monthly_amount=PRICE_PER_USER * paid)

	if quantity == paid:
		return {"ok": True, "invoice_url": None, "status": "Active"}

	if quantity < paid:
		if quantity == 0:
			# A assinatura é apagada na véspera do vencimento (daily_check): até lá as vagas pagas continuam
			_save_plan(plan, scheduled_extra_users=0, scheduled_from=plan.next_due_date, monthly_amount=0)
		else:
			_asaas(
				"PUT",
				f"/subscriptions/{plan.asaas_subscription_id}",
				{"value": PRICE_PER_USER * quantity, "description": _description(quantity), "updatePendingPayments": True},
			)
			_save_plan(
				plan,
				scheduled_extra_users=quantity,
				scheduled_from=plan.next_due_date,
				monthly_amount=PRICE_PER_USER * quantity,
			)
		return {"ok": True, "invoice_url": None, "status": "Active", "scheduled_from": plan.next_due_date}

	amount = _prorated(quantity - paid, plan.next_due_date)
	if amount < MIN_CHARGE:
		# Diferença abaixo do mínimo do Asaas: não cobra avulso; libera e o valor novo entra na próxima fatura
		_asaas(
			"PUT",
			f"/subscriptions/{plan.asaas_subscription_id}",
			{"value": PRICE_PER_USER * quantity, "description": _description(quantity), "updatePendingPayments": True},
		)
		_save_plan(plan, extra_users=quantity, monthly_amount=PRICE_PER_USER * quantity)
		restore_paused()
		return {"ok": True, "invoice_url": None, "status": "Active", "amount": 0}
	due = add_days(getdate(), UPGRADE_DUE_DAYS)
	payment = _asaas(
		"POST",
		"/payments",
		{
			"customer": plan.asaas_customer_id,
			"billingType": "CREDIT_CARD",
			"value": amount,
			"dueDate": str(due),
			"description": f"HubTech CRM - {quantity - paid} usuário(s) a mais até o próximo vencimento - {frappe.local.site}",
			"externalReference": frappe.local.site,
		},
	)
	_save_plan(
		plan,
		extra_users=quantity,
		pending_extra_users=quantity,
		upgrade_base_users=paid,
		upgrade_payment_id=payment["id"],
		upgrade_due_date=due,
		invoice_url=payment.get("invoiceUrl"),
	)
	restore_paused()
	return {"ok": True, "invoice_url": payment.get("invoiceUrl"), "status": "Active", "amount": amount}


def _drop_upgrade_payment(plan):
	"""Cancela no Asaas a cobrança da diferença de um aumento que ainda não foi paga (o admin pediu outro aumento ou
	diminuiu): as vagas desse aumento saem junto, as pagas ficam. Se ela acabou de ser paga, não mexe: o webhook
	aplica e o admin tenta de novo depois (nunca cobrar duas vezes)."""
	if not plan.upgrade_payment_id:
		return None
	payment = _asaas("GET", f"/payments/{plan.upgrade_payment_id}", allow_missing=True)
	if payment and payment.get("status") in CONFIRMED:
		return {
			"ok": False,
			"field": "quantity",
			"message": _("The previous payment was just confirmed. Wait a minute and try again."),
		}
	if payment and not payment.get("deleted"):
		# Pago no meio do caminho, o Asaas recusa o DELETE (erro na tela) e o webhook aplica o pagamento
		_asaas("DELETE", f"/payments/{plan.upgrade_payment_id}", allow_missing=True)
	_save_plan(
		plan,
		extra_users=cint(plan.upgrade_base_users),
		pending_extra_users=0,
		upgrade_payment_id=None,
		upgrade_base_users=0,
		upgrade_due_date=None,
		invoice_url=None,
	)
	return None


def _forget_subscription(plan):
	"""Apaga a assinatura antiga sem corrida com o webhook: o plano deixa de apontar para ela (commit) antes do
	DELETE no Asaas, então o SUBSCRIPTION_DELETED que chegar é ignorado. Se o Asaas recusar, o plano volta a
	apontar para ela (a assinatura continua cobrando, então o plano não pode fingir que acabou)."""
	old = plan.asaas_subscription_id
	if not old:
		return
	_save_plan(plan, asaas_subscription_id=None)
	frappe.db.commit()
	try:
		_asaas("DELETE", f"/subscriptions/{old}", allow_missing=True)
	except Exception:
		plan.reload()
		_save_plan(plan, asaas_subscription_id=old)
		frappe.db.commit()
		raise


def _prorated(extra, next_due_date):
	"""Diferença proporcional aos dias que faltam até o próximo vencimento (ciclo mensal)."""
	from frappe.utils import add_months, date_diff

	today = getdate()
	due = getdate(next_due_date) if next_due_date else add_months(today, 1)
	cycle_start = getdate(add_months(due, -1))
	days_in_cycle = max(date_diff(due, cycle_start), 1)
	days_left = min(max(date_diff(due, today), 0), days_in_cycle)
	return round(PRICE_PER_USER * extra * days_left / days_in_cycle, 2)


def _create_customer(payer):
	customer = _asaas(
		"POST",
		"/customers",
		{
			"name": payer["name"],
			"cpfCnpj": payer["tax_id"],
			"email": payer["email"],
			"mobilePhone": payer["phone"],
			"postalCode": payer["postal_code"],
			"externalReference": frappe.local.site,
			"notificationDisabled": False,
		},
	)
	return customer["id"]


def _save_plan(plan, **values):
	plan.update(values)
	plan.flags.ignore_permissions = True
	plan.save()


# Créditos de bureaus Premium (Boa Vista, Dossiê QUOD, SCR Bacen — credtech#302)
#
# Diferente do plano de usuários: aqui a CredTech paga a DirectD na hora (conta própria da
# plataforma, não a de cada cliente) e revende com margem fixa. Por isso precisa de saldo
# pré-pago: sem créditos, o bureau Premium não é chamado (nunca deixa a CredTech pagar sem
# receber). Reusa os mesmos helpers de Asaas do plano de usuários (_asaas, _create_customer,
# _clean_payer) — mesma conta, outro tipo de cobrança (pagamento avulso, não assinatura).


@frappe.whitelist(methods=["GET"])
def get_credit_wallet():
	"""Saldo atual e recargas recentes, para a tela de Análise de Crédito da Organização."""
	frappe.only_for(["System Manager", "Sales Manager"])
	wallet = _wallet()
	recent_topups = frappe.get_all(
		"Cred Hub Credit Topup",
		fields=["name", "amount", "status", "invoice_url", "requested_on"],
		order_by="creation desc",
		limit_page_length=10,
	)
	return {
		"balance": wallet.balance or 0,
		"total_topped_up": wallet.total_topped_up or 0,
		"total_spent": wallet.total_spent or 0,
		"needs_payer": not wallet.asaas_customer_id,
		"payer": {"name": wallet.payer_name, "email": wallet.payer_email} if wallet.asaas_customer_id else None,
		"recent_topups": recent_topups,
		"min_topup": MIN_CREDIT_TOPUP,
	}


@frappe.whitelist(methods=["POST"])
def request_credit_topup(amount, payer=None):
	"""Pede uma recarga de créditos: cria um pagamento avulso no Asaas e devolve o link da fatura.
	O saldo só entra quando o webhook confirmar o pagamento (apply_asaas_event)."""
	frappe.only_for(["System Manager", "Sales Manager"])
	if not billing_enabled():
		frappe.throw(_("Billing is not available right now. Please try again later."))
	amount = float(amount or 0)
	if amount < MIN_CREDIT_TOPUP:
		return _field_error("amount", _("Minimum top-up is R$ {0}.").format(MIN_CREDIT_TOPUP))

	wallet = _wallet()
	clean = None
	if not wallet.asaas_customer_id:
		# Reusa o cliente do plano de usuários se já existir (mesma empresa, mesmo pagador)
		plan = _plan()
		if plan.asaas_customer_id:
			wallet.asaas_customer_id = plan.asaas_customer_id
			wallet.payer_name = plan.payer_name
			wallet.payer_email = plan.payer_email
		else:
			clean, error = _clean_payer(payer)
			if error:
				return error
			wallet.asaas_customer_id = _create_customer(clean)
			wallet.payer_name = clean["name"]
			wallet.payer_email = clean["email"]
		wallet.flags.ignore_permissions = True
		wallet.save()

	due = add_days(getdate(), 3)
	payment = _asaas(
		"POST",
		"/payments",
		{
			"customer": wallet.asaas_customer_id,
			"billingType": "CREDIT_CARD",
			"value": amount,
			"dueDate": str(due),
			"description": f"HubTech CRM - Créditos de bureaus Premium (R$ {amount:.2f}) - {frappe.local.site}",
			"externalReference": frappe.local.site,
		},
	)
	frappe.get_doc({
		"doctype": "Cred Hub Credit Topup",
		"amount": amount,
		"status": "Pending",
		"asaas_payment_id": payment["id"],
		"invoice_url": payment.get("invoiceUrl"),
		"requested_by": frappe.session.user,
		"requested_on": now_datetime(),
	}).insert(ignore_permissions=True)
	return {"ok": True, "invoice_url": payment.get("invoiceUrl")}


def _apply_credit_topup(event, payment_id):
	"""Confirmação de uma recarga: credita a carteira. Chamado de apply_asaas_event quando o
	payment_id bate com um 'Cred Hub Credit Topup' pendente (nunca com o de um aumento de plano)."""
	if event not in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
		return False
	topup_name = frappe.db.get_value(
		"Cred Hub Credit Topup", {"asaas_payment_id": payment_id, "status": "Pending"}
	)
	if not topup_name:
		return False
	payment = _asaas("GET", f"/payments/{payment_id}")
	if payment.get("status") not in CONFIRMED:
		return False
	topup = frappe.get_doc("Cred Hub Credit Topup", topup_name)
	topup.status = "Paid"
	topup.flags.ignore_permissions = True
	topup.save()
	wallet = _wallet()
	wallet.balance = (wallet.balance or 0) + topup.amount
	wallet.total_topped_up = (wallet.total_topped_up or 0) + topup.amount
	wallet.flags.ignore_permissions = True
	wallet.save()
	frappe.db.commit()
	return True


def wallet_balance() -> float:
	"""Saldo atual da carteira de créditos (consulta rápida, sem debitar)."""
	return _wallet().balance or 0


def spend_credits(amount: float, description: str = "") -> None:
	"""Debita `amount` da carteira pra pagar um bureau Premium. Levanta InsufficientCreditsError
	sem debitar nada se o saldo não cobrir — nunca deixa a carteira negativa."""
	wallet = _wallet()
	balance = wallet.balance or 0
	if balance < amount:
		frappe.throw(
			_("Insufficient credits: balance is R$ {0}, this costs R$ {1}. Add credits to continue.").format(
				f"{balance:.2f}", f"{amount:.2f}"
			),
			InsufficientCreditsError,
		)
	wallet.balance = balance - amount
	wallet.total_spent = (wallet.total_spent or 0) + amount
	wallet.flags.ignore_permissions = True
	wallet.save()


# Pausar e reativar


def pause_over_limit(reason, pay_url=None):
	"""Pausa os usuários acima do limite, dos mais recentes para os mais antigos (o criador da empresa fica), e avisa
	os administradores do motivo (um de PAUSE_REASONS) e de como reativar. `pay_url`: a fatura que, paga, devolve os
	pausados (sem ela, no atraso da mensalidade vale a fatura em aberto do plano)."""
	users = active_users()
	excess = len(users) - user_limit()
	paused = list(reversed(users[-excess:] if excess > 0 else []))
	for name in paused:
		_set_user(name, enabled=0, paused=1)
	invitations = _expire_invitations_over_limit()
	if paused:
		_paused_notice(reason, paused, invitations, pay_url)


def _paused_notice(reason, paused, invitations, pay_url=None):
	"""E-mail aos administradores ativos quando alguém é pausado (decisão do dono, 27/10/2026)."""
	plan = _plan()
	admins = set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"))
	recipients = [name for name in active_users() if name in admins]
	if not recipients:
		return
	names = [frappe.db.get_value("User", name, "full_name") or name for name in paused]
	# Fatura em aberto: pagar devolve os pausados (restore_paused na confirmação do Asaas)
	if reason == "past_due":
		pay_url = pay_url or plan.invoice_url
	frappe.sendmail(
		recipients=recipients,
		subject=_("HubTech CRM: 1 user was paused")
		if len(names) == 1
		else _("HubTech CRM: {0} users were paused").format(len(names)),
		template="hubtech_users_paused",
		args={
			"reason": reason,
			"users": names,
			"invitations": invitations,
			"free": FREE_USERS,
			"price": frappe.utils.fmt_money(PRICE_PER_USER, currency="BRL"),
			"grace_days": GRACE_DAYS,
			"pay_url": pay_url,
			"plan_url": frappe.utils.get_url("/crm"),
		},
	)


def _expire_invitations_over_limit():
	"""Convites pendentes acima do limite deixam de valer (os mais recentes primeiro); o admin convida de novo depois.
	Devolve quantos deixaram de valer."""
	enabled = set(active_users())
	pending = [
		invitation.name
		for invitation in frappe.get_all(
			"CRM Invitation", filters={"status": "Pending"}, fields=["name", "email"], order_by="creation asc"
		)
		if invitation.email not in enabled
	]
	seats = max(user_limit() - len(enabled), 0)
	for name in pending[seats:]:
		frappe.db.set_value("CRM Invitation", name, "status", "Expired")
	return len(pending[seats:])


def restore_paused():
	limit = user_limit()
	paused = frappe.get_all("User", filters={"credhub_paused": 1}, pluck="name", order_by="creation asc")
	for name in paused:
		if len(active_users()) >= limit:
			break
		_set_user(name, enabled=1, paused=0)


def _set_user(name, enabled, paused):
	user = frappe.get_doc("User", name)
	user.enabled = enabled
	user.credhub_paused = paused
	frappe.flags.credhub_billing_sync = True
	try:
		user.save(ignore_permissions=True)
	finally:
		frappe.flags.credhub_billing_sync = False


# Asaas


def _asaas_base_url():
	if (os.environ.get("ASAAS_ENVIRONMENT") or "sandbox").lower() == "production":
		return "https://api.asaas.com/v3"
	return "https://sandbox.asaas.com/api/v3"


def _asaas(method, path, body=None, allow_missing=False):
	key = os.environ.get("ASAAS_API_KEY")
	if not key:
		frappe.throw(_("Billing is not available right now. Please try again later."))
	response = requests.request(
		method,
		_asaas_base_url() + path,
		json=body,
		headers={"access_token": key, "Content-Type": "application/json", "User-Agent": "Cred-Hub"},
		timeout=20,
	)
	if allow_missing and response.status_code == 404:
		return {}
	if response.status_code >= 400:
		try:
			detail = ", ".join(e.get("description", "") for e in response.json().get("errors", []))
		except ValueError:
			detail = response.text[:300]
		frappe.log_error(title="Asaas", message=f"{method} {path} -> {response.status_code}: {detail}")
		frappe.throw(_("The payment service refused the request: {0}").format(detail or response.status_code))
	return response.json() if response.content else {}


# Webhook (site central) e aplicação do evento (site da empresa)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def asaas_webhook():
	if not frappe.conf.get("credhub_signup"):
		raise frappe.PermissionError
	token = os.environ.get("CREDHUB_ASAAS_WEBHOOK_TOKEN") or ""
	received = frappe.get_request_header("asaas-access-token") or ""
	if not token or not hmac.compare_digest(token, received):
		raise frappe.AuthenticationError
	if not billing_enabled():
		return {"ok": True}
	payload = frappe.request.get_json(silent=True) or {}
	event = payload.get("event") or ""
	if event not in WEBHOOK_EVENTS:
		return {"ok": True}
	payment = payload.get("payment") or {}
	subscription_id = payment.get("subscription") or (payload.get("subscription") or {}).get("id")
	if not subscription_id and not payment.get("id"):
		return {"ok": True}
	frappe.enqueue(
		"cred_hub.billing.dispatch_event",
		queue="long",
		event=event,
		payment_id=payment.get("id"),
		subscription_id=subscription_id,
	)
	return {"ok": True}


def dispatch_event(event, payment_id, subscription_id):
	"""No site central: acha a empresa dona da assinatura (externalReference) e aplica o evento no site dela.
	Assinaturas que não são do Cred Hub (ex.: do Cred Chat, na mesma conta do Asaas) são ignoradas."""
	from cred_hub.signup import _bench, _site_exists

	if subscription_id:
		source = _asaas("GET", f"/subscriptions/{subscription_id}", allow_missing=True)
	else:
		# Cobrança avulsa (diferença de um aumento): a referência está no próprio pagamento
		source = _asaas("GET", f"/payments/{payment_id}", allow_missing=True)
	site = (source or {}).get("externalReference") or ""
	if not re.fullmatch(r"[a-z0-9.-]+", site) or not _site_exists(site):
		return
	kwargs = {"event": event, "payment_id": payment_id, "subscription_id": subscription_id}
	if site == frappe.local.site:
		apply_asaas_event(**kwargs)
	else:
		_bench(["--site", site, "execute", "cred_hub.billing.apply_asaas_event", "--kwargs", json.dumps(kwargs)])


def apply_asaas_event(event, payment_id=None, subscription_id=None):
	"""No site da empresa. Não confia no conteúdo do webhook: confere pagamento e assinatura na API do Asaas."""
	plan = _plan()
	if not subscription_id:
		# Pagamento avulso: ou é a diferença de um aumento de plano, ou uma recarga de créditos de
		# bureaus Premium — nunca os dois. Tenta recarga primeiro (só bate se o payment_id for de um
		# 'Cred Hub Credit Topup' pendente); senão, segue o caminho de aumento de plano de sempre.
		if payment_id != plan.upgrade_payment_id and _apply_credit_topup(event, payment_id):
			return
		_apply_upgrade_payment(plan, event, payment_id)
		return
	if plan.asaas_subscription_id != subscription_id:
		return
	if event in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
		payment = _asaas("GET", f"/payments/{payment_id}")
		# Só um pagamento confirmado desta assinatura muda o plano
		if payment.get("status") not in CONFIRMED or payment.get("subscription") != subscription_id:
			return
		subscription = _asaas("GET", f"/subscriptions/{subscription_id}")
		values = {"status": "Active", "grace_until": None, "next_due_date": subscription.get("nextDueDate")}
		if not plan.upgrade_payment_id:
			# 1ª assinatura: os extras entram agora (com um aumento em aberto, as vagas já estão certas)
			values.update(extra_users=cint(plan.pending_extra_users) or cint(plan.extra_users), pending_extra_users=0)
			values["invoice_url"] = None
		_save_plan(plan, **values)
		restore_paused()
	elif event == "PAYMENT_OVERDUE":
		payment = _asaas("GET", f"/payments/{payment_id}")
		if payment.get("status") != "OVERDUE" or payment.get("subscription") != subscription_id:
			return
		# Tolerância antes de pausar (daily_check pausa depois de grace_until)
		_save_plan(
			plan, status="Past Due", invoice_url=payment.get("invoiceUrl"), grace_until=add_days(getdate(), GRACE_DAYS)
		)
	else:
		subscription = _asaas("GET", f"/subscriptions/{subscription_id}", allow_missing=True)
		if subscription and not subscription.get("deleted") and subscription.get("status") == "ACTIVE":
			return
		_save_plan(
			plan,
			status="Canceled",
			extra_users=0,
			monthly_amount=0,
			asaas_subscription_id=None,
			invoice_url=None,
			next_due_date=None,
			grace_until=None,
			**_cleared(),
		)
		pause_over_limit("canceled")
	frappe.db.commit()


def _apply_upgrade_payment(plan, event, payment_id):
	"""Cobrança avulsa da diferença de um aumento. Paga (no prazo ou depois): as vagas novas ficam e a mensalidade
	sobe. Vencida sem pagamento: só os usuários novos são pausados, até pagar."""
	if not payment_id or payment_id == plan.applied_upgrade_payment_id:
		return  # o Asaas manda CONFIRMED e depois RECEIVED para o mesmo pagamento
	if event == "PAYMENT_OVERDUE":
		payment = _asaas("GET", f"/payments/{payment_id}") if payment_id == plan.upgrade_payment_id else {}
		if payment.get("status") == "OVERDUE":
			_upgrade_overdue(plan, payment)
			frappe.db.commit()
		return
	if event not in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
		return
	payment = _asaas("GET", f"/payments/{payment_id}")
	if payment.get("status") not in CONFIRMED:
		return
	if plan.upgrade_payment_id != payment_id or plan.status != "Active" or not plan.asaas_subscription_id:
		# Dinheiro entrou para um aumento que não vale mais (cancelado, trocado ou diminuído): avisa para estornar
		frappe.log_error(
			title="Asaas: pagamento sem aumento pendente",
			message=f"Pagamento {payment_id} ({payment.get('value')}) confirmado em {frappe.local.site}, mas o plano não "
			"espera este aumento. Conferir e estornar no Asaas se for o caso.",
		)
		return
	quantity = cint(plan.pending_extra_users)
	_asaas(
		"PUT",
		f"/subscriptions/{plan.asaas_subscription_id}",
		{"value": PRICE_PER_USER * quantity, "description": _description(quantity), "updatePendingPayments": True},
	)
	_save_plan(
		plan,
		extra_users=quantity,
		monthly_amount=PRICE_PER_USER * quantity,
		pending_extra_users=0,
		upgrade_payment_id=None,
		upgrade_base_users=0,
		upgrade_due_date=None,
		applied_upgrade_payment_id=payment_id,
		invoice_url=None,
	)
	restore_paused()
	frappe.db.commit()


def _upgrade_overdue(plan, payment):
	"""Prazo da diferença venceu: as vagas voltam às pagas e os usuários novos (os mais recentes) são pausados.
	A cobrança continua aberta: pagando, eles voltam (o e-mail leva o link dela, não o da mensalidade)."""
	if cint(plan.extra_users) <= cint(plan.upgrade_base_users):
		return
	_save_plan(plan, extra_users=cint(plan.upgrade_base_users))
	pause_over_limit("upgrade_unpaid", payment.get("invoiceUrl"))


def daily_check():
	"""Agendador diário (cada empresa): diminuição que chega na virada, tolerância vencida e aumento sem pagamento
	cujo aviso do Asaas não chegou."""
	if not billing_enabled() or frappe.conf.get("credhub_unlimited_users"):
		return
	plan = _plan()
	today = getdate()
	_trial_reminder(plan, today)
	if plan.scheduled_from:
		turn = getdate(plan.scheduled_from)
		if cint(plan.scheduled_extra_users) == 0 and plan.asaas_subscription_id and today >= add_days(turn, -1):
			# Cancelamento: a assinatura sai na véspera, para não gerar a cobrança do mês seguinte
			_forget_subscription(plan)
		if today >= turn:
			quantity = cint(plan.scheduled_extra_users)
			values = {"extra_users": quantity, "scheduled_extra_users": 0, "scheduled_from": None}
			if quantity == 0:
				values.update(status="Free", monthly_amount=0, next_due_date=None, invoice_url=None)
			_save_plan(plan, **values)
			pause_over_limit("canceled" if quantity == 0 else "downgrade")
	if plan.status == "Past Due" and plan.grace_until and today > getdate(plan.grace_until):
		pause_over_limit("past_due")
	if plan.upgrade_payment_id and plan.upgrade_due_date and today > getdate(plan.upgrade_due_date):
		payment = _asaas("GET", f"/payments/{plan.upgrade_payment_id}", allow_missing=True)
		if payment.get("status") in CONFIRMED:
			_apply_upgrade_payment(plan, "PAYMENT_CONFIRMED", plan.upgrade_payment_id)
		else:
			_upgrade_overdue(plan, payment)
	if not in_trial(plan):
		# Virada do teste e fim da tolerância da 1ª fatura contratada no teste; nos outros dias não há excesso
		pause_over_limit("past_due" if _trial_contract(plan) else "trial")
	frappe.db.commit()


def _trial_reminder(plan, today):
	"""Aviso aos administradores 3 dias antes e no último dia do teste (decisão do dono, 27/10/2026), uma vez por marco,
	só quando alguém vai ser pausado. Desliga com `credhub_trial_reminders: 0`."""
	if not cint(frappe.conf.get("credhub_trial_reminders", 1)) or not in_trial(plan):
		return
	end = trial_ends_on(plan)
	mark = str(date_diff(end, today))
	sent = set(filter(None, (plan.trial_reminders_sent or "").split(",")))
	if cint(mark) not in TRIAL_REMINDER_DAYS or mark in sent:
		return
	# Vagas depois do teste: o grátis mais os extras contratados (pagos ou com a 1ª fatura em aberto)
	seats = FREE_USERS + max(cint(plan.extra_users), cint(plan.pending_extra_users))
	excess = len(active_users()) + len(pending_invitations()) - seats
	admins = set(frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"}, pluck="parent"))
	recipients = [name for name in active_users() if name in admins]
	if excess > 0 and recipients:
		last_day = mark == "0"
		frappe.sendmail(
			recipients=recipients,
			subject=_("HubTech CRM: your free trial ends today")
			if last_day
			else _("HubTech CRM: your free trial ends on {0}").format(format_date(end)),
			template="hubtech_trial_reminder",
			args={
				"last_day": last_day,
				"end": format_date(end),
				"pause_on": format_date(add_days(end, 1)),
				"free": FREE_USERS,
				"price": frappe.utils.fmt_money(PRICE_PER_USER, currency="BRL"),
				"excess": excess,
				"plan_url": frappe.utils.get_url("/crm"),
			},
		)
	_save_plan(plan, trial_reminders_sent=",".join(sorted(sent | {mark})))


def ensure_asaas_webhook():
	"""Site central (deploy): cadastra no Asaas o webhook do Cred Hub, se ainda não existir. Pode repetir."""
	if not billing_enabled() or not frappe.conf.get("credhub_signup") or not os.environ.get("ASAAS_API_KEY"):
		return "webhook do Asaas: pulado"
	url = frappe.utils.get_url("/api/method/cred_hub.billing.asaas_webhook")
	# Recusa do Asaas não para o deploy (igual ao webhook da Meta): fica no resultado e no log de erros
	try:
		existing = _asaas("GET", "/webhooks").get("data") or []
		if any(hook.get("url") == url for hook in existing):
			return "webhook do Asaas: já cadastrado"
		_register_asaas_webhook(url)
	except (frappe.ValidationError, requests.RequestException) as e:
		return f"webhook do Asaas: FALHOU ({e})"
	return f"webhook do Asaas: cadastrado ({now_datetime():%Y-%m-%d %H:%M})"


def _register_asaas_webhook(url):
	_asaas(
		"POST",
		"/webhooks",
		{
			"name": "Cred Hub",
			"url": url,
			"email": WEBHOOK_ALERT_EMAIL,
			"enabled": True,
			"interrupted": False,
			"apiVersion": 3,
			"authToken": os.environ["CREDHUB_ASAAS_WEBHOOK_TOKEN"],
			"sendType": "SEQUENTIALLY",
			"events": WEBHOOK_EVENTS,
		},
	)
