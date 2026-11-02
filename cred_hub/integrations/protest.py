"""Funil de Cobrança com protesto no cartório pela CENPROT (credtech#290, módulo 2).

Ao ligar a CENPROT nas configurações, o site ganha o funil "Debt Collection" (Cobrança) com 8 etapas e os campos de
protesto no formulário do card. Depois:

- **Enviar a protesto** (botão do card, ou o card entrar na etapa de envio, se o cliente ligou; desligado por
  padrão, o cartório cobra custas): monta o título com o credor
  das configurações, o devedor da Organização do card (CNPJ e endereço da Receita) ou, sem CNPJ, o contato principal
  com CPF, e chama `EnviarTitulo`. Número do título e "nosso número" vazios recebem os dígitos do código do card.
  Só sai título vencido, e só quando ainda não está no cartório (status vazio, INEXISTENTE ou DEVOLVIDO).
- **Atualizar status** (botão, e todo dia para os títulos em andamento): `ConsultarTitulo`; mudou o status, o card
  anda para a etapa correspondente, a linha do tempo registra e o responsável recebe uma notificação.
- **Retirar ou cancelar** (botão, ou o card entrar na etapa de acordo, se o cliente ligou; desligado por padrão): `OperacaoTitulo` com REMOCAO, DESISTENCIA
  ou CANCELAMENTO conforme o status (manual, pág. 16).

Toda chamada fica em Cred Hub Bureau Query. Falha da CENPROT nunca usa frappe.throw depois da chamada: o histórico
precisa ficar gravado.
"""

import base64
import hashlib
from html import escape

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits
from cred_hub.integrations import bureau_log, get_settings, secret
from cred_hub.integrations.cenprot import (
	AT_SIGHT,
	KNOWN_STATUSES,
	OPERATION_FOR_STATUS,
	OUR_NUMBER_SIZE,
	CenprotClient,
	CenprotError,
	br_date,
)
from cred_hub.integrations.fields import DEAL_FORM_FIELDS

PROVIDER = "CENPROT"
SEND, CONSULT, OPERATE = "enviar_titulo", "consultar_titulo", "operacao_titulo"
DEAL = "CRM Deal"
# Pode enviar: primeiro envio ou título devolvido pelo cartório
SENDABLE = frozenset({"", "INEXISTENTE", "DEVOLVIDO"})
# A consulta diária para nesses status (nada mais muda sem nova ação)
FINAL = ("PAGO", "CANCELADO", "RETIRADO", "DEVOLVIDO", "INEXISTENTE")
# Status do cartório -> etapa do funil (campo das configurações)
STAGE_FOR_STATUS = {
	"CONFIRMADO": "stage_at_registry",
	"DISTRIBUIDO": "stage_at_registry",
	"INTIMADO": "stage_notified",
	"PROTESTADO": "stage_protested",
	"PAGO": "stage_paid",
	"CANCELADO": "stage_agreement",
	"RETIRADO": "stage_agreement",
}
DECLARATION_FOR_SPECIES = {"DMI": "D", "DSI": "D", "CCB": "C", "CBI": "C"}
ENDORSEMENT = {"Mandate endorsement": "M", "Transfer endorsement": "T", "Blank endorsement": "B"}
COLLECTION_PIPELINE = "Debt Collection"
# (etapa, tipo, cor, probabilidade, campo das configurações); nomes em inglês, pt-BR no .po
COLLECTION_STAGES = (
	("Overdue / Friendly Notice", "Open", "orange", 10, None),
	("Out-of-court Notice", "Ongoing", "amber", 20, None),
	("Send to the Registry", "Ongoing", "blue", 40, "stage_send"),
	("At the Registry", "Ongoing", "cyan", 50, "stage_at_registry"),
	("Notified by the Registry", "Ongoing", "violet", 60, "stage_notified"),
	("Protested", "Ongoing", "red", 40, "stage_protested"),
	("Paid at the Registry", "Won", "green", 100, "stage_paid"),
	("Agreement / Withdrawn", "Won", "teal", 100, "stage_agreement"),
)
# IP de saída do servidor do HubTech: o IEPTB só aceita chamadas de IPs cadastrados pelo cliente no contrato
OUTBOUND_IP = "2.31.80.97"
_TOKEN_KEY = "cred_hub:cenprot_token"
TOKEN_SECONDS = 1800


class TitleIncomplete(Exception):
	"""Falta dado para montar o título: nada foi enviado."""


def forget_token():
	frappe.cache.delete_keys(_TOKEN_KEY)


def _settings():
	settings = get_settings()
	if not settings.cenprot_enabled:
		frappe.throw(_("CENPROT is off. An admin turns it on in Settings > Integrations > Bureaus."))
	return settings


def _token_key(settings) -> str:
	digest = hashlib.sha256(f"{settings.cenprot_environment}:{settings.cenprot_username}".encode()).hexdigest()[:16]
	return f"{_TOKEN_KEY}:{digest}"


def _client(settings) -> CenprotClient:
	return CenprotClient(
		settings.cenprot_username,
		secret(settings, "cenprot_password"),
		settings.cenprot_environment,
		presenter=settings.cenprot_presenter_code,
		token=frappe.cache.get_value(_token_key(settings)) or "",
	)


def _remember(settings, client):
	if client.token:
		frappe.cache.set_value(_token_key(settings), client.token, expires_in_sec=TOKEN_SECONDS)


# Funil -------------------------------------------------------------------------------------------------------


def create_collection_pipeline(settings):
	"""Funil "Debt Collection" (Cobrança) com as 8 etapas e os campos de protesto; quem ligou a CENPROT fica como dono."""
	pipeline = frappe.new_doc("CRM Pipeline")
	pipeline.pipeline_name = COLLECTION_PIPELINE
	pipeline.color = "orange"
	pipeline.is_active = 1
	user = frappe.session.user
	if user not in ("Administrator", "Guest"):
		pipeline.append("members", {"user": user, "role": "Owner"})
	meta = frappe.get_meta(DEAL)
	for position, fieldname in enumerate(f for f in DEAL_FORM_FIELDS if meta.has_field(f)):
		pipeline.append("fields", {"block": "Card", "fieldname": fieldname, "position": position + 1})
	pipeline.insert(ignore_permissions=True)
	for position, (label, kind, color, probability, setting) in enumerate(COLLECTION_STAGES, start=1):
		stage = frappe.get_doc(
			{
				"doctype": "CRM Deal Status",
				"pipeline": pipeline.name,
				"deal_status": label,
				"type": kind,
				"color": color,
				"probability": probability,
				"position": position,
			}
		).insert(ignore_permissions=True)
		if setting:
			settings.set(setting, stage.name)
	settings.collection_pipeline = pipeline.name
	return pipeline.name


# Título ------------------------------------------------------------------------------------------------------


def _split_street(line: str) -> tuple[str, str]:
	"""'RUA DAS INDUSTRIAS, 500' -> ('RUA DAS INDUSTRIAS', '500')."""
	line = (line or "").strip()
	street, _sep, number = line.rpartition(",")
	if street and only_digits(number) and len(number.strip()) <= 10:
		return street.strip(), number.strip()
	return line, ""


def _address_party(address, neighborhood="", city="", state="") -> dict:
	street, number = _split_street(address.address_line1 if address else "")
	line2 = (address.address_line2 if address else "") or ""
	district = (neighborhood or "").strip()
	parts = [p.strip() for p in line2.split(",") if p.strip()]
	if not district and parts:
		district = parts[-1]
		parts = parts[:-1]
	complement = ", ".join(p for p in parts if p.lower() != district.lower())
	return {
		"endereco": street,
		"numero": number,
		"complemento": complement,
		"bairro": district,
		"municipio": city or (address.city if address else ""),
		"uf": state or (address.state if address else ""),
		"cep": only_digits(address.pincode if address else ""),
	}


def _contact_address(contact_name: str):
	name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Contact", "link_name": contact_name, "parenttype": "Address"},
		"parent",
	)
	return frappe.get_doc("Address", name) if name else None


def _primary_contact(deal) -> str | None:
	rows = deal.get("contacts") or []
	primary = next((row.contact for row in rows if row.get("is_primary")), None)
	return primary or next((row.contact for row in rows), None) or deal.get("contact")


def debtor_of(deal) -> dict:
	"""Devedor: a Organização do card com CNPJ ou, sem ela, o contato principal com CPF."""
	if deal.get("organization"):
		org = frappe.get_doc("CRM Organization", deal.organization)
		if is_valid_cnpj(org.get("cnpj")):
			address = frappe.get_doc("Address", org.address) if org.get("address") else None
			party = _address_party(address, org.get("neighborhood"), org.get("city"), org.get("state"))
			party.update(
				documento=only_digits(org.cnpj),
				nome=org.organization_name,
				fone=org.get("registry_phone"),
				email=org.get("registry_email"),
			)
			return party
	contact_name = _primary_contact(deal)
	if contact_name:
		contact = frappe.get_doc("Contact", contact_name)
		if len(only_digits(contact.get("cpf"))) == 11:
			party = _address_party(_contact_address(contact.name))
			party.update(
				documento=only_digits(contact.cpf),
				nome=contact.full_name,
				fone=contact.mobile_no or contact.phone,
				email=contact.email_id,
			)
			return party
	raise TitleIncomplete(
		_("The card needs an organization with CNPJ, or a primary contact with CPF, to be the debtor.")
	)


def _creditor(settings) -> dict:
	return {
		"codigo": settings.cenprot_presenter_code,
		"nome": settings.creditor_name,
		"documento": settings.creditor_document,
		"endereco": settings.creditor_address,
		"numero": settings.creditor_number,
		"complemento": settings.creditor_complement,
		"bairro": settings.creditor_district,
		"municipio": settings.creditor_city,
		"uf": settings.creditor_state,
		"cep": settings.creditor_zip,
	}


def _missing(party: dict, labels: dict) -> list[str]:
	missing = [label for key, label in labels.items() if not str(party.get(key) or "").strip()]
	if party.get("cep") and len(only_digits(party["cep"])) != 8:
		missing.append(labels["cep"])
	return missing


def address_labels_() -> dict:
	return {
		"endereco": _("street"),
		"cep": _("ZIP code (CEP)"),
		"municipio": _("city"),
		"uf": _("state (UF)"),
		"bairro": _("neighborhood"),
	}


def species_missing() -> str:
	return _("Choose the title species (DMI, DSI, CCB...): it has no default.")


def drawer_of(deal) -> dict | None:
	"""Sacador do título: o cedente do card (segunda organização), que emitiu o título antecipado. Vai no
	<sacador> do EnviarTitulo (manual v2.2: o <cedente> é o portador/apresentante). None sem cedente."""
	name = deal.get("second_organization")
	if not name or name == deal.get("organization"):
		return None
	org = frappe.get_doc("CRM Organization", name)
	address = frappe.get_doc("Address", org.address) if org.get("address") else None
	party = _address_party(address, org.get("neighborhood"), org.get("city"), org.get("state"))
	party.update(documento=only_digits(org.get("cnpj")), nome=org.organization_name)
	return party


def title_keys(deal, settings) -> dict:
	"""Chaves do título no card; número e "nosso número" vazios recebem os dígitos do código do card. A espécie
	nunca tem padrão: sem ela não há protesto (dono, 02/11/2026)."""
	code = only_digits(deal.name)
	species = (deal.get("protest_species") or "").strip().upper()
	amount = flt(deal.get("protest_amount")) or flt(deal.get("deal_value"))
	return {
		"protest_species": species,
		"protest_title_number": (deal.get("protest_title_number") or code[-11:]).strip(),
		"protest_our_number": (deal.get("protest_our_number") or code[-15:]).strip(),
		"protest_amount": amount,
		"protest_balance": flt(deal.get("protest_balance")) or amount,
		"protest_declaration": deal.get("protest_declaration") or DECLARATION_FOR_SPECIES.get(species, "A"),
	}


def build_title(deal, settings) -> tuple[dict, dict]:
	"""(título para a CENPROT, valores a gravar no card). TitleIncomplete com tudo o que falta."""
	keys = title_keys(deal, settings)
	debtor = debtor_of(deal)
	creditor = _creditor(settings)
	problems = [] if keys["protest_species"] else [species_missing()]
	address_labels = address_labels_()
	missing = _missing(debtor, address_labels)
	if missing:
		problems.append(_("Debtor address without: {0}.").format(", ".join(missing)))
	missing = _missing(creditor, {"nome": _("name"), "documento": _("CPF or CNPJ"), **address_labels})
	if missing:
		problems.append(_("Creditor in the settings without: {0}.").format(", ".join(missing)))
	if not keys["protest_title_number"] or not keys["protest_our_number"]:
		problems.append(_("Fill the title number and our number."))
	if keys["protest_amount"] <= 0:
		problems.append(_("Fill the title amount (or the card value)."))
	issue, due = deal.get("protest_issue_date"), deal.get("protest_due_date")
	if not issue:
		problems.append(_("Fill the issue date."))
	if not due:
		problems.append(_("Fill the due date."))
	elif getdate(due) >= getdate(today()):
		# a CENPROT recusa título ainda não vencido
		problems.append(_("Only overdue titles go to protest: the due date must be before today."))
	if problems:
		raise TitleIncomplete(" ".join(problems))
	title = {
		"cedente": creditor,
		"devedores": [debtor],
		"divida": {
			"especie": keys["protest_species"],
			"numero": keys["protest_title_number"],
			"nossoNumero": keys["protest_our_number"],
			"valor": keys["protest_amount"],
			"saldo": keys["protest_balance"],
			"tipoEndosso": ENDORSEMENT.get(settings.default_endorsement or ""),
			"aceite": "S" if settings.titles_accepted else "N",
			"declaracaoPortador": keys["protest_declaration"],
			"emissao": br_date(issue),
			"vencimento": br_date(due),
		},
	}
	if settings.cenprot_bearer_code:
		title["geral"] = {"codPortador": settings.cenprot_bearer_code, "nomePortador": settings.cenprot_bearer_name}
	keys["protest_debtor_document"] = debtor["documento"]
	return title, keys


def _consult_title(deal) -> dict:
	"""As chaves que o título recebeu no envio (gravadas no card)."""
	due = deal.get("protest_due_date")
	return {
		"devedor": {"documento": deal.get("protest_debtor_document"), "nome": ""},
		"divida": {
			"numero": deal.get("protest_title_number"),
			"nossoNumero": deal.get("protest_our_number"),
			"especie": deal.get("protest_species"),
			"vencimento": br_date(due) if due else AT_SIGHT,
			"emissao": br_date(deal.get("protest_issue_date")),
		},
		"protocolo": deal.get("protest_registry_protocol"),
	}


# Card --------------------------------------------------------------------------------------------------------


def _note(deal, text: str):
	deal.add_comment("Comment", text)


def _notify(deal, text: str):
	"""Notificação do CRM para o responsável pelo card."""
	user = deal.get("deal_owner")
	if not user or user == "Guest":
		return
	frappe.get_doc(
		{
			"doctype": "CRM Notification",
			"from_user": frappe.session.user,
			"to_user": user,
			"type": "Automation",
			"notification_text": text,
			"message": f"<p>{escape(text)}</p>",
			"notification_type_doctype": DEAL,
			"notification_type_doc": deal.name,
			"reference_doctype": DEAL,
			"reference_name": deal.name,
		}
	).insert(ignore_permissions=True)


def _move(deal, settings, stage: str | None):
	"""Leva o card do funil de Cobrança para `stage` (sem disparar de novo o gatilho de etapa)."""
	if not stage or deal.pipeline != settings.collection_pipeline:
		return
	deal = frappe.get_doc(DEAL, deal.name)
	if deal.status == stage:
		return
	deal.status = stage
	deal.flags.cred_hub_protest = True
	frappe.db.savepoint("cred_hub_protest_move")
	try:
		deal.save(ignore_permissions=True)
	except frappe.ValidationError as e:
		frappe.db.rollback(save_point="cred_hub_protest_move")
		frappe.clear_messages()
		label = frappe.db.get_value("CRM Deal Status", stage, "deal_status") or stage
		_note(deal, _("The card could not move to {0} by itself: {1}").format(_(label), e))


def _save_keys(deal, values: dict):
	deal.db_set(values, update_modified=False)


def send_title(deal) -> dict:
	"""Envia o título do card. Erro de dado (TitleIncomplete) sobe antes de qualquer chamada."""
	settings = _settings()
	status = (deal.get("protest_status") or "").upper()
	if status not in SENDABLE:
		return {"ok": False, "message": _("This title is already at the registry (status {0}).").format(status)}
	title, keys = build_title(deal, settings)
	client = _client(settings)
	try:
		result = client.send_title(title)
	except CenprotError as e:
		bureau_log.record(
			PROVIDER, SEND, bureau_log.ERROR, document=keys["protest_debtor_document"], reference=deal,
			result_code=e.code, error=str(e),
		)
		_save_keys(deal, {"protest_message": str(e)[:500]})
		return {"ok": False, "message": str(e)}
	finally:
		_remember(settings, client)
	accepted = result["ok"] or result["duplicate"]
	bureau_log.record(
		PROVIDER,
		SEND,
		bureau_log.SUCCESS if accepted else bureau_log.ERROR,
		document=keys["protest_debtor_document"],
		reference=deal,
		result_code=result["code"],
		protocol=result.get("protocol") or "",
		response=result["raw"],
		error="" if accepted else result["message"],
	)
	message = " ".join(filter(None, (result["code"], result["message"])))
	if not accepted:
		_save_keys(deal, {"protest_message": message[:500]})
		_note(deal, _("CENPROT refused the title: {0}").format(message))
		return {"ok": False, "message": _("CENPROT refused the title: {0}").format(message)}
	keys.update(
		protest_status="COLETADO",
		protest_message=message[:500],
		protest_sent_at=now_datetime(),
		protest_registry_protocol=result.get("protocol") or deal.get("protest_registry_protocol") or "",
	)
	_save_keys(deal, keys)
	if result["duplicate"]:
		_note(deal, _("CENPROT already had this title ({0}). Checking its status at the registry.").format(message))
		refresh_title(deal, settings, client)
	else:
		_note(deal, _("Title sent to protest at CENPROT: {0}").format(message))
	_move(deal, settings, settings.stage_send)
	return {"ok": True, "message": _("Title sent to protest."), "status": deal.get("protest_status")}


def refresh_title(deal, settings=None, client=None) -> dict:
	"""Consulta o título; mudou o status, grava, anda com o card e avisa o responsável."""
	settings = settings or _settings()
	if not deal.get("protest_sent_at"):
		return {"ok": False, "message": _("This card was not sent to protest.")}
	own_client = client is None
	client = client or _client(settings)
	try:
		result = client.consult_title(_consult_title(deal))
	except CenprotError as e:
		bureau_log.record(
			PROVIDER, CONSULT, bureau_log.ERROR, document=deal.get("protest_debtor_document"), reference=deal,
			result_code=e.code, error=str(e),
		)
		return {"ok": False, "message": str(e)}
	finally:
		if own_client:
			_remember(settings, client)
	found = result["title"]
	checked = {"protest_checked_at": now_datetime()}
	if not found or not found["status"]:
		_save_keys(deal, checked)
		message = " ".join(filter(None, (result["code"], result["message"]))) or _("no answer")
		return {"ok": False, "message": _("CENPROT did not find the title: {0}").format(message)}
	previous = (deal.get("protest_status") or "").upper()
	status = found["status"]
	checked.update(
		protest_status=status,
		protest_message=(found["message"] or "")[:500],
		protest_registry_protocol=found["protocol"] or deal.get("protest_registry_protocol") or "",
		protest_registry_office=(found["registry"] or deal.get("protest_registry_office") or "")[:60],
	)
	_save_keys(deal, checked)
	if status != previous:
		bureau_log.record(
			PROVIDER, CONSULT, bureau_log.SUCCESS, document=deal.get("protest_debtor_document"), reference=deal,
			result_code=status, protocol=found["protocol"], response=result["raw"],
		)
		text = _("Protest of {0}: the registry status changed from {1} to {2}.").format(
			deal.name, previous or "-", status
		)
		if found["message"]:
			text += " " + found["message"]
		_note(deal, text)
		_notify(deal, text)
		setting = STAGE_FOR_STATUS.get(status)
		if setting:
			_move(deal, settings, settings.get(setting))
	return {"ok": True, "status": status, "changed": status != previous, "message": found["message"]}


def withdraw_title(deal, justification: str = "") -> dict:
	settings = _settings()
	status = (deal.get("protest_status") or "").upper()
	operation = OPERATION_FOR_STATUS.get(status)
	if not operation:
		return {"ok": False, "message": _("A title in status {0} cannot be withdrawn or cancelled.").format(status or "-")}
	client = _client(settings)
	try:
		result = client.operate_title(_consult_title(deal), operation, justification)
	except CenprotError as e:
		bureau_log.record(
			PROVIDER, OPERATE, bureau_log.ERROR, document=deal.get("protest_debtor_document"), reference=deal,
			result_code=e.code, error=str(e),
		)
		return {"ok": False, "message": str(e)}
	finally:
		_remember(settings, client)
	message = " ".join(filter(None, (result["code"], result["message"])))
	bureau_log.record(
		PROVIDER,
		OPERATE,
		bureau_log.SUCCESS if result["ok"] else bureau_log.ERROR,
		document=deal.get("protest_debtor_document"),
		reference=deal,
		result_code=result["code"],
		response=result["raw"],
		error="" if result["ok"] else message,
	)
	_save_keys(deal, {"protest_message": message[:500]})
	if not result["ok"]:
		_note(deal, _("CENPROT refused the {0} of the title: {1}").format(operation, message))
		return {"ok": False, "message": _("CENPROT refused the {0} of the title: {1}").format(operation, message)}
	_note(deal, _("CENPROT accepted the {0} of the title: {1}").format(operation, message))
	return {"ok": True, "operation": operation, "message": message}


# Telas (whitelist) -------------------------------------------------------------------------------------------


def _deal(name: str, ptype: str):
	deal = frappe.get_doc(DEAL, name)
	deal.check_permission(ptype)
	return deal


@frappe.whitelist()
def get_status(deal: str) -> dict:
	doc = _deal(deal, "read")
	settings = get_settings()
	if not settings.cenprot_enabled:
		return {"enabled": False}
	status = (doc.get("protest_status") or "").upper()
	return {
		"enabled": True,
		"in_pipeline": bool(settings.collection_pipeline) and doc.pipeline == settings.collection_pipeline,
		"status": status,
		"message": doc.get("protest_message") or "",
		"sent_at": doc.get("protest_sent_at"),
		"checked_at": doc.get("protest_checked_at"),
		"protocol": doc.get("protest_registry_protocol") or "",
		"can_send": status in SENDABLE,
		"can_withdraw": status in OPERATION_FOR_STATUS,
		"can_write": bool(frappe.has_permission(DEAL, "write", doc)),
	}


@frappe.whitelist(methods=["POST"])
def send(deal: str) -> dict:
	doc = _deal(deal, "write")
	try:
		return send_title(doc)
	except TitleIncomplete as e:
		frappe.throw(str(e), title=_("The title cannot go to protest yet"))


@frappe.whitelist(methods=["POST"])
def refresh(deal: str) -> dict:
	return refresh_title(_deal(deal, "read"))


@frappe.whitelist(methods=["POST"])
def withdraw(deal: str, justification: str | None = None) -> dict:
	return withdraw_title(_deal(deal, "write"), (justification or "").strip())


@frappe.whitelist()
def server_ip() -> dict:
	"""O IP que o cliente cadastra no IEPTB (tela de Bureaus)."""
	frappe.only_for("System Manager")
	return {"ip": frappe.conf.get("credhub_outbound_ip") or OUTBOUND_IP}


@frappe.whitelist(methods=["POST"])
def test_connection() -> dict:
	"""Só autentica (não envia nem consulta título)."""
	frappe.only_for("System Manager")
	settings = get_settings()
	client = _client(settings)
	try:
		client.authenticate()
	except CenprotError as e:
		return {"ok": False, "message": str(e)}
	_remember(settings, client)
	return {"ok": True, "message": _("CENPROT accepted the login.")}


# Gatilhos e rotina -------------------------------------------------------------------------------------------


def on_deal_update(doc, method=None):
	"""Card entrou na etapa de envio ou na de acordo, com a opção ligada: age em segundo plano. As duas opções
	começam desligadas: o cartório cobra custas e quem decide gastar é o cliente (dono, 30/10/2026)."""
	if doc.flags.cred_hub_protest or not doc.has_value_changed("status"):
		return
	settings = get_settings()
	if not settings.cenprot_enabled or not settings.collection_pipeline or doc.pipeline != settings.collection_pipeline:
		return
	status = (doc.get("protest_status") or "").upper()
	if doc.status == settings.stage_send and cint(settings.auto_send_on_stage) and status in SENDABLE:
		job = "send_in_background"
	elif doc.status == settings.stage_agreement and cint(settings.auto_withdraw_on_stage) and status in OPERATION_FOR_STATUS:
		job = "withdraw_in_background"
	else:
		return
	frappe.enqueue(
		f"cred_hub.integrations.protest.{job}",
		queue="short",
		job_id=f"cred_hub_protest::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		deal=doc.name,
	)


def send_in_background(deal: str):
	doc = frappe.get_doc(DEAL, deal)
	try:
		result = send_title(doc)
	except TitleIncomplete as e:
		result = {"ok": False, "message": str(e)}
		_note(doc, _("The title did not go to protest: {0}").format(e))
	if not result["ok"]:
		_notify(doc, _("Protest of {0} was not sent: {1}").format(doc.name, result["message"]))


def withdraw_in_background(deal: str):
	doc = frappe.get_doc(DEAL, deal)
	result = withdraw_title(doc, _("Commercial agreement"))
	if not result["ok"]:
		_notify(doc, _("Protest of {0} was not withdrawn: {1}").format(doc.name, result["message"]))


def sync_all():
	"""Todo dia: atualiza os títulos em andamento no cartório."""
	settings = get_settings()
	if not settings.cenprot_enabled:
		return
	names = frappe.get_all(
		DEAL,
		filters={"protest_sent_at": ("is", "set"), "protest_status": ("not in", FINAL)},
		pluck="name",
	)
	has_rows = frappe.db.exists(TITLE_ROW, {"sent_at": ("is", "set"), "protest_status": ("not in", FINAL)})
	if not names and not has_rows:
		return
	client = _client(settings)
	if has_rows:
		sync_rows(settings, client)
	for name in names:
		try:
			refresh_title(frappe.get_doc(DEAL, name), settings, client)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Cred Hub: CENPROT status of {name} failed")
	_remember(settings, client)


# Títulos do card (tabela CRM Deal Title, credtech#315) ----------------------------------------------------------
#
# Cada linha da aba Títulos é um título próprio na CENPROT: envio, consulta diária e retirada por linha. O status
# da linha só vale quando é um status da CENPROT; textos da tela ou da importação (ex.: "Pendente") contam como
# "ainda não enviado".

TITLE_ROW = "CRM Deal Title"
# Espécies que o cartório só aceita com o documento (manual v2.2, "Espécies permitidas")
# (DM/DS sem aceite também: NF + comprovante de entrega; mesma lista da tela)
SPECIES_WITH_DOCUMENT = frozenset({"CCB", "CH", "NP", "CD", "CPS", "CA", "CL", "CM", "TA", "CT", "DM", "DS"})


def row_status(row) -> str:
	status = (row.get("protest_status") or "").strip().upper()
	return status if status in KNOWN_STATUSES else ""


def _our_number_for(deal, row) -> str:
	"""Nosso número gerado quando o título não tem: dígitos do card + posição da linha (estável e único no card)."""
	return f"{only_digits(deal.name)[-12:]}{cint(row.idx):03d}"[-OUR_NUMBER_SIZE:]


def _find_row(deal, row_name: str):
	return next((row for row in deal.get("titles") or [] if row.name == row_name), None)


DOCUMENT_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_EXTENSIONS = ("pdf", "jpg", "jpeg", "png")


def _document_of(row) -> tuple[dict | None, str]:
	"""(<documento> do EnviarTitulo, problema) a partir do anexo da linha (manual v2.2: até 5 MB)."""
	url = row.get("attachment")
	if not url:
		return None, ""
	name = frappe.db.get_value("File", {"file_url": url}, "name")
	if not name:
		return None, _("The attachment of title {0} was not found.").format(row.get("title_number") or "")
	file = frappe.get_doc("File", name)
	extension = (file.file_name or url).rsplit(".", 1)[-1].lower()
	if extension not in DOCUMENT_EXTENSIONS:
		return None, _("The attachment must be PDF, JPG or PNG (CENPROT).")
	content = file.get_content()
	if isinstance(content, str):
		content = content.encode()
	if len(content) > DOCUMENT_MAX_BYTES:
		return None, _("The attachment is larger than 5 MB, the CENPROT limit.")
	extension = "jpg" if extension == "jpeg" else extension
	return {"extensao": extension, "base64": base64.b64encode(content).decode()}, ""


def _row_keys(deal, row) -> dict:
	species = (row.get("species") or "").strip().upper()
	number = (row.get("title_number") or "").strip()
	return {
		"species": species,
		"number": "" if number.upper() == "S/N" else number,
		"our_number": (row.get("our_number") or "").strip() or _our_number_for(deal, row),
		"amount": flt(row.get("original_amount")),
		"balance": flt(row.get("updated_amount")) or flt(row.get("original_amount")),
	}


def build_title_from_row(deal, row, settings) -> tuple[dict, dict]:
	"""(título para a CENPROT, chaves usadas) a partir de uma linha da aba Títulos. TitleIncomplete com tudo o que
	falta, antes de qualquer chamada paga."""
	keys = _row_keys(deal, row)
	problems = [] if keys["species"] else [species_missing()]
	if not keys["number"]:
		problems.append(_("Fill the title number."))
	if keys["amount"] <= 0:
		problems.append(_("Fill the title amount."))
	issue, due = row.get("issue_date"), row.get("due_date")
	if not issue:
		problems.append(_("Fill the issue date."))
	if not due:
		problems.append(_("Fill the due date."))
	elif getdate(due) >= getdate(today()):
		problems.append(_("Only overdue titles go to protest: the due date must be before today."))
	if keys["species"] in SPECIES_WITH_DOCUMENT and not row.get("attachment"):
		problems.append(
			_("Title of species {0} requires an attachment (PDF/document) before protesting.").format(keys["species"])
		)
	document, problem = _document_of(row)
	if problem:
		problems.append(problem)

	try:
		debtor = debtor_of(deal)
	except TitleIncomplete as e:
		problems.append(str(e))
		debtor = None
	address_labels = address_labels_()
	if debtor:
		missing = _missing(debtor, address_labels)
		if missing:
			problems.append(_("Debtor address without: {0}.").format(", ".join(missing)))
	creditor = _creditor(settings)
	missing = _missing(creditor, {"nome": _("name"), "documento": _("CPF or CNPJ"), **address_labels})
	if missing:
		problems.append(_("Creditor in the settings without: {0}.").format(", ".join(missing)))
	drawer = drawer_of(deal)
	if drawer:
		missing = _missing(drawer, {"nome": _("name"), "documento": _("CNPJ"), **address_labels})
		if missing:
			problems.append(_("Cedente (drawer of the title) without: {0}.").format(", ".join(missing)))
	if problems:
		raise TitleIncomplete(" ".join(problems))

	title = {
		"cedente": creditor,
		"devedores": [debtor],
		"divida": {
			"especie": keys["species"],
			"numero": keys["number"],
			"nossoNumero": keys["our_number"],
			"valor": keys["amount"],
			"saldo": keys["balance"],
			"tipoEndosso": ENDORSEMENT.get(settings.default_endorsement or ""),
			"aceite": "S" if settings.titles_accepted else "N",
			# Com anexo vai a imagem do título ("I"); sem anexo, a declaração da espécie
			"declaracaoPortador": "I" if document else DECLARATION_FOR_SPECIES.get(keys["species"], "A"),
			"emissao": br_date(issue),
			"vencimento": br_date(due),
		},
	}
	if document:
		title["divida"]["documento"] = document
	if drawer:
		title["sacador"] = drawer
	if settings.cenprot_bearer_code:
		title["geral"] = {"codPortador": settings.cenprot_bearer_code, "nomePortador": settings.cenprot_bearer_name}
	keys["debtor_document"] = debtor["documento"]
	return title, keys


def _row_consult(deal, row) -> dict:
	"""As chaves com que a linha foi enviada (número, nosso número, vencimento e o documento do devedor)."""
	debtor = debtor_of(deal)
	return {
		"devedor": {"documento": debtor["documento"], "nome": ""},
		"divida": {
			"numero": row.get("title_number"),
			"nossoNumero": row.get("our_number"),
			"especie": row.get("species"),
			"vencimento": br_date(row.get("due_date")) if row.get("due_date") else AT_SIGHT,
			"emissao": br_date(row.get("issue_date")),
		},
		"protocolo": row.get("protest_protocol"),
	}


def _set_row(row, values: dict):
	frappe.db.set_value(TITLE_ROW, row.name, values, update_modified=False)
	row.update(values)


def send_deal_title_row(deal, row_name: str) -> dict:
	settings = _settings()
	row = _find_row(deal, row_name)
	if not row:
		return {"ok": False, "message": _("Title row not found.")}
	status = row_status(row)
	if status not in SENDABLE:
		return {"ok": False, "message": _("This title is already at the registry (status {0}).").format(status)}

	title, keys = build_title_from_row(deal, row, settings)
	client = _client(settings)
	try:
		result = client.send_title(title)
	except CenprotError as e:
		bureau_log.record(
			PROVIDER, SEND, bureau_log.ERROR, document=keys["debtor_document"], reference=deal,
			result_code=e.code, error=str(e),
		)
		return {"ok": False, "message": str(e)}
	finally:
		_remember(settings, client)

	accepted = result["ok"] or result["duplicate"]
	bureau_log.record(
		PROVIDER,
		SEND,
		bureau_log.SUCCESS if accepted else bureau_log.ERROR,
		document=keys["debtor_document"],
		reference=deal,
		result_code=result["code"],
		protocol=result.get("protocol") or "",
		response=result["raw"],
		error="" if accepted else result["message"],
	)
	message = " ".join(filter(None, (result["code"], result["message"])))
	if not accepted:
		_note(deal, _("CENPROT refused title {0}: {1}").format(keys["number"], message))
		return {"ok": False, "message": _("CENPROT refused the title: {0}").format(message)}

	# Grava as chaves usadas no envio: a consulta diária e a retirada precisam das mesmas
	_set_row(
		row,
		{
			"title_number": keys["number"],
			"our_number": keys["our_number"],
			"species": keys["species"],
			"protest_status": "COLETADO",
			"protest_protocol": result.get("protocol") or row.get("protest_protocol") or "",
			"sent_at": now_datetime(),
		},
	)
	if result["duplicate"]:
		_note(deal, _("CENPROT already had title {0} ({1}). Checking its status at the registry.").format(keys["number"], message))
		refresh_title_row(deal, row, settings, client)
	else:
		_note(deal, _("Title {0} sent to protest at CENPROT: {1}").format(keys["number"], message))
	return {"ok": True, "message": _("Title sent to protest."), "status": row.get("protest_status")}


def refresh_title_row(deal, row, settings=None, client=None) -> dict:
	"""Consulta o título da linha; mudou o status, grava, registra no card e avisa o responsável."""
	settings = settings or _settings()
	if not row.get("sent_at"):
		return {"ok": False, "message": _("This title was not sent to protest.")}
	own_client = client is None
	client = client or _client(settings)
	try:
		consult = _row_consult(deal, row)
		result = client.consult_title(consult)
	except (CenprotError, TitleIncomplete) as e:
		bureau_log.record(
			PROVIDER, CONSULT, bureau_log.ERROR, reference=deal, result_code=getattr(e, "code", ""), error=str(e)
		)
		return {"ok": False, "message": str(e)}
	finally:
		if own_client:
			_remember(settings, client)
	found = result["title"]
	if not found or not found["status"]:
		message = " ".join(filter(None, (result["code"], result["message"]))) or _("no answer")
		return {"ok": False, "message": _("CENPROT did not find the title: {0}").format(message)}
	previous = row_status(row)
	status = found["status"]
	_set_row(row, {"protest_status": status, "protest_protocol": found["protocol"] or row.get("protest_protocol") or ""})
	if status != previous:
		bureau_log.record(
			PROVIDER, CONSULT, bureau_log.SUCCESS, document=consult["devedor"]["documento"], reference=deal,
			result_code=status, protocol=found["protocol"], response=result["raw"],
		)
		text = _("Protest of title {0} ({1}): the registry status changed from {2} to {3}.").format(
			row.get("title_number"), deal.name, previous or "-", status
		)
		if found["message"]:
			text += " " + found["message"]
		_note(deal, text)
		_notify(deal, text)
	return {"ok": True, "status": status, "changed": status != previous, "message": found["message"]}


def withdraw_title_row(deal, row_name: str, justification: str = "") -> dict:
	"""Tira o título da linha do cartório pela operação que o status permite (manual v2.2): REMOCAO antes de ir
	ao cartório, DESISTENCIA confirmado e CANCELAMENTO protestado."""
	settings = _settings()
	row = _find_row(deal, row_name)
	if not row:
		return {"ok": False, "message": _("Title row not found.")}
	status = row_status(row)
	operation = OPERATION_FOR_STATUS.get(status)
	if not operation:
		return {"ok": False, "message": _("A title in status {0} cannot be withdrawn or cancelled.").format(status or "-")}
	client = _client(settings)
	try:
		consult = _row_consult(deal, row)
		result = client.operate_title(consult, operation, justification)
	except (CenprotError, TitleIncomplete) as e:
		bureau_log.record(
			PROVIDER, OPERATE, bureau_log.ERROR, reference=deal, result_code=getattr(e, "code", ""), error=str(e)
		)
		return {"ok": False, "message": str(e)}
	finally:
		_remember(settings, client)
	message = " ".join(filter(None, (result["code"], result["message"])))
	bureau_log.record(
		PROVIDER,
		OPERATE,
		bureau_log.SUCCESS if result["ok"] else bureau_log.ERROR,
		document=consult["devedor"]["documento"],
		reference=deal,
		result_code=result["code"],
		response=result["raw"],
		error="" if result["ok"] else message,
	)
	if not result["ok"]:
		_note(deal, _("CENPROT refused the {0} of title {1}: {2}").format(operation, row.get("title_number"), message))
		return {"ok": False, "message": _("CENPROT refused the {0} of the title: {1}").format(operation, message)}
	_note(deal, _("CENPROT accepted the {0} of title {1}: {2}").format(operation, row.get("title_number"), message))
	if operation == "REMOCAO":
		# Removido antes de ir ao cartório: a linha volta a "não enviado" e pode ser enviada de novo
		_set_row(row, {"protest_status": "", "protest_protocol": "", "sent_at": None})
	else:
		# Desistência e cancelamento: o status novo vem da consulta à CENPROT
		refresh_title_row(deal, row, settings)
	return {"ok": True, "operation": operation, "message": message, "status": row.get("protest_status")}


def sync_rows(settings, client):
	"""Rotina diária: atualiza as linhas enviadas que ainda estão em andamento no cartório."""
	rows = frappe.get_all(
		TITLE_ROW,
		filters={"parenttype": DEAL, "sent_at": ("is", "set"), "protest_status": ("not in", FINAL)},
		fields=["name", "parent"],
	)
	for parent in sorted({row.parent for row in rows}):
		try:
			deal = frappe.get_doc(DEAL, parent)
			for row in deal.get("titles") or []:
				if row.get("sent_at") and row_status(row) not in FINAL:
					refresh_title_row(deal, row, settings, client)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Cred Hub: CENPROT status of the titles of {parent} failed")


@frappe.whitelist(methods=["POST"])
def send_row(deal: str, title_row: str) -> dict:
	doc = _deal(deal, "write")
	try:
		return send_deal_title_row(doc, title_row)
	except TitleIncomplete as e:
		frappe.throw(str(e), title=_("The title cannot go to protest yet"))


@frappe.whitelist(methods=["POST"])
def refresh_row(deal: str, title_row: str) -> dict:
	doc = _deal(deal, "read")
	row = _find_row(doc, title_row)
	if not row:
		return {"ok": False, "message": _("Title row not found.")}
	return refresh_title_row(doc, row)


@frappe.whitelist(methods=["POST"])
def withdraw_row(deal: str, title_row: str, justification: str | None = None) -> dict:
	return withdraw_title_row(_deal(deal, "write"), title_row, (justification or "").strip())
