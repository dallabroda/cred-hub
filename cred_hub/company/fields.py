"""Campos de presença digital da empresa (LinkedIn, funcionários) em Organização e Lead.
`website` e `no_of_employees` já existem no Frappe CRM; aqui ficam só os campos que faltam.

Nomes internos e rótulos em inglês (regra de idioma do dono); pt-BR em locale/pt_BR.po.
"""

COMPANY_SECTION = "cred_hub_company_section"
COMPANY_SECTION_LABEL = "Digital Presence"


def company_custom_fields(insert_after: str) -> list[dict]:
	fields = [
		{"fieldname": "digital_presence_section", "fieldtype": "Section Break", "label": COMPANY_SECTION_LABEL, "collapsible": 1},
		{"fieldname": "linkedin", "fieldtype": "Data", "label": "Company LinkedIn", "options": "URL"},
		{"fieldname": "employee_count", "fieldtype": "Int", "label": "Employees (count)"},
		{"fieldname": "employee_range", "fieldtype": "Data", "label": "Employees (range/floor)"},
		{"fieldname": "employee_source", "fieldtype": "Data", "label": "Employees Source", "read_only": 1},
		{"fieldname": "company_checked_at", "fieldtype": "Datetime", "label": "Digital Presence Checked At", "read_only": 1},
	]
	previous = insert_after
	for field in fields:
		field["insert_after"] = previous
		previous = field["fieldname"]
	return fields


COMPANY_SIDE_PANEL_FIELDS = [
	"linkedin",
	"employee_count",
	"employee_range",
	"employee_source",
	"company_checked_at",
]
