"""Funil de Cobrança com protesto no cartório pela CENPROT (credtech#290, módulo 2).

Ao ligar a CENPROT nas configurações, o site ganha o funil "Debt Collection" (Cobrança) com 6 etapas e os campos de
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
from crm.fcrm.doctype.crm_deal_title.crm_deal_title import balance_of
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from cred_hub.billing import CREDIT_AND_COLLECTION, automation_allowed, plan_allows, requires_feature
from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits
from cred_hub.integrations import bureau_log, get_settings, secret
from cred_hub.integrations.cenprot import (
	AT_SIGHT,
	DEBTOR_SIZES,
	KNOWN_STATUSES,
	OPERATION_FOR_STATUS,
	OUR_NUMBER_SIZE,
	PARTY_SIZES,
	TITLE_NUMBER_SIZE,
	CenprotClient,
	CenprotError,
	ascii_text,
	br_date,
	describe,
	truncations,
)
from cred_hub.integrations.fields import DEAL_FORM_FIELDS
from cred_hub.party.document import format_cpf, is_valid_cpf

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
# The 6 stages approved by the owner (02/11/2026). Won and Lost are not stages: they are the card status.
# Only the registry stages are tied to a setting (the card moves there by itself when CENPROT answers); the other
# stage settings (send, notified, paid, agreement) stay empty and a client may still pick stages for them.
COLLECTION_STAGES = (
	("Backlog", "Open", "gray", 10, None),
	("In negotiation", "Ongoing", "amber", 30, None),
	("Registry Alert", "Ongoing", "green", 40, None),
	("In the Registry", "Ongoing", "pink", 50, "stage_at_registry"),
	("Protested", "Ongoing", "orange", 60, "stage_protested"),
	("Legal / Lawsuit", "Ongoing", "purple", 40, None),
)
# IP de saída do servidor do HubTech: o IEPTB só aceita chamadas de IPs cadastrados pelo cliente no contrato
OUTBOUND_IP = "2.31.80.97"
_TOKEN_KEY = "cred_hub:cenprot_token"
TOKEN_SECONDS = 1800


class TitleIncomplete(Exception):
	"""Falta dado para montar o título: nada foi enviado. `problems` traz cada falta em separado."""

	def __init__(self, problems):
		self.problems = [problems] if isinstance(problems, str) else list(problems)
		super().__init__(" ".join(self.problems))


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
	"""Funil "Debt Collection" (Cobrança) com as 6 etapas e os campos de protesto; quem ligou a CENPROT fica como dono."""
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


@frappe.whitelist()
def collection_funnel_status() -> dict:
	"""O que a tela de Políticas de Crédito & Compliance mostra sobre o funil de Cobrança (sem segredos da CENPROT)."""
	frappe.only_for(["System Manager", "Sales Manager"])
	settings = get_settings()
	pipeline = settings.collection_pipeline
	if pipeline and not frappe.db.exists("CRM Pipeline", pipeline):
		pipeline = ""
	return {
		"enabled": bool(settings.cenprot_enabled),
		"installed": bool(pipeline),
		"pipeline": pipeline or "",
		"stages": [_(stage[0]) for stage in COLLECTION_STAGES],
	}


@frappe.whitelist()
@requires_feature(CREDIT_AND_COLLECTION)
def install_collection_template() -> dict:
	"""Instala o funil "Debt Collection" (Cobrança) com as 6 etapas e campos de protesto.
	Só administradores e gerentes. Pode ser instalado mesmo antes de configurar a CENPROT."""
	frappe.only_for(["System Manager", "Sales Manager"])
	settings = get_settings()
	pipeline = settings.collection_pipeline
	if not pipeline or not frappe.db.exists("CRM Pipeline", pipeline):
		create_collection_pipeline(settings)
		settings.save(ignore_permissions=True)
	return collection_funnel_status()


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
	# Localize (credtech#290) cria o endereço com Dynamic Link pro contato; o painel do Contato
	# (campo nativo "address", restaurado no credtech#315) só define o Link direto. Aceita os dois.
	name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Contact", "link_name": contact_name, "parenttype": "Address"},
		"parent",
	) or frappe.db.get_value("Contact", contact_name, "address")
	return frappe.get_doc("Address", name) if name else None


def _contact_party(contact) -> dict:
	"""Monta o endereço CENPROT do Contato lendo primeiro os campos diretos da seção Localize (Assertiva)
	(`cenprot_*`) e usando o Address vinculado como fallback para qualquer campo em branco."""
	address = None
	try:
		address = _contact_address(contact.name)
	except Exception:
		address = None
	base = _address_party(address)
	direct = {
		"endereco": (contact.get("cenprot_street") or "").strip(),
		"numero": (contact.get("cenprot_number") or "").strip(),
		"complemento": (contact.get("cenprot_complement") or "").strip(),
		"bairro": (contact.get("cenprot_neighborhood") or "").strip(),
		"municipio": (contact.get("cenprot_city") or "").strip(),
		"uf": (contact.get("cenprot_state") or "").strip(),
		"cep": only_digits(contact.get("cenprot_zip") or ""),
	}
	return {k: direct.get(k) or base.get(k) or "" for k in ("endereco", "numero", "complemento", "bairro", "municipio", "uf", "cep")}


def _primary_contact(deal) -> str | None:
	rows = deal.get("contacts") or []
	primary = next((row.contact for row in rows if row.get("is_primary")), None)
	return primary or next((row.contact for row in rows), None) or deal.get("contact")


def debtor_of(deal, row=None) -> dict:
	"""Devedor: o sacado do título (credtech#367: no card por cedente cada título tem o seu), senão a Organização do
	card com CNPJ ou, sem ela, o contato principal com CPF."""
	if row is not None and (row.get("sacado") or row.get("sacado_contact")):
		organization, contact_name = row.get("sacado"), row.get("sacado_contact")
	else:
		organization, contact_name = deal.get("organization"), None
	if organization:
		org = None
		try:
			org = frappe.get_doc("CRM Organization", organization)
		except frappe.DoesNotExistError:
			org = None
		if org and is_valid_cnpj(org.get("cnpj")):
			address = None
			if org.get("address"):
				try:
					address = frappe.get_doc("Address", org.address)
				except frappe.DoesNotExistError:
					address = None
			party = _address_party(address, org.get("neighborhood"), org.get("city"), org.get("state"))
			party.update(
				documento=only_digits(org.cnpj),
				nome=org.organization_name,
				fone=org.get("registry_phone"),
				email=org.get("registry_email"),
			)
			return party
	if not contact_name and not (row is not None and row.get("sacado")):
		# A título with its own sacado company never falls back to the card's contact
		contact_name = _primary_contact(deal)
	if contact_name:
		contact = None
		try:
			contact = frappe.get_doc("Contact", contact_name)
		except frappe.DoesNotExistError:
			contact = None
		if contact and len(only_digits(contact.get("cpf"))) == 11:
			party = _contact_party(contact)
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


def drawer_of(deal, row=None) -> dict | None:
	"""Sacador do título: o cedente que emitiu o título antecipado, o da linha (credtech#315) ou, sem ele, o do
	card (segunda organização). Vai no <sacador> do EnviarTitulo (manual v2.2: o <cedente> é o
	portador/apresentante). None sem cedente."""
	name = (row.get("cedente") if row is not None else None) or deal.get("second_organization")
	if not name or name == deal.get("organization"):
		return None
	try:
		org = frappe.get_doc("CRM Organization", name)
	except frappe.DoesNotExistError:
		return None
	address = None
	if org.get("address"):
		try:
			address = frappe.get_doc("Address", org.address)
		except frappe.DoesNotExistError:
			address = None
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
	message = describe(result)
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
		message = describe(result) or _("no answer")
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
	message = describe(result)
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
@requires_feature(CREDIT_AND_COLLECTION)
def send(deal: str) -> dict:
	doc = _deal(deal, "write")
	try:
		return send_title(doc)
	except TitleIncomplete as e:
		frappe.throw(str(e), title=_("The title cannot go to protest yet"))


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def refresh(deal: str) -> dict:
	return refresh_title(_deal(deal, "read"))


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def withdraw(deal: str, justification: str | None = None) -> dict:
	return withdraw_title(_deal(deal, "write"), (justification or "").strip())


@frappe.whitelist()
def server_ip() -> dict:
	"""O IP que o cliente cadastra no IEPTB (tela de Bureaus)."""
	frappe.only_for("System Manager")
	return {"ip": frappe.conf.get("credhub_outbound_ip") or OUTBOUND_IP}


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
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


def has_erp_titles(doc) -> bool:
	"""Card with títulos that came from an ERP (credtech#367): the registry is only ever reached by hand."""
	return any(row.get("erp_title_id") for row in doc.get("titles") or [])


def on_deal_update(doc, method=None):
	"""Card entrou na etapa de envio ou na de acordo, com a opção ligada: age em segundo plano. As duas opções
	começam desligadas: o cartório cobra custas e quem decide gastar é o cliente (dono, 30/10/2026)."""
	if doc.flags.cred_hub_protest or not doc.has_value_changed("status"):
		return
	if not automation_allowed(CREDIT_AND_COLLECTION):
		# Plano sem Crédito e Cobrança (credtech#398): a automação não roda
		return
	from cred_hub.sandbox import skip_side_effects

	if skip_side_effects(doc):
		return
	settings = get_settings()
	if not settings.cenprot_enabled or not settings.collection_pipeline or doc.pipeline != settings.collection_pipeline:
		return
	if has_erp_titles(doc):
		# Owner decision (10/11/2026): protest of a título synced from the ERP is manual, even with the automatic
		# send/withdraw options on; the registry charges fees and the client decides each one
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
	if not plan_allows(CREDIT_AND_COLLECTION):
		# Plano sem Crédito e Cobrança (credtech#398): a automação não roda
		return
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
# Codevedores por título: o limite da produção que já roda (o manual não fixa um)
MAX_CODEBTORS = 8
ADDRESS_KEYS = ("endereco", "numero", "complemento", "bairro", "municipio", "uf", "cep")
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
	encoded = base64.b64encode(content).decode()
	if len(encoded) > DOCUMENT_MAX_BYTES:
		return None, _(
			"The attachment is too big: CENPROT limits the document (in base64) to 5 MB, which is about 3.7 MB of file."
		)
	extension = "jpg" if extension == "jpeg" else extension
	return {"extensao": extension, "base64": encoded}, ""


def _row_keys(deal, row) -> dict:
	species = (row.get("species") or "").strip().upper()
	number = (row.get("title_number") or "").strip()
	return {
		"species": species,
		"number": "" if number.upper() == "S/N" else number,
		"our_number": (row.get("our_number") or "").strip() or _our_number_for(deal, row),
		"amount": flt(row.get("original_amount")),
		# o que ainda se deve (pagamentos registrados já abatidos); título quitado fica com 0, não volta ao valor original
		"balance": balance_of(row),
	}


def codebtor_candidates(deal) -> list[dict]:
	"""Possíveis codevedores do título: TODOS os contatos da organização devedora (sócios trazidos pela Receita,
	pelo Localizar ou cadastrados à mão). Mandar os sócios junto com o devedor é opcional, quem decide é o
	operador a cada envio (credtech#315). O Localizar só automatiza o preenchimento: o CPF e o endereço também
	podem ser digitados no contato. Cada candidato diz o que falta para ir, conforme o que a CENPROT exige."""
	organization = deal.get("organization")
	if not organization:
		return []
	contacts = frappe.get_all(
		"Contact",
		filters={"company_name": organization},
		fields=[
			"name", "full_name", "cpf", "designation", "share_percentage", "mobile_no", "phone", "email_id",
			"cenprot_zip", "cenprot_street", "cenprot_number", "cenprot_complement", "cenprot_neighborhood", "cenprot_city", "cenprot_state",
		],
		order_by="share_percentage desc, full_name asc",
	)
	address_labels = address_labels_()
	labels = {"documento": _("valid CPF"), "nome": _("name"), **address_labels}
	try:
		company = debtor_of(deal)
	except TitleIncomplete:
		company = None
	company_has_address = bool(company) and not _missing(company, address_labels)
	found = []
	for contact in contacts:
		party = _contact_party(contact)
		party.update(
			documento=only_digits(contact.cpf) if is_valid_cpf(contact.cpf) else "",
			nome=contact.full_name,
			fone=contact.mobile_no or contact.phone,
			email=contact.email_id,
		)
		address_from = "contact"
		if _missing(party, address_labels) and company_has_address:
			# Como na produção que já roda: o sócio sem endereço próprio vai com o da empresa, sem consulta extra
			party.update({key: company.get(key) for key in ADDRESS_KEYS})
			address_from = "company"
		found.append(
			{
				"contact": contact.name,
				"name": contact.full_name,
				"cpf": format_cpf(contact.cpf) if contact.cpf else "",
				"designation": contact.designation or "",
				"share": contact.share_percentage,
				"party": party,
				"address_from": address_from,
				"missing": _missing(party, labels),
			}
		)
	return found


def _as_list(value) -> list:
	if isinstance(value, str):
		value = frappe.parse_json(value) if value.strip() else []
	return list(value or [])


def _cenprot_key(number, our_number, due) -> tuple:
	"""Como o título chega na CENPROT depois dos cortes de tamanho do manual."""
	return (ascii_text(number, TITLE_NUMBER_SIZE), ascii_text(our_number, OUR_NUMBER_SIZE), str(due or ""))


def _collect_row(deal, row, settings, selected=None):
	"""(problemas que impedem o envio, título, chaves, avisos) de uma linha da aba Títulos. Nada é enviado nem
	cobrado aqui. `selected` são os contatos que o operador marcou para ir como codevedores."""
	keys = _row_keys(deal, row)
	problems = [] if keys["species"] else [species_missing()]
	if not keys["number"]:
		problems.append(_("Fill the title number."))
	if keys["amount"] <= 0:
		problems.append(_("Fill the title amount."))
	elif keys["balance"] <= 0:
		problems.append(_("This title is already paid: there is nothing left to protest."))
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

	mine = _cenprot_key(keys["number"], keys["our_number"], due)
	for other in deal.get("titles") or []:
		if other.name != row.name and _cenprot_key(
			_row_keys(deal, other)["number"], _row_keys(deal, other)["our_number"], other.get("due_date")
		) == mine:
			problems.append(
				_(
					"This title would reach CENPROT with the same number, our number and due date as the title {0} of this card (the manual cuts the number at 11 and our number at 15 characters). Change one of them."
				).format(other.get("title_number"))
			)
			break

	try:
		debtor = debtor_of(deal, row)
	except TitleIncomplete as e:
		problems.extend(e.problems)
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
	drawer = drawer_of(deal, row)
	if drawer:
		missing = _missing(drawer, {"nome": _("name"), "documento": _("CNPJ"), **address_labels})
		if missing:
			problems.append(_("Cedente (drawer of the title) without: {0}.").format(", ".join(missing)))

	codebtors = []
	candidates = {candidate["contact"]: candidate for candidate in codebtor_candidates(deal)}
	if len({contact for contact in selected or [] if contact in candidates}) > MAX_CODEBTORS:
		problems.append(
			_("A title takes up to {0} co-debtors in CENPROT. Uncheck some of the partners.").format(MAX_CODEBTORS)
		)
	for contact in selected or []:
		candidate = candidates.get(contact)
		if not candidate:
			continue  # só vai quem é candidato: contato de outra empresa nunca entra por aqui
		if candidate["missing"]:
			problems.append(
				_("Co-debtor {0} without: {1}. Uncheck it or complete the contact.").format(
					candidate["name"], ", ".join(candidate["missing"])
				)
			)
		else:
			codebtors.append(candidate["party"])

	if not debtor:
		return problems, None, keys, []
	title = {
		"cedente": creditor,
		"devedores": [debtor, *codebtors],
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
			"emissao": br_date(issue) if issue else "",
			"vencimento": br_date(due) if due else "",
		},
	}
	if document:
		title["divida"]["documento"] = document
	if drawer:
		title["sacador"] = drawer
	if settings.cenprot_bearer_code:
		title["geral"] = {"codPortador": settings.cenprot_bearer_code, "nomePortador": settings.cenprot_bearer_name}
	keys["debtor_document"] = debtor["documento"]
	parties = [(_("Debtor"), debtor, DEBTOR_SIZES)]
	parties += [(_("Co-debtor {0}").format(party["nome"]), party, DEBTOR_SIZES) for party in codebtors]
	parties.append((_("Creditor"), creditor, PARTY_SIZES))
	if drawer:
		parties.append((_("Cedente"), drawer, PARTY_SIZES))
	notes = truncations(parties, title["divida"])
	return problems, title, keys, notes


def build_title_from_row(deal, row, settings, codebtors=None) -> tuple[dict, dict]:
	"""(título para a CENPROT, chaves usadas) a partir de uma linha da aba Títulos. TitleIncomplete com tudo o que
	falta, antes de qualquer chamada paga. `codebtors`: contatos que o operador marcou para ir junto."""
	problems, title, keys, notes = _collect_row(deal, row, settings, codebtors)
	if problems:
		raise TitleIncomplete(problems)
	keys["notes"] = notes
	return title, keys


def _row_consult(deal, row) -> dict:
	"""As chaves com que a linha foi enviada (número, nosso número, vencimento e o documento do devedor)."""
	debtor = debtor_of(deal, row)
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


def send_deal_title_row(deal, row_name: str, codebtors=None) -> dict:
	settings = _settings()
	row = _find_row(deal, row_name)
	if not row:
		return {"ok": False, "message": _("Title row not found.")}
	status = row_status(row)
	if status not in SENDABLE:
		return {"ok": False, "message": _("This title is already at the registry (status {0}).").format(status)}

	title, keys = build_title_from_row(deal, row, settings, codebtors)
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
	message = describe(result)
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
	if len(title["devedores"]) > 1:
		_note(
			deal,
			_("Title {0} went with {1} co-debtor(s): {2}.").format(
				keys["number"],
				len(title["devedores"]) - 1,
				", ".join(debtor["nome"] for debtor in title["devedores"][1:]),
			),
		)
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
		message = describe(result) or _("no answer")
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
	message = describe(result)
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


@frappe.whitelist()
def preview_row(deal: str, title_row: str, codebtors=None) -> dict:
	"""O que a tela mostra antes de enviar um título (nada é enviado nem cobrado): o que impede o envio, os avisos
	(cortes de tamanho do manual) e os possíveis codevedores, com o que falta em cada um. Mandar os sócios junto
	é opcional; o Localizar só automatiza o preenchimento (credtech#315)."""
	doc = _deal(deal, "read")
	row = _find_row(doc, title_row)
	if not row:
		frappe.throw(_("Title row not found."))
	# Sem a CENPROT ligada ou sem usuário e senha, a tela mostra isso junto com o resto que falta (em vez de
	# uma mensagem solta): o cliente vê de uma vez tudo o que precisa resolver
	settings = get_settings()
	problems, _title, _keys, notes = _collect_row(doc, row, settings, _as_list(codebtors))
	if not settings.cenprot_enabled:
		problems.insert(0, _("CENPROT is off. An admin turns it on in Settings > Integrations > Bureaus."))
	elif not (settings.cenprot_username and secret(settings, "cenprot_password")):
		problems.insert(0, _("Fill the CENPROT username and password in Settings > Bureaus."))
	return {
		"problems": problems,
		"notes": notes,
		"codebtors": [
			{
				key: candidate[key]
				for key in ("contact", "name", "cpf", "designation", "share", "address_from", "missing")
			}
			for candidate in codebtor_candidates(doc)
		],
		"localize_on": bool(settings.get("assertiva_enabled")),
		"organization": doc.get("organization"),
	}


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def send_row(deal: str, title_row: str, codebtors=None) -> dict:
	doc = _deal(deal, "write")
	try:
		return send_deal_title_row(doc, title_row, _as_list(codebtors))
	except TitleIncomplete as e:
		frappe.throw(str(e), title=_("The title cannot go to protest yet"))


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def refresh_row(deal: str, title_row: str) -> dict:
	doc = _deal(deal, "read")
	row = _find_row(doc, title_row)
	if not row:
		return {"ok": False, "message": _("Title row not found.")}
	return refresh_title_row(doc, row)


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def withdraw_row(deal: str, title_row: str, justification: str | None = None) -> dict:
	return withdraw_title_row(_deal(deal, "write"), title_row, (justification or "").strip())
