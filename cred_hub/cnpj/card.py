"""CNPJ no card (CRM Deal, épico #66). Os dados da Receita moram só na Organização (sem cópia no card;
a tela lê a Organização ligada ao card). Quando um card nasce de uma entrada automática (lead com
CNPJ), o CNPJ vai para a Organização do card, que então é consultada na Receita.
"""

import frappe

from cred_hub.cnpj.fields import RECEITA_FIELDS


def after_insert(doc, method=None):
	"""Card criado a partir de um lead com CNPJ: leva o CNPJ (e o que já veio da Receita) para a
	Organização do card, criando-a se o lead não tinha nome de empresa."""
	if not doc.get("lead"):
		return
	lead = frappe.db.get_value("CRM Lead", doc.lead, ["cnpj", *RECEITA_FIELDS], as_dict=True)
	if not lead or not lead.cnpj:
		return

	values = {f: lead.get(f) for f in ("cnpj", *RECEITA_FIELDS) if lead.get(f)}
	if doc.organization:
		org = frappe.get_doc("CRM Organization", doc.organization)
		missing = {f: v for f, v in values.items() if not org.get(f)}
		if missing:
			org.update(missing)
			org.save(ignore_permissions=True)
		return

	name = lead.trade_name or lead.legal_name or lead.cnpj
	org_name = frappe.db.get_value("CRM Organization", {"cnpj": lead.cnpj}) or frappe.db.exists(
		"CRM Organization", {"organization_name": name}
	)
	if not org_name:
		org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, **values})
		org.insert(ignore_permissions=True)
		org_name = org.name
	doc.db_set("organization", org_name)
