"""Mensagem de boas-vindas pelo WhatsApp para o lead de anúncio ou formulário (credtech#53).

Decisões do dono (26/10/2026):
- o admin conecta o Cred Chat da empresa uma vez (conta + chave de acesso de um usuário dela, que pode ser a de um
  admin que já existe) e escolhe por qual caixa (número) a mensagem sai. É só esta parte da ligação Cred Hub ↔ Cred
  Chat (credtech#42); o resto segue congelado;
- vem desligada: o admin liga por origem de leads, ciente de que a Meta cobra cada conversa iniciada pela empresa na
  conta de WhatsApp dela;
- o texto é um template já aprovado pela Meta no Cred Chat da empresa; campos do lead preenchem as variáveis.

Só card novo (Created) recebe; repetido e teste nunca. O envio vai para a fila depois do commit do card, para não
atrasar a entrada do lead, e não é repetido sozinho (melhor uma falha visível que uma mensagem em dobro).
Origem: "facebook:<id do formulário>" para formulário da Meta, ou o name da Cred Hub Lead Source.
"""

import json
import os
import re

import frappe
import phonenumbers
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from cred_hub import chat_panel

CONNECTION = "Cred Hub Chat Connection"
WELCOME = "Cred Hub Welcome"
EVENT = "Cred Hub Lead Event"
SOURCE = "Cred Hub Lead Source"
MANAGERS = ("System Manager", "Sales Manager")
DEFAULT_CHAT_URL = "https://chat.credtechdigital.com.br"
# Campos do lead que podem preencher uma variável do template
LEAD_FIELD_TYPES = ("Data", "Select", "Link", "Small Text", "Int", "Float", "Currency", "Date", "Phone")
VARIABLE = re.compile(r"{{\s*([\w.]+)\s*}}")


class ChatError(Exception):
	pass


def _only_managers():
	if not set(MANAGERS) & set(frappe.get_roles()):
		raise frappe.PermissionError


def _chat_url():
	return (os.environ.get("CREDHUB_CHAT_URL") or frappe.conf.get("credhub_chat_url") or DEFAULT_CHAT_URL).rstrip("/")


def _call(method, path, token, **kwargs):
	"""Uma chamada à API do Cred Chat; erros viram ChatError com texto para o admin."""
	try:
		response = requests.request(
			method, _chat_url() + path, headers={"api_access_token": token}, timeout=20, **kwargs
		)
	except requests.RequestException:
		raise ChatError(_("Cred Chat did not answer. Try again in a few minutes."))
	if response.status_code == 401:
		raise ChatError(_("The Cred Chat access key is not valid."))
	if response.status_code in (403, 404):
		raise ChatError(_("This access key has no access to that Cred Chat account."))
	if response.status_code >= 400:
		try:
			body = response.json()
			detail = body.get("message") or body.get("error") or "; ".join(body.get("attributes") or []) or ""
		except ValueError:
			detail = ""
		raise ChatError(_("Cred Chat refused the request: {0}").format(str(detail or response.status_code)[:300]))
	return response.json() if response.content else {}


# Conexão


def _connection():
	doc = frappe.get_single(CONNECTION)
	if not doc.account_id:
		return None, None
	return doc, doc.get_password("access_token", raise_exception=False)


def _variables(template):
	"""Variáveis do corpo do template, na ordem ({{1}}, {{2}}... ou nomeadas)."""
	body = next((c for c in template.get("components") or [] if c.get("type") == "BODY"), {})
	seen = []
	for name in VARIABLE.findall(body.get("text") or ""):
		if name not in seen:
			seen.append(name)
	return seen


def _supported(template):
	"""Só templates de texto: cabeçalho com mídia ou botão com variável pedem dados que o lead não tem."""
	for component in template.get("components") or []:
		kind = component.get("type")
		if kind == "HEADER" and (component.get("format") not in (None, "TEXT") or VARIABLE.search(component.get("text") or "")):
			return False
		if kind == "BUTTONS" and any(VARIABLE.search(b.get("url") or "") for b in component.get("buttons") or []):
			return False
	return True


def _templates(inbox):
	return [
		{
			"name": t.get("name"),
			"language": t.get("language"),
			"category": t.get("category"),
			"body": next((c.get("text") for c in t.get("components") or [] if c.get("type") == "BODY"), ""),
			"variables": _variables(t),
		}
		for t in inbox.get("message_templates") or []
		if str(t.get("status")).upper() == "APPROVED" and _supported(t)
	]


def _inboxes(account_id, token):
	"""Caixas de WhatsApp da conta, cada uma com seus templates aprovados."""
	data = _call("GET", f"/api/v1/accounts/{cint(account_id)}/inboxes", token)
	return [
		{"id": i["id"], "name": i.get("name"), "phone": i.get("phone_number"), "templates": _templates(i)}
		for i in data.get("payload") or []
		if i.get("channel_type") == "Channel::Whatsapp"
	]


@frappe.whitelist(methods=["GET"])
def get_chat_connection():
	_only_managers()
	doc, token = _connection()
	if not doc:
		return {
			"connected": False,
			"account_id": None,
			"account_name": None,
			"error": None,
			"inboxes": [],
			"panel_installed": False,
			"panel_error": None,
		}
	result = {
		"connected": True,
		"account_id": doc.account_id,
		"account_name": doc.account_name,
		"error": None,
		"inboxes": [],
		# Painel do Cred Hub na conversa (credtech#45), instalado pela mesma conexão
		"panel_installed": bool(doc.panel_app_id),
		"panel_error": doc.panel_error,
	}
	try:
		result["inboxes"] = _inboxes(doc.account_id, token)
	except ChatError as e:
		result["error"] = str(e)
	return result


@frappe.whitelist(methods=["POST"])
def connect_chat(account_id, access_token):
	"""Confere a chave na API do Cred Chat antes de guardar; a chave nunca volta para a tela."""
	_only_managers()
	account_id, access_token = cint(account_id), (access_token or "").strip()
	if not account_id or not access_token:
		frappe.throw(_("Inform the Cred Chat account number and access key."))
	try:
		profile = _call("GET", "/api/v1/profile", access_token)
		account = next((a for a in profile.get("accounts") or [] if cint(a.get("id")) == account_id), None)
		if not account:
			raise ChatError(_("This access key has no access to that Cred Chat account."))
		_inboxes(account_id, access_token)
	except ChatError as e:
		frappe.throw(str(e))
	doc, old_token = _connection()
	if doc and cint(doc.account_id) != account_id:
		chat_panel.uninstall(doc, old_token)  # o painel não fica na conta antiga
	doc = doc or frappe.get_single(CONNECTION)
	doc.update(
		{
			"account_id": account_id,
			"account_name": account.get("name"),
			"access_token": access_token,
			"connected_by": frappe.session.user,
			"connected_on": now_datetime(),
		}
	)
	chat_panel.install(doc, access_token)
	doc.save(ignore_permissions=True)
	return get_chat_connection()


@frappe.whitelist(methods=["POST"])
def disconnect_chat():
	"""Apaga a chave, tira o painel do Cred Chat e desliga as boas-vindas de todas as origens (sem conexão, nada
	sairia)."""
	_only_managers()
	doc, token = _connection()
	if doc:
		chat_panel.uninstall(doc, token)
	doc = doc or frappe.get_single(CONNECTION)
	doc.update({"account_id": None, "account_name": None, "access_token": None, "connected_by": None, "connected_on": None})
	doc.save(ignore_permissions=True)
	frappe.db.set_value(WELCOME, {"enabled": 1}, "enabled", 0)
	return get_chat_connection()


# Configuração por origem


def _lead_fields():
	meta = frappe.get_meta("CRM Lead")
	return [
		{"fieldname": f.fieldname, "label": _(f.label)}
		for f in meta.fields
		if f.fieldtype in LEAD_FIELD_TYPES and f.label and not f.hidden
	]


def _check_source(source):
	if source.startswith("facebook:"):
		if not frappe.db.exists("Facebook Lead Form", source.partition(":")[2]):
			frappe.throw(_("Lead source not found."))
	elif not frappe.db.exists(SOURCE, source):
		frappe.throw(_("Lead source not found."))


def _load_variables(raw):
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw else (raw or [])
	except ValueError:
		value = []
	return [str(v) for v in value] if isinstance(value, list) else []


@frappe.whitelist(methods=["GET"])
def get_welcome(source):
	_only_managers()
	_check_source(source)
	doc = frappe.db.get_value(
		WELCOME, source, ["enabled", "inbox_id", "template_name", "template_language", "variables"], as_dict=True
	) or {}
	return {
		"source": source,
		"enabled": bool(doc.get("enabled")),
		"inbox_id": doc.get("inbox_id") or None,
		"template_name": doc.get("template_name"),
		"template_language": doc.get("template_language"),
		"variables": _load_variables(doc.get("variables")),
		"lead_fields": _lead_fields(),
	}


@frappe.whitelist(methods=["POST"])
def save_welcome(source, enabled=None, inbox_id=None, template_name=None, template_language=None, variables=None):
	"""Liga/desliga e escolhe caixa, template e o campo do lead de cada variável. Ligada, confere tudo no Cred Chat."""
	_only_managers()
	_check_source(source)
	doc = frappe.get_doc(WELCOME, source) if frappe.db.exists(WELCOME, source) else frappe.new_doc(WELCOME)
	doc.source = source
	if enabled is not None:
		doc.enabled = cint(enabled)
	if inbox_id is not None:
		doc.inbox_id = cint(inbox_id) or None
	if template_name is not None:
		doc.template_name = template_name or None
	if template_language is not None:
		doc.template_language = template_language or None
	if variables is not None:
		doc.variables = json.dumps(_load_variables(variables))
	if doc.enabled:
		doc.template_category = _validate(doc)["category"]
	doc.save(ignore_permissions=True)
	return get_welcome(source)


def _validate(doc):
	"""O template escolhido existe, está aprovado nessa caixa e cada variável tem um campo do lead."""
	connection, token = _connection()
	if not connection:
		frappe.throw(_("Connect the Cred Chat before turning on the welcome message."))
	if not doc.inbox_id or not doc.template_name:
		frappe.throw(_("Choose the WhatsApp number and the message template."))
	try:
		inbox = next((i for i in _inboxes(connection.account_id, token) if i["id"] == doc.inbox_id), None)
	except ChatError as e:
		frappe.throw(str(e))
	if not inbox:
		frappe.throw(_("This WhatsApp number is not in the connected Cred Chat account."))
	template = next(
		(
			t
			for t in inbox["templates"]
			if t["name"] == doc.template_name and (not doc.template_language or t["language"] == doc.template_language)
		),
		None,
	)
	if not template:
		frappe.throw(_("This template is not approved for this WhatsApp number."))
	doc.template_language = template["language"]
	variables = _load_variables(doc.variables)
	fields = {f["fieldname"] for f in _lead_fields()}
	if len(variables) != len(template["variables"]) or any(v not in fields for v in variables):
		frappe.throw(_("Choose a lead field for each variable of the template."))
	return template


# Envio


def _source_key(event):
	return f"facebook:{event.form_id}" if event.source == "Facebook" else event.source


def queue(event):
	"""Card novo gravado (ainda antes do commit): se a origem tem boas-vindas ligadas, o envio vai para a fila."""
	if not frappe.db.get_value(WELCOME, _source_key(event), "enabled"):
		return
	frappe.db.set_value(EVENT, event.name, "welcome", "Queued", update_modified=False)
	frappe.enqueue("cred_hub.welcome.send", queue="short", event=event.name, enqueue_after_commit=True)


def _e164(phone):
	try:
		number = phonenumbers.parse(phone or "", "BR")
	except phonenumbers.NumberParseException:
		return None
	return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(number) else None


def _phone_variants(phone):
	"""O mesmo celular brasileiro com e sem o 9 na frente: contatos antigos do WhatsApp chegam sem ele, e comparar o
	número exato criava um segundo contato (e uma segunda conversa no mesmo chat do cliente, credtech#258)."""
	digits = (phone or "").lstrip("+")
	variants = {"+" + digits}
	if digits.startswith("55") and len(digits) == 13 and digits[4] == "9":
		variants.add("+" + digits[:4] + digits[5:])
	elif digits.startswith("55") and len(digits) == 12 and digits[4] in "6789":
		variants.add("+" + digits[:4] + "9" + digits[4:])
	return variants


def _find_contact(account_id, token, phone):
	"""Contato do Cred Chat com este número (com ou sem o 9), preferindo o número exato."""
	variants = _phone_variants(phone)
	# a busca do Cred Chat compara pedaços do número: os 8 últimos dígitos são iguais nas duas formas
	found = _call("GET", f"/api/v1/accounts/{account_id}/contacts/search", token, params={"q": phone[-8:]})
	matches = [c for c in found.get("payload") or [] if c.get("phone_number") in variants]
	exact = next((c for c in matches if c.get("phone_number") == phone), None)
	return (exact or (matches[0] if matches else {})).get("id")


def _contact(account_id, token, name, phone):
	"""Contato do Cred Chat com este número: o que já existe ou um novo."""
	existing = _find_contact(account_id, token, phone)
	if existing:
		return existing
	created = _call("POST", f"/api/v1/accounts/{account_id}/contacts", token, json={"name": name, "phone_number": phone})
	payload = created.get("payload") or {}
	return (payload.get("contact") or payload)["id"]


def send(event):
	"""Fila: manda o template para o lead e grava o resultado no evento (aparece em "Últimos leads")."""
	doc = frappe.get_doc(EVENT, event)
	if doc.welcome != "Queued":
		return
	try:
		conversation = _send(doc)
		doc.db_set({"welcome": "Sent", "welcome_error": None, "welcome_conversation": conversation}, update_modified=False)
	except ChatError as e:
		doc.db_set({"welcome": "Failed", "welcome_error": str(e)[:500]}, update_modified=False)
	except Exception as e:
		frappe.log_error(title=f"Welcome message {event}")
		doc.db_set({"welcome": "Failed", "welcome_error": str(e)[:500]}, update_modified=False)
	frappe.db.commit()


def _send(event):
	welcome = frappe.get_doc(WELCOME, _source_key(event))
	connection, token = _connection()
	if not welcome.enabled or not connection:
		raise ChatError(_("The welcome message was turned off or the Cred Chat was disconnected."))
	lead = frappe.get_doc("CRM Lead", event.lead)
	phone = _e164(lead.mobile_no or lead.phone)
	if not phone:
		raise ChatError(_("The lead has no valid mobile number."))
	values = {}
	inbox = next((i for i in _inboxes(connection.account_id, token) if i["id"] == welcome.inbox_id), None)
	template = next(
		(t for t in (inbox or {}).get("templates") or [] if t["name"] == welcome.template_name and t["language"] == welcome.template_language),
		None,
	)
	if not template:
		raise ChatError(_("The template is no longer approved for this WhatsApp number."))
	for name, field in zip(template["variables"], _load_variables(welcome.variables)):
		value = str(lead.get(field) or "").strip()
		if not value:
			raise ChatError(_("The lead has no value for {0}.").format(_(frappe.get_meta("CRM Lead").get_label(field))))
		values[name] = value
	content = VARIABLE.sub(lambda m: values.get(m.group(1), m.group(0)), template["body"])
	contact = _contact(connection.account_id, token, lead.lead_name or lead.first_name or phone, phone)
	conversation = _call(
		"POST",
		f"/api/v1/accounts/{connection.account_id}/conversations",
		token,
		json={
			"contact_id": contact,
			"inbox_id": welcome.inbox_id,
			"message": {
				"content": content,
				"template_params": {
					"name": template["name"],
					"category": template["category"],
					"language": template["language"],
					"processed_params": {"body": values},
				},
			},
		},
	)
	return str(conversation.get("id"))


def conversation_url(event):
	"""Link da conversa no Cred Chat (para "Últimos leads")."""
	if not event.get("welcome_conversation"):
		return None
	account_id = frappe.db.get_single_value(CONNECTION, "account_id")
	return f"{_chat_url()}/app/accounts/{account_id}/conversations/{event.welcome_conversation}" if account_id else None
