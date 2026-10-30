"""Campos do épico credtech#290 no Contato (Localize) e no Card (protesto CENPROT).

Nomes e rótulos em inglês; pt-BR em locale/pt_BR.po. Os textos do Card são curtos de propósito: a linha do Card
no MariaDB tem limite de tamanho (ver crm/fcrm/doctype/crm_pipeline/fields.py).
"""

import frappe

SPECIES = ("DMI", "DSI", "CBI", "CCB", "DM", "DS", "NP", "CH", "CD", "CPS", "CA", "CL", "CM", "TA", "DD")
DECLARATIONS = ("D", "C", "A", "G", "I")

CONTACT_FIELDS = [
	{"fieldname": "localize_section", "fieldtype": "Section Break", "label": "Localize (Assertiva)", "collapsible": 1},
	{"fieldname": "cpf", "fieldtype": "Data", "label": "CPF", "length": 14, "search_index": 1},
	{"fieldname": "has_whatsapp", "fieldtype": "Check", "label": "Has WhatsApp", "read_only": 1},
	{"fieldname": "phone_assertiveness", "fieldtype": "Int", "label": "Phone assertiveness", "read_only": 1},
	{"fieldname": "phone_carrier", "fieldtype": "Data", "label": "Phone carrier", "length": 40, "read_only": 1},
	{"fieldname": "localize_column", "fieldtype": "Column Break"},
	{"fieldname": "share_percentage", "fieldtype": "Percent", "label": "Share of the capital"},
	{"fieldname": "last_enriched_at", "fieldtype": "Datetime", "label": "Localize query on", "read_only": 1},
	{"fieldname": "assertiva_protocol", "fieldtype": "Data", "label": "Assertiva protocol", "length": 60, "read_only": 1},
]
# Painel lateral do Contato
CONTACT_PANEL_FIELDS = ["cpf", "has_whatsapp", "phone_assertiveness", "phone_carrier", "share_percentage", "last_enriched_at"]

DEAL_FIELDS = [
	{"fieldname": "protest_section", "fieldtype": "Tab Break", "label": "Protest (CENPROT)"},
	{"fieldname": "protest_species", "fieldtype": "Select", "label": "Title type", "options": "\n" + "\n".join(SPECIES), "length": 10},
	{"fieldname": "protest_title_number", "fieldtype": "Data", "label": "Title number", "length": 11},
	{"fieldname": "protest_our_number", "fieldtype": "Data", "label": "Our number", "length": 15},
	{"fieldname": "protest_amount", "fieldtype": "Currency", "label": "Title amount"},
	{"fieldname": "protest_balance", "fieldtype": "Currency", "label": "Balance to protest"},
	{"fieldname": "protest_issue_date", "fieldtype": "Date", "label": "Issue date"},
	{"fieldname": "protest_due_date", "fieldtype": "Date", "label": "Due date"},
	{
		"fieldname": "protest_declaration",
		"fieldtype": "Select",
		"label": "Bearer declaration",
		"options": "\n" + "\n".join(DECLARATIONS),
		"length": 1,
		"description": "Empty: D for DMI/DSI, C for CCB/CBI, A for the others.",
	},
	{"fieldname": "protest_column", "fieldtype": "Column Break"},
	{"fieldname": "protest_status", "fieldtype": "Data", "label": "Registry status", "length": 20, "read_only": 1, "in_standard_filter": 1},
	{"fieldname": "protest_message", "fieldtype": "Small Text", "label": "Registry message", "read_only": 1},
	{"fieldname": "protest_registry_protocol", "fieldtype": "Data", "label": "Registry protocol", "length": 40, "read_only": 1},
	{"fieldname": "protest_registry_office", "fieldtype": "Data", "label": "Registry office", "length": 60, "read_only": 1},
	{"fieldname": "protest_debtor_document", "fieldtype": "Data", "label": "Protested CPF or CNPJ", "length": 14, "read_only": 1},
	{"fieldname": "protest_sent_at", "fieldtype": "Datetime", "label": "Sent to the registry on", "read_only": 1},
	{"fieldname": "protest_checked_at", "fieldtype": "Datetime", "label": "Registry checked on", "read_only": 1},
]
# Campos do Card no funil de Cobrança (as quebras de seção não entram no formulário do funil)
DEAL_FORM_FIELDS = [f["fieldname"] for f in DEAL_FIELDS if f["fieldtype"] not in ("Tab Break", "Section Break", "Column Break")]


def _chained(fields, insert_after):
	chained, previous = [], insert_after
	for field in fields:
		chained.append({**field, "insert_after": previous})
		previous = field["fieldname"]
	return chained


def _last_field(doctype, own) -> str:
	"""Último campo do doctype fora os deste módulo: as seções novas ficam no fim do formulário."""
	names = [df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldname not in own]
	return names[-1]


def custom_fields() -> dict:
	return {
		doctype: _chained(fields, _last_field(doctype, {f["fieldname"] for f in fields}))
		for doctype, fields in (("Contact", CONTACT_FIELDS), ("CRM Deal", DEAL_FIELDS))
	}
