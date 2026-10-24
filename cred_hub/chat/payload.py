"""Leitura do webhook do Cred Chat e chave de telefone. Sem Frappe, para poder testar isolado."""

import re
from dataclasses import dataclass

WHATSAPP_CHANNEL = "Channel::Whatsapp"


@dataclass
class Conversation:
	conversation_id: int | None
	account_id: int | None
	channel: str
	name: str
	phone: str
	email: str
	first_message: str


def only_digits(value: str | None) -> str:
	return re.sub(r"\D", "", value or "")


def phone_key(raw: str | None) -> str:
	"""Chave para achar o mesmo telefone escrito de jeitos diferentes.

	Números do Brasil viram 55 + DDD + 8 últimos dígitos: o WhatsApp às vezes entrega o celular sem o 9º
	dígito, e o cadastro pode ter sido feito com ou sem +55, parênteses ou traço. Outros países: só os dígitos.
	"""
	digits = only_digits(raw)
	if len(digits) in (10, 11) and not (raw or "").strip().startswith("+"):
		digits = "55" + digits  # DDD + número, sem o código do país (com "+" já é internacional)
	if digits.startswith("55") and len(digits) in (12, 13):
		return digits[:4] + digits[-8:]
	return digits


def parse_conversation(payload: dict) -> Conversation:
	"""Campos usados do evento `conversation_created` (Conversations::EventDataPresenter#webhook_data)."""
	sender = (payload.get("meta") or {}).get("sender") or {}
	messages = payload.get("messages") or []
	first = messages[0] if messages and isinstance(messages[0], dict) else {}
	return Conversation(
		conversation_id=_int(payload.get("id")),
		account_id=_int((payload.get("account") or {}).get("id")),
		channel=payload.get("channel") or "",
		name=(sender.get("name") or "").strip(),
		phone=(sender.get("phone_number") or "").strip(),
		email=(sender.get("email") or "").strip(),
		first_message=(first.get("content") or "").strip(),
	)


def split_name(name: str) -> tuple[str, str]:
	parts = name.split()
	if not parts:
		return "", ""
	return parts[0], " ".join(parts[1:])


def conversation_url(chat_url: str, account_id: int, conversation_id: int) -> str:
	return f"{chat_url.rstrip('/')}/app/accounts/{account_id}/conversations/{conversation_id}"


def _int(value) -> int | None:
	try:
		return int(value)
	except (TypeError, ValueError):
		return None
