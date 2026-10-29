"""WhatsApp do card pelo SmartChat da empresa (credtech#211).

Pedido do dono (28/10/2026): o vendedor escolhe por qual caixa de entrada a mensagem sai. O atalho de WhatsApp do card
deixa de abrir o WhatsApp pessoal (wa.me) e passa a mandar pela conexão do SmartChat que o admin já fez para a
boas-vindas (#53):

- só aparecem as caixas de WhatsApp (oficial ou QR Code) de que o usuário do vendedor no SmartChat (mesmo e-mail) é
  membro; admin do SmartChat vê todas;
- número oficial com a janela de 24 h da Meta aberta, ou número por QR Code: texto livre. Número oficial fora da
  janela: só modelo aprovado, com as variáveis preenchidas pelo vendedor;
- a conversa fica atribuída ao vendedor e o card ganha um comentário com a caixa, o texto e o link da conversa.

A API do SmartChat não deixa escolher o remetente: a mensagem sai em nome do usuário da chave da conexão.

Também a partir do contato (credtech#258): o número precisa ser um dos telefones do contato e o envio fica registrado
como anotação dele.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

from cred_hub import welcome

OFFICIAL, QR = "official", "qr"


def _inboxes(account_id, token):
	"""Caixas de WhatsApp da conta: número oficial (com os modelos aprovados) ou número por QR Code."""
	data = welcome._call("GET", f"/api/v1/accounts/{cint(account_id)}/inboxes", token)
	inboxes = []
	for inbox in data.get("payload") or []:
		if inbox.get("channel_type") == "Channel::Whatsapp":
			kind, templates = OFFICIAL, welcome._templates(inbox)
		elif (
			inbox.get("channel_type") == "Channel::Api"
			and (inbox.get("additional_attributes") or {}).get("connection_type") == "evolution_qr"
		):
			kind, templates = QR, []
		else:
			continue
		inboxes.append(
			{"id": inbox["id"], "name": inbox.get("name"), "phone": inbox.get("phone_number"), "kind": kind, "templates": templates}
		)
	return inboxes


def _agent(account_id, token):
	"""O usuário do SmartChat com o e-mail de quem está no HubTech."""
	email = (frappe.db.get_value("User", frappe.session.user, "email") or frappe.session.user).strip().lower()
	agents = welcome._call("GET", f"/api/v1/accounts/{cint(account_id)}/agents", token)
	agent = next((a for a in agents or [] if str(a.get("email") or "").strip().lower() == email), None)
	if not agent:
		raise welcome.ChatError(
			_("Your e-mail ({0}) is not a user of the company's SmartChat. Ask an admin to invite you there.").format(email)
		)
	return agent


def _allowed(account_id, token, agent, inboxes):
	if agent.get("role") == "administrator":
		return inboxes
	allowed = []
	for inbox in inboxes:
		members = welcome._call("GET", f"/api/v1/accounts/{cint(account_id)}/inbox_members/{cint(inbox['id'])}", token)
		if any(cint(m.get("id")) == cint(agent.get("id")) for m in members.get("payload") or []):
			allowed.append(inbox)
	return allowed


def _existing_contact(account_id, token, phone):
	return welcome._find_contact(cint(account_id), token, phone)


def _conversations(account_id, token, contact_id):
	"""Última conversa do contato em cada caixa: {inbox_id: conversa}."""
	if not contact_id:
		return {}
	data = welcome._call("GET", f"/api/v1/accounts/{cint(account_id)}/contacts/{cint(contact_id)}/conversations", token)
	latest = {}
	for conversation in data.get("payload") or []:
		inbox_id = cint(conversation.get("inbox_id"))
		if inbox_id not in latest or cint(conversation.get("id")) > cint(latest[inbox_id].get("id")):
			latest[inbox_id] = conversation
	return latest


def _reference(deal, contact):
	"""O card ou o contato de onde sai a mensagem; quem envia precisa poder ver o registro."""
	if deal:
		doc = frappe.get_doc("CRM Deal", deal)
	elif contact:
		doc = frappe.get_doc("Contact", contact)
	else:
		frappe.throw(_("Choose a card or a contact."))
	doc.check_permission("read")
	return doc


def _contact_numbers(contact):
	numbers = [contact.mobile_no, contact.phone, *(row.phone for row in contact.get("phone_nos") or [])]
	return {welcome._e164(n) for n in numbers if n} - {None}


def _options(deal, phone, contact=None):
	reference = _reference(deal, contact)
	number = welcome._e164(phone)
	if not number:
		raise welcome.ChatError(_("This contact has no valid mobile number."))
	# Do contato só sai para um telefone dele: a tela não escolhe um número qualquer
	if reference.doctype == "Contact" and number not in _contact_numbers(reference):
		raise welcome.ChatError(_("This number is not one of the contact's phones."))
	connection, token = welcome._connection()
	if not connection:
		return None, None, number, [], None
	account_id = connection.account_id
	agent = _agent(account_id, token)
	inboxes = _allowed(account_id, token, agent, _inboxes(account_id, token))
	conversations = _conversations(account_id, token, _existing_contact(account_id, token, number))
	for inbox in inboxes:
		conversation = conversations.get(cint(inbox["id"]))
		inbox["conversation_id"] = conversation.get("id") if conversation else None
		# QR Code não tem a janela de 24 h da Meta; número oficial só aceita texto livre com a janela aberta
		inbox["can_reply"] = inbox["kind"] == QR or bool(conversation and conversation.get("can_reply"))
	return connection, token, number, inboxes, agent


@frappe.whitelist()
def get_whatsapp_options(deal: str | None = None, phone: str = "", contact: str | None = None) -> dict:
	"""O que a janela "Enviar WhatsApp" mostra: as caixas que o vendedor pode usar e, em cada uma, se dá para
	escrever livre ou só com modelo."""
	try:
		result = _options(deal, phone, contact)
	except welcome.ChatError as e:
		return {"connected": True, "error": str(e), "inboxes": []}
	connection, _token, number, inboxes, _agent_ = result
	if not connection:
		return {"connected": False, "inboxes": [], "phone": number}
	return {"connected": True, "phone": number, "inboxes": inboxes}


@frappe.whitelist(methods=["POST"])
def send_whatsapp(
	deal: str | None = None,
	phone: str = "",
	inbox_id: int = 0,
	name: str | None = None,
	text: str | None = None,
	template_name: str | None = None,
	template_language: str | None = None,
	variables: str | dict | None = None,
	contact: str | None = None,
) -> dict:
	"""Manda a mensagem pela caixa escolhida, atribui a conversa ao vendedor e registra no card. Tudo é conferido de
	novo aqui: a caixa precisa ser do vendedor e o texto livre só passa com a janela aberta."""
	try:
		connection, token, number, inboxes, agent = _options(deal, phone, contact)
	except welcome.ChatError as e:
		frappe.throw(str(e))
	if not connection:
		frappe.throw(_("Connect the company's SmartChat first (Settings › Integrations › Lead Sources)."))
	inbox = next((i for i in inboxes if cint(i["id"]) == cint(inbox_id)), None)
	if not inbox:
		frappe.throw(_("You are not a member of this SmartChat inbox."), frappe.PermissionError)

	account_id = cint(connection.account_id)
	text = (text or "").strip()
	if inbox["can_reply"]:
		if not text:
			frappe.throw(_("Write the message."))
		message = {"content": text}
	else:
		message = _template_message(inbox, template_name, template_language, variables)

	try:
		conversation_id = inbox.get("conversation_id")
		if conversation_id:
			welcome._call(
				"POST",
				f"/api/v1/accounts/{account_id}/conversations/{cint(conversation_id)}/messages",
				token,
				json={**message, "message_type": "outgoing"},
			)
		else:
			chat_contact = welcome._contact(account_id, token, (name or "").strip() or number, number)
			created = welcome._call(
				"POST",
				f"/api/v1/accounts/{account_id}/conversations",
				token,
				json={"contact_id": chat_contact, "inbox_id": cint(inbox["id"]), "message": message},
			)
			conversation_id = created.get("id")
		welcome._call(
			"POST",
			f"/api/v1/accounts/{account_id}/conversations/{cint(conversation_id)}/assignments",
			token,
			json={"assignee_id": cint(agent.get("id"))},
		)
	except welcome.ChatError as e:
		frappe.throw(str(e))

	url = f"{welcome._chat_url()}/app/accounts/{account_id}/conversations/{cint(conversation_id)}"
	record = (
		_("WhatsApp sent to {0} through the SmartChat inbox {1}:").format(number, inbox["name"])
		+ f"<br>{frappe.utils.escape_html(message['content'])}<br>"
		+ f'<a href="{url}" target="_blank" rel="noopener noreferrer">{_("Open the conversation in SmartChat")}</a>'
	)
	if deal:
		frappe.get_doc("CRM Deal", deal).add_comment("Comment", record)
	else:
		# O contato não tem linha do tempo: o envio fica como anotação dele (aba Anotações, credtech#214)
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": _("WhatsApp sent"),
				"content": record,
				"reference_doctype": "Contact",
				"reference_docname": contact,
			}
		).insert(ignore_permissions=True)
	return {"conversation_id": conversation_id, "conversation_url": url}


def _template_message(inbox, template_name, template_language, variables):
	"""Mensagem com modelo aprovado: o único jeito de falar primeiro (ou depois de 24 h) num número oficial."""
	template = next(
		(t for t in inbox["templates"] if t["name"] == template_name and t["language"] == template_language), None
	)
	if not template:
		frappe.throw(_("Outside the 24-hour window, WhatsApp only accepts a template approved by Meta. Choose one."))
	given = json.loads(variables) if isinstance(variables, str) and variables else (variables or {})
	values = {}
	for variable in template["variables"]:
		value = str(given.get(variable) or "").strip()
		if not value:
			frappe.throw(_("Fill in every field of the template."))
		values[variable] = value
	return {
		"content": welcome.VARIABLE.sub(lambda m: values.get(m.group(1), m.group(0)), template["body"]),
		"template_params": {
			"name": template["name"],
			"category": template["category"],
			"language": template["language"],
			"processed_params": {"body": values},
		},
	}
