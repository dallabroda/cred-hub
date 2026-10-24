"""Instalação/migração do Cred Hub em cada site (idempotente)."""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from cred_hub.cnpj.fields import DOCTYPES, SIDE_PANEL_FIELDS, custom_fields
from cred_hub.cnpj.sectors import SECTORS
from cred_hub.company.fields import COMPANY_SECTION, COMPANY_SECTION_LABEL, COMPANY_SIDE_PANEL_FIELDS, company_custom_fields

CNPJ_SECTION = "cred_hub_cnpj_section"
CNPJ_SECTION_LABEL = "Company Registry (CNPJ)"


def after_install():
	create_custom_fields({dt: custom_fields(insert_after="facebook") + _missing(dt, company_custom_fields(insert_after="cnpj_checked_at")) for dt in DOCTYPES}, update=True)
	add_industries()
	for doctype in DOCTYPES:
		add_side_panel_section(doctype)
		add_side_panel_section(doctype, COMPANY_SECTION, COMPANY_SECTION_LABEL, COMPANY_SIDE_PANEL_FIELDS)
	add_quick_entry_cnpj("CRM Organization", after="organization_name")
	frappe.db.commit()


def _layout(name):
	if not frappe.db.exists("CRM Fields Layout", name):
		return None, None
	doc = frappe.get_doc("CRM Fields Layout", name)
	return doc, json.loads(doc.layout or "[]")


def _missing(doctype, fields):
	"""Só os campos que o doctype ainda não tem como campo padrão (ex.: o CRM já tem `linkedin` na Organização)."""
	standard = {f.fieldname for f in frappe.get_meta(doctype).fields if not f.get("is_custom_field")}
	kept = [f for f in fields if f["fieldname"] not in standard]
	for previous, field in zip([fields[0]["insert_after"], *[f["fieldname"] for f in kept]], kept):
		field["insert_after"] = previous  # refaz a ordem sem os campos pulados
	return kept


def add_industries():
	"""Setores da tabela de-para CNAE (nomes em inglês; pt-BR em locale/pt_BR.po)."""
	for _prefixes, sector, _pt in SECTORS:
		if not frappe.db.exists("CRM Industry", sector):
			frappe.get_doc({"doctype": "CRM Industry", "industry": sector}).insert(ignore_permissions=True)


def add_side_panel_section(doctype, name=CNPJ_SECTION, label=CNPJ_SECTION_LABEL, fields=SIDE_PANEL_FIELDS):
	"""Seção no painel lateral do Lead/Organização: "Company Registry (CNPJ)" (pt-BR: Dados da Receita) ou
	"Digital Presence". Mantém a lista de campos sempre igual à definida aqui (a v0.1 tinha nomes em português).
	"""
	doc, layout = _layout(f"{doctype}-Side Panel")
	if doc is None:
		return
	section = {
		"label": label,
		"name": name,
		"opened": True,
		"columns": [{"name": f"{name}_col", "fields": fields}],
	}
	current = next((s for s in layout if s.get("name") == name), None)
	if current == section:
		return
	if current:
		layout[layout.index(current)] = section
	else:
		layout.append(section)
	doc.layout = json.dumps(layout)
	doc.save(ignore_permissions=True)


def add_quick_entry_cnpj(doctype, after):
	"""Campo CNPJ logo depois do nome no cadastro rápido."""
	doc, layout = _layout(f"{doctype}-Quick Entry")
	if doc is None:
		return
	for section in layout:
		for column in section.get("columns", []):
			fields = column.get("fields", [])
			if "cnpj" in fields:
				return
			if after in fields:
				fields.insert(fields.index(after) + 1, "cnpj")
				doc.layout = json.dumps(layout)
				doc.save(ignore_permissions=True)
				return
