"""Campos de CNPJ adicionados a Organização e Lead. Os campos "da Receita" sempre refletem o CNPJ
informado: quando o CNPJ muda, eles são preenchidos de novo."""

DOCTYPES = ("CRM Organization", "CRM Lead")

# campo no documento -> chave em mapping.from_*
RECEITA_FIELDS = {
	"razao_social": "razao_social",
	"nome_fantasia": "nome_fantasia",
	"situacao_cadastral": "situacao_cadastral",
	"data_abertura": "data_abertura",
	"cnae_principal": "cnae_principal",
	"natureza_juridica": "natureza_juridica",
	"porte": "porte",
	"capital_social": "capital_social",
	"telefone_empresa": "telefone",
	"email_empresa": "email",
	"endereco": "endereco",
	"municipio": "municipio",
	"uf": "uf",
	"cep": "cep",
}


def custom_fields(insert_after: str) -> list[dict]:
	"""Definição dos campos (formato de create_custom_fields), na ordem em que aparecem."""
	fields = [
		{"fieldname": "dados_receita_section", "fieldtype": "Section Break", "label": "Dados da Receita (CNPJ)", "collapsible": 1},
		{"fieldname": "cnpj", "fieldtype": "Data", "label": "CNPJ", "in_standard_filter": 1, "search_index": 1},
		{"fieldname": "razao_social", "fieldtype": "Data", "label": "Razão social"},
		{"fieldname": "nome_fantasia", "fieldtype": "Data", "label": "Nome fantasia"},
		{"fieldname": "situacao_cadastral", "fieldtype": "Data", "label": "Situação cadastral"},
		{"fieldname": "data_abertura", "fieldtype": "Date", "label": "Data de abertura"},
		{"fieldname": "cnae_principal", "fieldtype": "Data", "label": "CNAE principal", "length": 255},
		{"fieldname": "natureza_juridica", "fieldtype": "Data", "label": "Natureza jurídica"},
		{"fieldname": "porte", "fieldtype": "Data", "label": "Porte"},
		{"fieldname": "capital_social", "fieldtype": "Currency", "label": "Capital social"},
		{"fieldname": "dados_receita_col", "fieldtype": "Column Break"},
		{"fieldname": "telefone_empresa", "fieldtype": "Data", "label": "Telefone (Receita)", "options": "Phone"},
		{"fieldname": "email_empresa", "fieldtype": "Data", "label": "E-mail (Receita)"},
		{"fieldname": "endereco", "fieldtype": "Small Text", "label": "Endereço"},
		{"fieldname": "municipio", "fieldtype": "Data", "label": "Município"},
		{"fieldname": "uf", "fieldtype": "Data", "label": "UF", "length": 2},
		{"fieldname": "cep", "fieldtype": "Data", "label": "CEP", "length": 9},
		{"fieldname": "cnpj_consultado_em", "fieldtype": "Datetime", "label": "CNPJ consultado em", "read_only": 1},
	]
	previous = insert_after
	for field in fields:
		field["insert_after"] = previous
		previous = field["fieldname"]
	return fields


# Ordem dos campos na seção do painel lateral do CRM
SIDE_PANEL_FIELDS = ["cnpj", *RECEITA_FIELDS.keys(), "cnpj_consultado_em"]
