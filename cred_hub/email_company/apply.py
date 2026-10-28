"""Aplicar no card a empresa achada pelo e-mail do lead (credtech#187, épico #183).

Pedido do dono (27/10/2026):
- confiança alta: cria ou liga a Organização com os dados da Receita e liga o Contato;
- confiança média: o card mostra "Empresa provável: X (CNPJ)" para confirmar ou descartar com 1 clique;
- baixa: nada;
- registro na linha do tempo do card;
- interruptor em Configurações, ligado por padrão.

Nunca troca sozinho uma organização que alguém escolheu: só preenche o card sem organização ou a organização sem
CNPJ que existe só para este card (a que a entrada do lead criou com o nome digitado). Nos outros casos, a alta
também vira sugestão. Pôr o CNPJ na organização já dispara a busca dos dados na Receita (cred_hub.cnpj.events).
"""

import frappe
from frappe import _
from frappe.utils import cint

from cred_hub.cnpj.mapping import format_cnpj, only_digits
from cred_hub.email_company import score

SETTINGS = "FCRM Settings"
SETTING_FIELD = "email_company_discovery"
SUGGESTED, CONFIRMED, DISMISSED = "Suggested", "Confirmed", "Dismissed"
SOURCES = {"rdap": "Registro.br", "organization": "organization already registered"}


def custom_fields():
	return {
		"CRM Deal": [
			{
				"fieldname": "email_company_cnpj",
				"fieldtype": "Data",
				"label": "Probable company CNPJ",
				"read_only": 1,
				"hidden": 1,
				"insert_after": "organization",
			},
			{
				"fieldname": "email_company_name",
				"fieldtype": "Data",
				"label": "Probable company",
				"read_only": 1,
				"hidden": 1,
				"insert_after": "email_company_cnpj",
			},
			{
				"fieldname": "email_company_domain",
				"fieldtype": "Data",
				"label": "Probable company domain",
				"read_only": 1,
				"hidden": 1,
				"insert_after": "email_company_name",
			},
			{
				"fieldname": "email_company_status",
				"fieldtype": "Select",
				"label": "Probable company status",
				"options": f"\n{SUGGESTED}\n{CONFIRMED}\n{DISMISSED}",
				"read_only": 1,
				"hidden": 1,
				"insert_after": "email_company_domain",
			},
		],
		SETTINGS: [
			{
				"fieldname": SETTING_FIELD,
				"fieldtype": "Check",
				"label": "Find the company from the lead's e-mail",
				"default": "1",
			}
		],
	}


def install():
	"""Campos no card e o interruptor; sites que já existem começam com ele ligado (o padrão do campo só vale para
	configurações gravadas depois)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(custom_fields(), update=True)
	if not frappe.db.exists("Singles", {"doctype": SETTINGS, "field": SETTING_FIELD}):
		frappe.db.set_single_value(SETTINGS, SETTING_FIELD, 1)


def enabled() -> bool:
	"""Interruptor em Configurações (ligado por padrão); `credhub_email_company` no site_config ainda manda."""
	forced = frappe.conf.get("credhub_email_company")
	if forced is not None:
		return bool(cint(forced))
	if not frappe.get_meta(SETTINGS).has_field(SETTING_FIELD):
		return False
	return bool(cint(frappe.db.get_single_value(SETTINGS, SETTING_FIELD)))


@frappe.whitelist(methods=["GET"])
def get_setting() -> dict:
	return {"enabled": enabled()}


@frappe.whitelist(methods=["POST"])
def save_setting(enabled: int) -> dict:
	if not set(frappe.get_roles()) & {"System Manager", "Sales Manager"}:
		frappe.throw(_("Only managers can change this setting."), frappe.PermissionError)
	frappe.db.set_single_value(SETTINGS, SETTING_FIELD, 1 if cint(enabled) else 0)
	return get_setting()


def apply(lead: str, result: dict | None):
	"""Depois da descoberta (job): aplica a alta, sugere a média, ignora a baixa."""
	if not result or result.get("confidence") not in (score.HIGH, score.MEDIUM):
		return
	deal = frappe.db.get_value("CRM Deal", {"lead": lead}, "name")
	if not deal:
		return
	doc = frappe.get_doc("CRM Deal", deal)
	if doc.get("email_company_status"):
		return
	if result["confidence"] == score.HIGH:
		organization = _apply(doc, result, confirmed=False)
		if organization:
			doc.add_comment(
				"Comment",
				_("Company identified from the e-mail domain {0} ({1}), high confidence: {2}.").format(
					result["domain"], _(SOURCES.get(result.get("source"), result.get("source") or "")), organization
				),
			)
			return
	_suggest(doc, result)


def _suggest(doc, result):
	cnpj = result.get("cnpj") or _organization_cnpj(result.get("organization"))
	name = (result.get("company") or {}).get("trade_name") or (result.get("company") or {}).get("legal_name")
	name = name or result.get("organization")
	if not cnpj or not name:
		return
	doc.db_set(
		{
			"email_company_cnpj": format_cnpj(cnpj),
			"email_company_name": name,
			"email_company_domain": result.get("rdap_domain") or result.get("domain"),
			"email_company_status": SUGGESTED,
		},
		update_modified=False,
	)
	doc.add_comment(
		"Comment",
		_("Probable company from the e-mail domain {0}: {1} (CNPJ {2}). Confirm or dismiss it on the card.").format(
			result.get("domain"), name, format_cnpj(cnpj)
		),
	)


@frappe.whitelist(methods=["POST"])
def confirm(deal: str) -> dict:
	"""1 clique no card: a empresa provável passa a ser a organização do card."""
	from cred_hub.email_company import finder

	doc = frappe.get_doc("CRM Deal", deal)
	doc.check_permission("write")
	if doc.get("email_company_status") != SUGGESTED or not doc.get("email_company_cnpj"):
		frappe.throw(_("There is no probable company to confirm on this card."))
	cnpj = only_digits(doc.email_company_cnpj)
	result = {
		"cnpj": cnpj,
		"domain": doc.email_company_domain,
		"organization": finder.find_organization(set(), cnpj),
		"company": finder.receita(cnpj) or {"legal_name": doc.email_company_name},
	}
	organization = _apply(doc, result, confirmed=True)
	doc.db_set("email_company_status", CONFIRMED, update_modified=False)
	doc.add_comment(
		"Comment", _("{0} confirmed the probable company: {1}.").format(frappe.session.user, organization)
	)
	return {"organization": organization}


@frappe.whitelist(methods=["POST"])
def dismiss(deal: str) -> dict:
	doc = frappe.get_doc("CRM Deal", deal)
	doc.check_permission("write")
	if doc.get("email_company_status") != SUGGESTED:
		frappe.throw(_("There is no probable company to dismiss on this card."))
	doc.db_set("email_company_status", DISMISSED, update_modified=False)
	doc.add_comment("Comment", _("{0} dismissed the probable company {1}.").format(frappe.session.user, doc.email_company_name))
	return {"dismissed": True}


def _apply(doc, result, confirmed: bool) -> str | None:
	"""Põe a empresa no card. Sem confirmação, só onde não troca escolha de ninguém; devolve None se não aplicou."""
	cnpj = only_digits(result.get("cnpj") or _organization_cnpj(result.get("organization")))
	current = doc.organization
	if current:
		current_cnpj = only_digits(_organization_cnpj(current))
		if current_cnpj and current_cnpj == cnpj:
			_link_contacts(doc, current)
			return current
		only_this_card = not current_cnpj and _used_only_by(current, doc.name)
		if only_this_card and not result.get("organization"):
			# a organização que a entrada do lead criou com o nome digitado ganha o CNPJ (e os dados da Receita)
			organization = frappe.get_doc("CRM Organization", current)
			organization.cnpj = format_cnpj(cnpj)
			organization.save(ignore_permissions=True)
			_link_contacts(doc, current)
			return current
		if not (confirmed or only_this_card):
			return None
	organization = result.get("organization") or _create_organization(cnpj, result)
	doc.organization = organization
	doc.flags.ignore_permissions = True
	doc.save()
	_link_contacts(doc, organization)
	return organization


def _create_organization(cnpj, result) -> str:
	company = result.get("company") or {}
	name = (company.get("trade_name") or company.get("legal_name") or format_cnpj(cnpj)).strip()
	if frappe.db.exists("CRM Organization", name):
		name = f"{name} ({format_cnpj(cnpj)})"
	values = {"doctype": "CRM Organization", "organization_name": name, "cnpj": format_cnpj(cnpj)}
	site = result.get("final_domain") or result.get("rdap_domain") or result.get("domain")
	if site:
		values["website"] = f"https://{site}"
	return frappe.get_doc(values).insert(ignore_permissions=True).name


def _organization_cnpj(organization) -> str:
	if not organization or not frappe.get_meta("CRM Organization").has_field("cnpj"):
		return ""
	return frappe.db.get_value("CRM Organization", organization, "cnpj") or ""


def _used_only_by(organization, deal) -> bool:
	return not frappe.db.exists("CRM Deal", {"organization": organization, "name": ("!=", deal)})


def _link_contacts(doc, organization):
	"""Os contatos do card passam a ser pessoas da organização."""
	for row in doc.get("contacts") or []:
		contact = frappe.get_doc("Contact", row.contact)
		if any(l.link_doctype == "CRM Organization" and l.link_name == organization for l in contact.links):
			continue
		contact.append("links", {"link_doctype": "CRM Organization", "link_name": organization})
		contact.save(ignore_permissions=True)
