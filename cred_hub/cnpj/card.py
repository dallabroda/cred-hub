"""CNPJ no card (CRM Deal, épico #66). Os dados da Receita moram só na Organização (sem cópia no card;
a tela lê a Organização ligada ao card). Quando um card nasce de uma entrada automática (lead com
CNPJ), o CNPJ vai para a Organização do card, que então é consultada na Receita.
"""

import frappe
from frappe import _

from cred_hub.cnpj.fields import RECEITA_FIELDS
from cred_hub.cnpj.mapping import format_cnpj, only_digits


def after_insert(doc, method=None):
	"""Card criado a partir de um lead com CNPJ: leva o CNPJ (e o que já veio da Receita) para a
	Organização do card, sem nunca misturar empresas diferentes."""
	if not doc.get("lead"):
		return
	lead = frappe.db.get_value("CRM Lead", doc.lead, ["cnpj", *RECEITA_FIELDS], as_dict=True)
	if not lead or not only_digits(lead.cnpj or ""):
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
	doc.db_set("organization", _organization_for(digits, values, lead))


def _organization_for(digits: str, values: dict, lead) -> str:
	"""Organização com esse CNPJ; senão uma de mesmo nome ainda sem CNPJ; senão uma nova."""
	formatted = format_cnpj(digits)
	same_cnpj = frappe.get_all(
		"CRM Organization", {"cnpj": ("in", (formatted, digits))}, pluck="name", limit=1
	)
	if same_cnpj:
		return same_cnpj[0]

	name = lead.trade_name or lead.legal_name or formatted
	homonym = frappe.db.get_value(
		"CRM Organization", {"organization_name": name}, ["name", "cnpj"], as_dict=True
	)
	if homonym and not homonym.cnpj:
		org = frappe.get_doc("CRM Organization", homonym.name)
		org.update(values)
		org.save(ignore_permissions=True)
		return org.name
	if homonym:
		# Mesmo nome, outra empresa: o nome é a chave da organização, então vai com o CNPJ junto
		name = f"{name} ({formatted})"

	org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, **values})
	org.insert(ignore_permissions=True)
	return org.name
