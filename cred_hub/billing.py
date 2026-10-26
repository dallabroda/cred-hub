"""Limite de usuários e cobrança de usuários extras pelo Asaas (credtech#25).

Regras do dono (25 e 26/10/2026): cada empresa tem 2 usuários grátis; do 3º em diante, só com a assinatura ativa,
R$ 60 por usuário a mais por mês, no cartão, pelo Asaas (a mesma conta do Cred Chat). Atraso ou cancelamento pausa
os usuários acima do limite, começando pelos mais recentes, sem apagar nada; eles voltam quando a assinatura é
regularizada.

- O plano fica no doctype único "Cred Hub Plan" de cada empresa. Só administradores (System Manager) veem e mudam.
- Aumentar com a assinatura ativa: cobra na hora a diferença proporcional aos dias que faltam no ciclo; os usuários
  novos só liberam depois do pagamento e daí em diante a mensalidade já vem com o valor novo. Diminuir: o valor
  menor vale a partir da próxima cobrança (sem reembolso do mês corrente). Decisões do dono, 26/10/2026.
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
from frappe.utils import cint, getdate, now_datetime

from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits

FREE_USERS = 2
PRICE_PER_USER = 60
PLAN = "Cred Hub Plan"
UNLIMITED = 1_000_000
# Menor cobrança avulsa aceita pelo Asaas no cartão
MIN_CHARGE = 5
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


# Plano e uso


def _plan():
	return frappe.get_single(PLAN)


def user_limit(plan=None):
	if frappe.conf.get("credhub_unlimited_users"):
		return UNLIMITED
	plan = plan or _plan()
	# Extras só valem com a assinatura em dia; trocar de quantidade não tira os já pagos até o pagamento novo
	extras = cint(plan.extra_users) if plan.status == "Active" else 0
	return FREE_USERS + extras


def active_users(exclude=None):
	filters = {"enabled": 1, "user_type": "System User", "name": ("not in", (*HIDDEN_USERS, exclude or ""))}
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
	if not _enforced() or doc.name in HIDDEN_USERS or frappe.flags.credhub_billing_sync:
		return
	if not doc.enabled or doc.user_type != "System User":
		return
	if not (doc.is_new() or doc.has_value_changed("enabled") or doc.has_value_changed("user_type")):
		return
	limit = user_limit()
	if len(active_users(exclude=doc.name)) >= limit:
		frappe.throw(_limit_message(limit), UserLimitError)


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
	plan = _plan()
	paused = frappe.get_all(
		"User", filters={"credhub_paused": 1}, fields=["name", "full_name"], order_by="creation asc"
	)
	return {
		"limit": user_limit(plan),
		"free": FREE_USERS,
		"extras": cint(plan.extra_users),
		"pending_extras": cint(plan.pending_extra_users),
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
	"""Quantos usuários extras a empresa quer (0 = só os grátis). Devolve {ok, invoice_url} ou {ok: False, ...}."""
	frappe.only_for("System Manager")
	quantity = cint(quantity)
	if quantity < 0 or quantity > 500:
		return _field_error("quantity", _("Choose between 0 and 500 extra users."))
	plan = _plan()
	needed_off = len(active_users()) - (FREE_USERS + quantity)
	if needed_off > 0:
		return {
			"ok": False,
			"field": "quantity",
			"deactivate": needed_off,
			"message": _("Deactivate {0} users before reducing the plan.").format(needed_off),
		}

	if quantity == 0:
		if plan.asaas_subscription_id:
			_asaas("DELETE", f"/subscriptions/{plan.asaas_subscription_id}", allow_missing=True)
		_save_plan(
			plan,
			status="Free",
			extra_users=0,
			pending_extra_users=0,
			monthly_amount=0,
			asaas_subscription_id=None,
			invoice_url=None,
			next_due_date=None,
			upgrade_payment_id=None,
		)
		return {"ok": True, "invoice_url": None, "status": "Free"}

	value = PRICE_PER_USER * quantity
	description = f"Cred Hub - {quantity} usuário(s) a mais - {frappe.local.site}"
	if plan.status == "Active" and plan.asaas_subscription_id:
		current = cint(plan.extra_users)
		if quantity <= current:
			# Diminuir: o valor menor vale a partir da próxima cobrança (a fatura já gerada fica como está)
			_asaas(
				"PUT",
				f"/subscriptions/{plan.asaas_subscription_id}",
				{"value": value, "description": description, "updatePendingPayments": False},
			)
			_save_plan(plan, extra_users=quantity, pending_extra_users=0, monthly_amount=value, invoice_url=None)
			return {"ok": True, "invoice_url": None, "status": "Active"}
		# Aumentar: cobra agora a diferença proporcional; os novos só liberam com o pagamento
		amount = _prorated(quantity - current, plan.next_due_date)
		payment = _asaas(
			"POST",
			"/payments",
			{
				"customer": plan.asaas_customer_id,
				"billingType": "CREDIT_CARD",
				"value": amount,
				"dueDate": str(getdate()),
				"description": f"Cred Hub - {quantity - current} usuário(s) a mais até o próximo vencimento - {frappe.local.site}",
				"externalReference": frappe.local.site,
			},
		)
		_save_plan(
			plan,
			pending_extra_users=quantity,
			upgrade_payment_id=payment["id"],
			invoice_url=payment.get("invoiceUrl"),
		)
		return {"ok": True, "invoice_url": payment.get("invoiceUrl"), "status": "Active", "amount": amount}

	clean = None
	if not plan.asaas_customer_id:
		clean, error = _clean_payer(payer)
		if error:
			return error
	if plan.asaas_subscription_id:
		# Assinatura antiga sem pagamento (aguardando, em atraso ou cancelada): começa de novo
		_asaas("DELETE", f"/subscriptions/{plan.asaas_subscription_id}", allow_missing=True)
	customer = plan.asaas_customer_id or _create_customer(clean)
	subscription = _asaas(
		"POST",
		"/subscriptions",
		{
			"customer": customer,
			"billingType": "CREDIT_CARD",
			"value": value,
			"nextDueDate": str(getdate()),
			"cycle": "MONTHLY",
			"description": description,
			"externalReference": frappe.local.site,
		},
	)
	payments = _asaas("GET", f"/subscriptions/{subscription['id']}/payments").get("data") or []
	invoice_url = payments[0].get("invoiceUrl") if payments else None
	_save_plan(
		plan,
		status="Awaiting Payment",
		pending_extra_users=quantity,
		monthly_amount=value,
		asaas_customer_id=customer,
		payer_name=clean["name"] if clean else plan.payer_name,
		payer_email=clean["email"] if clean else plan.payer_email,
		upgrade_payment_id=None,
		asaas_subscription_id=subscription["id"],
		invoice_url=invoice_url,
		next_due_date=subscription.get("nextDueDate"),
	)
	return {"ok": True, "invoice_url": invoice_url, "status": "Awaiting Payment"}


def _prorated(extra, next_due_date):
	"""Diferença proporcional aos dias que faltam até o próximo vencimento (ciclo mensal)."""
	from frappe.utils import add_months, date_diff

	today = getdate()
	due = getdate(next_due_date) if next_due_date else add_months(today, 1)
	cycle_start = getdate(add_months(due, -1))
	days_in_cycle = max(date_diff(due, cycle_start), 1)
	days_left = min(max(date_diff(due, today), 0), days_in_cycle)
	amount = round(PRICE_PER_USER * extra * days_left / days_in_cycle, 2)
	return max(amount, MIN_CHARGE)


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


# Pausar e reativar


def pause_over_limit():
	"""Pausa os usuários acima do limite, dos mais recentes para os mais antigos (o criador da empresa fica)."""
	users = active_users()
	excess = len(users) - user_limit()
	for name in reversed(users[-excess:] if excess > 0 else []):
		_set_user(name, enabled=0, paused=1)


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
		_save_plan(
			plan,
			status="Active",
			extra_users=cint(plan.pending_extra_users) or cint(plan.extra_users),
			pending_extra_users=0,
			invoice_url=None,
			next_due_date=subscription.get("nextDueDate"),
		)
		restore_paused()
	elif event == "PAYMENT_OVERDUE":
		payment = _asaas("GET", f"/payments/{payment_id}")
		if payment.get("status") != "OVERDUE" or payment.get("subscription") != subscription_id:
			return
		_save_plan(plan, status="Past Due", invoice_url=payment.get("invoiceUrl"))
		pause_over_limit()
	else:
		subscription = _asaas("GET", f"/subscriptions/{subscription_id}", allow_missing=True)
		if subscription and not subscription.get("deleted") and subscription.get("status") == "ACTIVE":
			return
		_save_plan(
			plan,
			status="Canceled",
			extra_users=0,
			pending_extra_users=0,
			monthly_amount=0,
			asaas_subscription_id=None,
			invoice_url=None,
			next_due_date=None,
		)
		pause_over_limit()
	frappe.db.commit()


def _apply_upgrade_payment(plan, event, payment_id):
	"""Cobrança avulsa da diferença de um aumento: confirmada, libera os extras e sobe a mensalidade."""
	if event not in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED") or not payment_id:
		return
	if plan.upgrade_payment_id != payment_id or plan.status != "Active":
		return
	payment = _asaas("GET", f"/payments/{payment_id}")
	if payment.get("status") not in CONFIRMED:
		return
	quantity = cint(plan.pending_extra_users)
	value = PRICE_PER_USER * quantity
	_asaas(
		"PUT",
		f"/subscriptions/{plan.asaas_subscription_id}",
		{
			"value": value,
			"description": f"Cred Hub - {quantity} usuário(s) a mais - {frappe.local.site}",
			"updatePendingPayments": True,
		},
	)
	_save_plan(
		plan,
		extra_users=quantity,
		pending_extra_users=0,
		monthly_amount=value,
		upgrade_payment_id=None,
		invoice_url=None,
	)
	restore_paused()
	frappe.db.commit()


def ensure_asaas_webhook():
	"""Site central (deploy): cadastra no Asaas o webhook do Cred Hub, se ainda não existir. Pode repetir."""
	if not frappe.conf.get("credhub_signup") or not os.environ.get("ASAAS_API_KEY"):
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
