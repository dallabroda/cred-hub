"""Campos de presença digital da empresa (LinkedIn, funcionários) em Organização e Lead.
`website` e `no_of_employees` já existem no Frappe CRM; aqui ficam só os campos que faltam.

Nomes internos e rótulos em inglês (regra de idioma do dono); pt-BR em locale/pt_BR.po.
"""

# Seção "Digital Presence" do painel lateral: saiu (redundante, pedido do dono 26/10/2026); o nome fica para removê-la
# dos sites que já a têm. Os campos continuam no doctype e são preenchidos pelo enriquecimento.
COMPANY_SECTION = "cred_hub_company_section"
COMPANY_SECTION_LABEL = "Digital Presence"


def company_custom_fields(insert_after: str) -> list[dict]:
	fields = [
		{"fieldname": "digital_presence_section", "fieldtype": "Section Break", "label": COMPANY_SECTION_LABEL, "collapsible": 1},
		{"fieldname": "linkedin", "fieldtype": "Data", "label": "Company LinkedIn", "options": "URL"},
		{"fieldname": "employee_count", "fieldtype": "Int", "label": "Employees (count)"},
		{"fieldname": "employee_range", "fieldtype": "Data", "label": "Employees (range/floor)"},
		{"fieldname": "employee_source", "fieldtype": "Data", "label": "Employees Source", "read_only": 1},
		{"fieldname": "company_checked_on", "fieldtype": "Date", "label": "Digital Presence Checked On", "read_only": 1},
	]
	previous = insert_after
	for field in fields:
		field["insert_after"] = previous
		previous = field["fieldname"]
	return fields

