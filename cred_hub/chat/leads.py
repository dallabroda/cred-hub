"""Conversa nova no Cred Chat -> Lead no Cred Hub (ou só o link, se a pessoa já está cadastrada)."""

import frappe
from frappe import _
from frappe.utils import escape_html, validate_email_address

from cred_hub.chat.payload import WHATSAPP_CHANNEL, Conversation, conversation_url, phone_key, split_name

CONVERSATION_FIELD = "chat_conversation_url"
# Origens do lead (nomes em inglês como as do Frappe CRM; pt-BR em locale/pt_BR.po)
WHATSAPP_SOURCE = "WhatsApp"
CHAT_SOURCE = "Cred Chat"


def handle_conversation(conv: Conversation, chat_url: str, account_id: int | None = None) -> dict:
	if not conv.conversation_id or not conv.account_id:
		return {"status": "ignored", "reason": "missing conversation or account id"}
	if account_id and conv.account_id != account_id:
		return {"status": "ignored", "reason": "other Cred Chat account"}
	if not (conv.phone or conv.email):
		return {"status": "ignored", "reason": "contact without phone or e-mail"}

	url = conversation_url(chat_url, conv.account_id, conv.conversation_id)
	email = _valid_email(conv.email)

	lead = find_open_lead(conv.phone, email)
	if lead:
		_link(frappe.get_doc("CRM Lead", lead), url, conv)
		return {"status": "updated", "doctype": "CRM Lead", "name": lead}

	deal = find_deal(conv.phone, email)
	if deal:
		_link(frappe.get_doc("CRM Deal", deal), url, conv)
		return {"status": "updated", "doctype": "CRM Deal", "name": deal}

	doc = create_lead(conv, email, url)
	return {"status": "created", "doctype": "CRM Lead", "name": doc.name}


def find_open_lead(phone: str, email: str) -> str | None:
	"""Lead ainda não convertido em negócio, pelo telefone (qualquer formato) ou e-mail."""
	key = phone_key(phone)
	if key:
		rows = frappe.db.sql(
			"""select name, mobile_no, phone from `tabCRM Lead`
			where converted = 0
				and (regexp_replace(ifnull(mobile_no, ''), '[^0-9]', '') like %(tail)s
					or regexp_replace(ifnull(phone, ''), '[^0-9]', '') like %(tail)s)
			order by modified desc""",
			{"tail": "%" + key[-8:]},
			as_dict=True,
		)
		for row in rows:
			if key in (phone_key(row.mobile_no), phone_key(row.phone)):
				return row.name
	if email:
		return frappe.db.get_value("CRM Lead", {"converted": 0, "email": email}, "name", order_by="modified desc")
	return None


def find_deal(phone: str, email: str) -> str | None:
	"""Negócio mais recente de um contato com esse telefone ou e-mail."""
	contacts = set()
	key = phone_key(phone)
	if key:
		rows = frappe.db.sql(
			"""select parent, phone from `tabContact Phone`
			where parenttype = 'Contact' and regexp_replace(ifnull(phone, ''), '[^0-9]', '') like %(tail)s""",
			{"tail": "%" + key[-8:]},
			as_dict=True,
		)
		contacts.update(row.parent for row in rows if phone_key(row.phone) == key)
	if email:
		contacts.update(frappe.get_all("Contact Email", filters={"parenttype": "Contact", "email_id": email}, pluck="parent"))
	if not contacts:
		return None
	deals = frappe.get_all(
		"CRM Contacts",
		filters={"parenttype": "CRM Deal", "contact": ["in", list(contacts)]},
		pluck="parent",
	)
	if not deals:
		return None
	return frappe.get_all("CRM Deal", filters={"name": ["in", deals]}, pluck="name", order_by="modified desc", limit=1)[0]


def create_lead(conv: Conversation, email: str, url: str):
	first_name, last_name = split_name(conv.name)
	source = ensure_source(WHATSAPP_SOURCE if conv.channel == WHATSAPP_CHANNEL else CHAT_SOURCE)
	doc = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": first_name or conv.phone or email,
			"last_name": last_name,
			"mobile_no": conv.phone,
			"email": email,
			"source": source,
			CONVERSATION_FIELD: url,
		}
	).insert(ignore_permissions=True)
	doc.add_comment("Comment", _comment(_("Lead criado a partir de uma conversa nova no Cred Chat."), url, conv))
	return doc


def ensure_source(name: str) -> str:
	if not frappe.db.exists("CRM Lead Source", name):
		frappe.get_doc({"doctype": "CRM Lead Source", "source_name": name}).insert(ignore_permissions=True)
	return name


def _link(doc, url: str, conv: Conversation):
	# Guarda sempre a conversa mais recente; as anteriores ficam nos comentários
	doc.db_set(CONVERSATION_FIELD, url)
	doc.add_comment("Comment", _comment(_("Nova conversa no Cred Chat."), url, conv))


def _comment(text: str, url: str, conv: Conversation) -> str:
	html = f'{escape_html(text)} <a href="{escape_html(url)}" target="_blank">{escape_html(_("Abrir conversa"))}</a>'
	if conv.first_message:
		html += f"<br><i>{escape_html(conv.first_message[:500])}</i>"
	return html


def _valid_email(email: str) -> str:
	return email if email and validate_email_address(email) else ""
