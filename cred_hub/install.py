"""Instalação/migração do Cred Hub em cada site (idempotente)."""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from cred_hub.cnpj.fields import DOCTYPES, SIDE_PANEL_FIELDS, custom_fields

CNPJ_SECTION = "cred_hub_cnpj_section"


def after_install():
	create_custom_fields({dt: custom_fields(insert_after="facebook") for dt in DOCTYPES}, update=True)
	for doctype in DOCTYPES:
		add_side_panel_section(doctype)
	add_quick_entry_cnpj("CRM Organization", after="organization_name")
	frappe.db.commit()


def _layout(name):
	if not frappe.db.exists("CRM Fields Layout", name):
		return None, None
	doc = frappe.get_doc("CRM Fields Layout", name)
	return doc, json.loads(doc.layout or "[]")


def add_side_panel_section(doctype):
	"""Seção "Dados da Receita (CNPJ)" no painel lateral do Lead/Organização."""
	doc, layout = _layout(f"{doctype}-Side Panel")
	if doc is None or any(s.get("name") == CNPJ_SECTION for s in layout):
		return
	layout.append(
		{
			"label": "Dados da Receita (CNPJ)",
			"name": CNPJ_SECTION,
			"opened": True,
			"columns": [{"name": f"{CNPJ_SECTION}_col", "fields": SIDE_PANEL_FIELDS}],
		}
	)
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
