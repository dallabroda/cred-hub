"""CNPJ no card (CRM Deal, épico #66). Os dados da Receita moram só na Organização (sem cópia no card;
a tela lê a Organização ligada ao card). Quando um card nasce de uma entrada automática (lead com
CNPJ), o CNPJ vai para a Organização do card, que então é consultada na Receita.
"""

import frappe
from frappe import _

from cred_hub.cnpj.fields import RECEITA_FIELDS
from cred_hub.cnpj.mapping import format_cnpj, only_digits
from cred_hub.party.document import CPF, document_kind, format_cpf
from cred_hub.party.resolve import organization_for_cnpj


def after_insert(doc, method=None):
	"""Card criado a partir de um lead com CNPJ: leva o CNPJ (e o que já veio da Receita) para a
	Organização do card, sem nunca misturar empresas diferentes. CPF no campo do CNPJ é pessoa física:
	vai para o contato do card e nunca vira Organização (credtech#315)."""
	if not doc.get("lead"):
		return
	lead = frappe.db.get_value("CRM Lead", doc.lead, ["cnpj", *RECEITA_FIELDS], as_dict=True)
	if not lead or not only_digits(lead.cnpj or ""):
		return
	if document_kind(lead.cnpj) == CPF:
		_cpf_to_contact(doc, lead.cnpj)
		return
	digits = only_digits(lead.cnpj)
	values = {f: lead.get(f) for f in ("cnpj", *RECEITA_FIELDS) if lead.get(f)}

	if doc.organization:
		org = frappe.get_doc("CRM Organization", doc.organization)
		if org.cnpj and only_digits(org.cnpj) != digits:
			# Outra empresa: não mistura os dados, só registra o CNPJ que veio na entrada
			doc.add_comment(
				"Comment",
				_("The entry came with CNPJ {0}, different from the organization's ({1}).").format(
					lead.cnpj, org.cnpj
				),
			)
			return
		missing = {f: v for f, v in values.items() if not org.get(f)}
		if missing:
			org.update(missing)
			org.save(ignore_permissions=True)
		return

	# db_set: só liga a organização ao card recém-criado, sem rodar de novo as validações do card
	name = lead.trade_name or lead.legal_name or format_cnpj(digits)
	doc.db_set("organization", organization_for_cnpj(digits, name, values))


def _primary_contact(doc) -> str | None:
	rows = doc.get("contacts") or []
	primary = next((row.contact for row in rows if row.get("is_primary")), None)
	return primary or next((row.contact for row in rows), None) or doc.get("contact")


def _cpf_to_contact(doc, cpf):
	"""Põe o CPF no contato principal do card quando ele ainda não tem; senão só registra no card."""
	formatted = format_cpf(cpf)
	contact = _primary_contact(doc)
	current = frappe.db.get_value("Contact", contact, "cpf") if contact else None
	if contact and not current:
		frappe.db.set_value("Contact", contact, "cpf", formatted)
		return
	if contact and only_digits(current) == only_digits(cpf):
		return
	doc.add_comment(
		"Comment",
		_("The entry came with the CPF {0}: a person goes in the contact, not in an organization.").format(formatted),
	)
