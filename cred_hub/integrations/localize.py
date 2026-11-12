"""Buscar contatos da Organização na Assertiva Localize (credtech#290, módulo 1).

O botão "Buscar contatos (Localize)" da Organização consulta o CNPJ e cria ou atualiza um Contato por sócio pessoa
física (CPF, cargo, participação). Com "Também buscar telefones e e-mails de cada sócio" ligado pelo cliente
(desligado por padrão: quem paga decide, dono 30/10/2026), cada sócio é consultado pelo CPF (uma consulta paga a mais
por sócio) e o Contato recebe:

- os celulares e fixos, com o celular de WhatsApp de maior assertividade como celular principal (o celular que o
  contato já tinha continua principal se a Assertiva também o devolveu);
- `has_whatsapp`, `phone_assertiveness` e `phone_carrier` do celular principal, para a régua de WhatsApp disparar
  só para quem tem WhatsApp;
- os e-mails, sem duplicar.

O sócio que a consulta do CNPJ na Receita já cadastrou (só com o nome) é reaproveitado pelo nome. Telefone e e-mail
da empresa preenchem os da Receita na Organização só quando estão vazios. Consultas dentro do prazo do cache saem do
histórico, sem custo, salvo pedido explícito de nova consulta paga.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from cred_hub.billing import CREDIT_AND_COLLECTION, requires_feature
from cred_hub.cnpj.enrich import person_name
from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits
from cred_hub.integrations import bureau_log, get_settings, secret
from cred_hub.integrations import bigdatacorp as bdc_adapter
from cred_hub.integrations.assertiva import (
	PURPOSES,
	AssertivaClient,
	AssertivaError,
	addresses as _assertiva_addresses,
	emails as _assertiva_emails,
	partners as _assertiva_partners,
	phones as _assertiva_phones,
	protocol_of as _assertiva_protocol_of,
)
from cred_hub.integrations.bigdatacorp import BigDataCorpClient, BigDataCorpError
from cred_hub.party.document import format_cpf

PROVIDER = "Assertiva"
BDC_PROVIDER = "BigDataCorp"
OP_CNPJ, OP_CPF = "localize_cnpj", "localize_cpf"
DEFAULT_PURPOSE = "Credit protection and collection"


def _is_bdc_payload(payload) -> bool:
	return isinstance(payload, dict) and isinstance(payload.get("Result"), list)


def protocol_of(payload) -> str:
	return bdc_adapter.protocol_of(payload) if _is_bdc_payload(payload) else _assertiva_protocol_of(payload)


def partners(payload) -> list[dict]:
	return bdc_adapter.partners(payload) if _is_bdc_payload(payload) else _assertiva_partners(payload)


def phones(payload) -> list[dict]:
	return bdc_adapter.phones(payload) if _is_bdc_payload(payload) else _assertiva_phones(payload)


def emails(payload) -> list[str]:
	return bdc_adapter.emails(payload) if _is_bdc_payload(payload) else _assertiva_emails(payload)


def addresses(payload) -> list[dict]:
	return bdc_adapter.addresses(payload) if _is_bdc_payload(payload) else _assertiva_addresses(payload)


def _use_bdc(settings) -> bool:
	return bool(getattr(settings, "bigdatacorp_localize_enabled", 0))


def _active_provider(settings) -> str:
	return BDC_PROVIDER if _use_bdc(settings) else PROVIDER


def _settings():
	settings = get_settings()
	if not settings.assertiva_enabled and not _use_bdc(settings):
		frappe.throw(_("Assertiva Localize is off. An admin turns it on in Settings > Integrations > Bureaus."))
	return settings


def _client(settings):
	if _use_bdc(settings):
		return BigDataCorpClient(
			secret(settings, "bigdatacorp_access_token") or getattr(settings, "bigdatacorp_access_token", ""),
			getattr(settings, "bigdatacorp_token_id", "") or "",
		)
	return AssertivaClient(settings.assertiva_client_id, secret(settings, "assertiva_client_secret"))


def _purpose(settings) -> tuple[int, str]:
	label = settings.assertiva_purpose or DEFAULT_PURPOSE
	return PURPOSES.get(label, 2), label


def _query(client, settings, operation, document, reference, force) -> tuple[dict | None, bool]:
	"""(resposta ou None se não localizado, veio do cache?). Toda consulta paga fica no histórico."""
	provider = _active_provider(settings)
	cache_days = cint(getattr(settings, "bigdatacorp_cache_days", 0) or settings.assertiva_cache_days or 30)
	if not force:
		cached = bureau_log.recent(provider, operation, document, cache_days)
		if cached:
			return cached.data, True
	purpose, label = _purpose(settings)
	call = client.localize_cnpj if operation == OP_CNPJ else client.localize_cpf
	try:
		status, payload = call(document, purpose)
	except (AssertivaError, BigDataCorpError) as e:
		bureau_log.record(
			provider, operation, bureau_log.ERROR, document=document, reference=reference,
			http_status=e.http_status, purpose=label, error=str(e),
		)
		raise
	bureau_log.record(
		provider,
		operation,
		bureau_log.SUCCESS if payload is not None else bureau_log.NOT_FOUND,
		document=document,
		reference=reference,
		http_status=status,
		protocol=protocol_of(payload),
		purpose=label,
		response=payload,
	)
	return payload, False


@frappe.whitelist()
def get_status(organization: str) -> dict:
	"""Para o botão: ligado? tem CNPJ? quando foi a última consulta (dentro do prazo do cache)?"""
	org = frappe.get_doc("CRM Organization", organization)
	org.check_permission("read")
	settings = get_settings()
	if not settings.assertiva_enabled and not _use_bdc(settings):
		return {"enabled": False}
	provider = _active_provider(settings)
	cnpj = only_digits(org.get("cnpj"))
	cache_days = cint(getattr(settings, "bigdatacorp_cache_days", 0) or settings.assertiva_cache_days or 30)
	last = bureau_log.recent(provider, OP_CNPJ, cnpj, cache_days) if cnpj else None
	return {
		"enabled": True,
		"provider": provider,
		"has_cnpj": is_valid_cnpj(cnpj),
		"can_write": bool(frappe.has_permission("CRM Organization", "write", org)),
		"last_query_on": last.creation if last else None,
		"cache_days": cache_days,
		"partner_lookup": True if _use_bdc(settings) else bool(settings.assertiva_partner_lookup),
	}


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def enrich_organization(organization: str, force: int | str = 0) -> dict:
	org = frappe.get_doc("CRM Organization", organization)
	org.check_permission("write")
	settings = _settings()
	cnpj = only_digits(org.get("cnpj"))
	if not is_valid_cnpj(cnpj):
		frappe.throw(_("Fill a valid CNPJ in the organization first."))
	force = cint(force)
	client = _client(settings)
	try:
		company, cached = _query(client, settings, OP_CNPJ, cnpj, org, force)
	except (AssertivaError, BigDataCorpError) as e:
		# sem frappe.throw: o histórico da tentativa (cobrada ou não) precisa ficar gravado
		return {"ok": False, "message": str(e)}
	summary = {"ok": True, "found": company is not None, "cached": cached, "paid_queries": 0 if cached else 1}
	summary.update(created=0, updated=0, phones=0, whatsapp=0, emails=0, addresses=0, dnd=0, errors=[])
	if company is None:
		summary["message"] = _("Assertiva did not find this CNPJ.")
		return summary

	protocol = protocol_of(company)
	for partner in partners(company):
		person, person_protocol = partner.get("inline_person_payload"), protocol
		if settings.assertiva_partner_lookup and person is None:
			try:
				person, person_cached = _query(client, settings, OP_CPF, partner["cpf"], org, force)
			except (AssertivaError, BigDataCorpError) as e:
				summary["errors"].append(f"{person_name(partner['name'])}: {e}")
			else:
				summary["paid_queries"] += 0 if person_cached else 1
				person_protocol = protocol_of(person) or protocol
		frappe.db.savepoint("cred_hub_localize_contact")
		try:
			stats = upsert_contact(org, partner, person, person_protocol)
		except Exception:
			frappe.db.rollback(save_point="cred_hub_localize_contact")
			frappe.log_error(title=f"Cred Hub: Localize could not save partner of {org.name}")
			summary["errors"].append(_("{0}: the contact could not be saved.").format(person_name(partner["name"])))
			continue
		summary["created" if stats["created"] else "updated"] += 1
		for key in ("phones", "whatsapp", "emails", "addresses", "dnd"):
			summary[key] += stats[key]

	_fill_company_contact(org, company)
	summary["cached"] = cached and summary["paid_queries"] == 0
	summary["message"] = _summary_text(summary, protocol)
	org.add_comment("Comment", summary["message"])
	return summary


def _summary_text(summary, protocol) -> str:
	text = _("Assertiva Localize: {0} contact(s) created and {1} updated, {2} phone(s) ({3} with WhatsApp) and {4} e-mail(s).").format(
		summary["created"], summary["updated"], summary["phones"], summary["whatsapp"], summary["emails"]
	)
	if summary.get("dnd"):
		text += " " + _("{0} number(s) marked 'do not disturb' were saved but not used as the main phone.").format(
			summary["dnd"]
		)
	if summary.get("addresses"):
		text += " " + _("{0} address(es) saved for the CENPROT.").format(summary["addresses"])
	if summary["cached"]:
		text += " " + _("Saved result, at no cost.")
	else:
		text += " " + _("{0} paid query(ies).").format(summary["paid_queries"])
	if protocol:
		text += " " + _("Protocol {0}.").format(protocol)
	return text


def _fill_company_contact(org, company):
	"""Telefone e e-mail da empresa só preenchem os campos da Receita que estão vazios."""
	values = {}
	if org.meta.has_field("registry_phone") and not org.get("registry_phone"):
		best = next(iter(phones(company)), None)
		if best:
			values["registry_phone"] = best["phone"]
	if org.meta.has_field("registry_email") and not org.get("registry_email"):
		best = next(iter(emails(company)), None)
		if best:
			values["registry_email"] = best
	if values:
		# db_set: não dispara de novo a consulta do CNPJ nem a busca de presença digital
		org.db_set(values, update_modified=False)


def _find_contact(org, partner) -> str | None:
	cpf = format_cpf(partner["cpf"])
	name = frappe.db.get_value("Contact", {"company_name": org.name, "cpf": cpf})
	if name:
		return name
	# sócio que a consulta do CNPJ na Receita cadastrou só com o nome
	wanted = person_name(partner["name"]).lower()
	for row in frappe.get_all(
		"Contact", filters={"company_name": org.name, "cpf": ("is", "not set")}, fields=["name", "full_name"]
	):
		if (row.full_name or "").strip().lower() == wanted:
			return row.name
	return None


def upsert_contact(org, partner, person, protocol) -> dict:
	name = _find_contact(org, partner)
	contact = frappe.get_doc("Contact", name) if name else frappe.new_doc("Contact")
	if not name:
		contact.first_name = person_name(partner["name"])
		contact.company_name = org.name
	contact.cpf = format_cpf(partner["cpf"])
	if partner.get("qualification") and not contact.get("designation"):
		contact.designation = person_name(partner["qualification"])
	if partner.get("share") is not None:
		contact.share_percentage = partner["share"]

	found_phones = phones(person) if person else []
	found_emails = emails(person) if person else []
	stats = {
		"created": not name,
		"phones": 0,
		"whatsapp": 0,
		"emails": 0,
		"addresses": 0,
		"dnd": sum(1 for phone in found_phones if phone.get("dnd")),
	}

	known = {only_digits(row.phone) for row in contact.get("phone_nos") or []}
	for phone in found_phones:
		if only_digits(phone["phone"]) not in known:
			contact.append("phone_nos", {"phone": phone["phone"]})
			known.add(only_digits(phone["phone"]))
			stats["phones"] += 1
	_set_primary_phones(contact, found_phones)
	stats["whatsapp"] = sum(1 for phone in found_phones if phone["whatsapp"])

	known_emails = {(row.email_id or "").strip().lower() for row in contact.get("email_ids") or []}
	for address in found_emails:
		if address not in known_emails:
			contact.append("email_ids", {"email_id": address, "is_primary": 0 if known_emails else 1})
			known_emails.add(address)
			stats["emails"] += 1

	if person:
		best_addr = next(iter(addresses(person)), None)
		if best_addr:
			for attr, key in (
				("cenprot_zip", "zip"),
				("cenprot_street", "street"),
				("cenprot_number", "number"),
				("cenprot_complement", "complement"),
				("cenprot_neighborhood", "neighborhood"),
				("cenprot_city", "city"),
				("cenprot_state", "state"),
			):
				if not contact.get(attr) and best_addr.get(key):
					contact.set(attr, best_addr[key])
	contact.last_enriched_at = now_datetime()
	contact.assertiva_protocol = (protocol or "")[:60]
	contact.flags.ignore_mandatory = True
	if name:
		contact.save(ignore_permissions=True)
	else:
		contact.insert(ignore_permissions=True)
	stats["addresses"] = _ensure_address(contact, person)
	return stats


def _ensure_address(contact, person) -> int:
	"""O melhor endereço da consulta por CPF vira o endereço do contato, que a CENPROT exige para mandar o sócio
	como codevedor. Contato que já tem endereço (digitado pelo cliente, por exemplo) não é alterado."""
	if not person:
		return 0
	if frappe.db.exists(
		"Dynamic Link", {"link_doctype": "Contact", "link_name": contact.name, "parenttype": "Address"}
	):
		return 0
	best = next(iter(addresses(person)), None)
	if not best:
		return 0
	# a vírgula separa o número do logradouro e o bairro fica por último na linha 2 (o que o envio à CENPROT lê)
	street = f"{best['street']}, {best['number']}" if best["number"] else best["street"]
	line2 = ", ".join(part for part in (best["complement"], best["neighborhood"]) if part) if best["neighborhood"] else ""
	frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": (contact.full_name or contact.name)[:100],
			"address_type": "Personal",
			"address_line1": street,
			"address_line2": line2,
			"city": best["city"],
			"state": best["state"],
			"pincode": best["zip"],
			"country": "Brazil",
			"links": [{"link_doctype": "Contact", "link_name": contact.name}],
		}
	).insert(ignore_permissions=True)
	return 1


def _set_primary_phones(contact, found_phones):
	mobiles = [p for p in found_phones if p["mobile"] and not p.get("dnd")]
	rows = contact.get("phone_nos") or []
	if mobiles:
		current = only_digits(contact.get("mobile_no"))
		chosen = next((p for p in mobiles if only_digits(p["phone"]) == current), mobiles[0])
		for row in rows:
			row.is_primary_mobile_no = int(only_digits(row.phone) == only_digits(chosen["phone"]))
		contact.has_whatsapp = int(chosen["whatsapp"])
		contact.phone_assertiveness = chosen["score"]
		contact.phone_carrier = chosen["carrier"]
	fixed = next((p for p in found_phones if not p["mobile"] and not p.get("dnd")), None)
	if fixed and not any(row.is_primary_phone for row in rows):
		for row in rows:
			if only_digits(row.phone) == only_digits(fixed["phone"]):
				row.is_primary_phone = 1
				break


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def test_connection() -> dict:
	"""Só gera o token (não é consulta paga): confere Client ID e Client Secret."""
	frappe.only_for("System Manager")
	settings = get_settings()
	try:
		_client(settings).token(renew=True)
	except AssertivaError as e:
		return {"ok": False, "message": str(e)}
	return {"ok": True, "message": _("Assertiva accepted the credentials.")}

