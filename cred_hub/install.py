"""Instalação/migração do Cred Hub em cada site (idempotente)."""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from cred_hub.cnpj.fields import CHECKED_DATE_FIELDS, DETAILS_ADDRESS_FIELDS, DOCTYPES, SIDE_PANEL_FIELDS, custom_fields
from cred_hub.chat.leads import CONVERSATION_FIELD
from cred_hub.cnpj.sectors import SECTORS
from cred_hub.company.fields import COMPANY_SECTION, COMPANY_SECTION_LABEL, COMPANY_SIDE_PANEL_FIELDS, company_custom_fields

CNPJ_SECTION = "cred_hub_cnpj_section"
CNPJ_SECTION_LABEL = "Company Registry (CNPJ)"
# Território (região de vendas do Frappe CRM) sai das telas: a CredTech não usa (pedido do dono, 24/10/2026)
HIDDEN_FIELDS = ("territory",)
TERRITORY_DOCTYPES = ("CRM Lead", "CRM Deal", "CRM Organization")
# Link da conversa do Cred Chat que originou o lead/negócio (cred_hub.chat)
CHAT_FIELD_AFTER = {"CRM Lead": "source", "CRM Deal": "lead"}
CHAT_PANEL_AFTER = {"CRM Lead": "source", "CRM Deal": "deal_owner"}


def after_install():
	create_custom_fields({dt: custom_fields(insert_after="facebook") + _missing(dt, company_custom_fields(insert_after="cnpj_checked_on")) for dt in DOCTYPES}, update=True)
	migrate_checked_dates()
	create_custom_fields({dt: [_chat_field(after)] for dt, after in CHAT_FIELD_AFTER.items()}, update=True)
	add_industries()
	for doctype in DOCTYPES:
		add_side_panel_section(doctype)
		add_side_panel_section(doctype, COMPANY_SECTION, COMPANY_SECTION_LABEL, COMPANY_SIDE_PANEL_FIELDS)
		add_address_to_details(doctype)
	for doctype in TERRITORY_DOCTYPES:
		for kind in ("Side Panel", "Quick Entry", "Data Fields"):
			remove_from_layout(f"{doctype}-{kind}", HIDDEN_FIELDS)
	for doctype, after in CHAT_PANEL_AFTER.items():
		add_to_side_panel(doctype, CONVERSATION_FIELD, after)
	# Gera o token do webhook do Cred Chat na primeira vez (a URL aparece em Cred Hub Settings)
	frappe.get_single("Cred Hub Settings").save(ignore_permissions=True)
	add_quick_entry_cnpj("CRM Organization", after="organization_name")
	frappe.db.commit()


def _layout(name):
	if not frappe.db.exists("CRM Fields Layout", name):
		return None, None
	doc = frappe.get_doc("CRM Fields Layout", name)
	return doc, json.loads(doc.layout or "[]")


def migrate_checked_dates():
	"""Datas de consulta passaram de data e hora para só a data (o Frappe não troca o tipo de um campo
	existente): copia o dia dos campos antigos para os novos e remove os antigos."""
	for doctype in DOCTYPES:
		for old, new in CHECKED_DATE_FIELDS.items():
			name = f"{doctype}-{old}"
			if not frappe.db.exists("Custom Field", name):
				continue
			if frappe.db.has_column(doctype, old):
				table = f"tab{doctype}"
				frappe.db.sql(f"update `{table}` set `{new}` = date(`{old}`) where `{new}` is null and `{old}` is not null")
			frappe.delete_doc("Custom Field", name, ignore_permissions=True)


def _field_name(field):
	return field.get("fieldname") if isinstance(field, dict) else field


def _strip_fields(node, names) -> bool:
	"""Tira os campos `names` de toda lista "fields" do layout (qualquer formato: seções, abas)."""
	changed = False
	if isinstance(node, dict):
		fields = node.get("fields")
		if isinstance(fields, list):
			kept = [f for f in fields if _field_name(f) not in names]
			if len(kept) != len(fields):
				node["fields"] = kept
				changed = True
		for value in node.values():
			if isinstance(value, list | dict):
				changed = _strip_fields(value, names) or changed
	elif isinstance(node, list):
		for item in node:
			changed = _strip_fields(item, names) or changed
	return changed


def remove_from_layout(name, names):
	doc, layout = _layout(name)
	if doc is None:
		return
	changed = _strip_fields(layout, set(names))
	# Seção própria do campo (ex.: "territory_section" do cadastro rápido) que ficou vazia sai junto
	empty = {f"{n}_section" for n in names}
	kept = [s for s in layout if not (s.get("name") in empty and not any(c.get("fields") for c in s.get("columns", [])))]
	if changed or len(kept) != len(layout):
		doc.layout = json.dumps(kept)
		doc.save(ignore_permissions=True)


def add_address_to_details(doctype):
	"""UF, cidade e bairro na seção Detalhes do painel lateral, logo depois do Endereço (Organização) ou do
	nº de funcionários (Lead, que não tem Endereço)."""
	doc, layout = _layout(f"{doctype}-Side Panel")
	if doc is None:
		return
	details = next((s for s in layout if s.get("name") == "details_section"), None)
	if not details or not details.get("columns"):
		return
	fields = details["columns"][0].setdefault("fields", [])
	anchor = next((a for a in ("address", "no_of_employees") if a in fields), None)
	position = fields.index(anchor) + 1 if anchor else len(fields)
	if fields[position : position + len(DETAILS_ADDRESS_FIELDS)] == DETAILS_ADDRESS_FIELDS:
		return
	_strip_fields(layout, set(DETAILS_ADDRESS_FIELDS))
	fields = details["columns"][0]["fields"]
	position = fields.index(anchor) + 1 if anchor else len(fields)
	fields[position:position] = DETAILS_ADDRESS_FIELDS
	doc.layout = json.dumps(layout)
	doc.save(ignore_permissions=True)


def _chat_field(insert_after):
	return {
		"fieldname": CONVERSATION_FIELD,
		"fieldtype": "Data",
		"options": "URL",
		"label": "Cred Chat Conversation",
		"read_only": 1,
		"insert_after": insert_after,
	}


def add_to_side_panel(doctype, field, after):
	"""Põe `field` no painel lateral, logo depois de `after` (na seção onde `after` estiver)."""
	doc, layout = _layout(f"{doctype}-Side Panel")
	if doc is None:
		return
	for section in layout:
		for column in section.get("columns", []):
			fields = column.get("fields", [])
			if field in fields:
				return
			if after in fields:
				fields.insert(fields.index(after) + 1, field)
				doc.layout = json.dumps(layout)
				doc.save(ignore_permissions=True)
				return


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
