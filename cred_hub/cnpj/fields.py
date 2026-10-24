"""Campos de CNPJ adicionados a Organização e Lead. Os campos "da Receita" sempre refletem o CNPJ
informado: quando o CNPJ muda, eles são preenchidos de novo.

Nomes internos e rótulos em inglês (regra de idioma do dono); a interface em pt-BR vem de locale/pt_BR.po.
"""

from cred_hub.cnpj.mapping import COMPANY_SIZES

DOCTYPES = ("CRM Organization", "CRM Lead")

# Campos preenchidos direto com a chave de mesmo nome de mapping.from_*
RECEITA_FIELDS = (
	"legal_name",
	"trade_name",
	"registration_status",
	"activity_start_date",
	"main_cnae",
	"legal_nature",
	"company_size",
	"is_mei",
	"share_capital",
	"registry_phone",
	"registry_email",
	"neighborhood",
	"city",
	"state",
)

# Nomes antigos (v0.1, em português) -> novos; usados pela migração (patches/v0_2)
RENAMED_FIELDS = {
	"razao_social": "legal_name",
	"nome_fantasia": "trade_name",
	"situacao_cadastral": "registration_status",
	"data_abertura": "activity_start_date",
	"cnae_principal": "main_cnae",
	"natureza_juridica": "legal_nature",
	"porte": "company_size",
	"capital_social": "share_capital",
	"telefone_empresa": "registry_phone",
	"email_empresa": "registry_email",
	"municipio": "city",
	"uf": "state",
	"cnpj_consultado_em": "cnpj_checked_at",
}
# Saem da seção (o endereço completo vai para o Address padrão, em Detalhes)
REMOVED_FIELDS = ("endereco", "cep", "dados_receita_section", "dados_receita_col")


def custom_fields(insert_after: str) -> list[dict]:
	"""Definição dos campos (formato de create_custom_fields), na ordem em que aparecem."""
	fields = [
		{"fieldname": "registry_section", "fieldtype": "Section Break", "label": "Company Registry (CNPJ)", "collapsible": 1},
		{"fieldname": "cnpj", "fieldtype": "Data", "label": "CNPJ", "in_standard_filter": 1, "search_index": 1},
		{"fieldname": "legal_name", "fieldtype": "Data", "label": "Legal Name"},
		{"fieldname": "trade_name", "fieldtype": "Data", "label": "Trade Name"},
		{"fieldname": "registration_status", "fieldtype": "Data", "label": "Registration Status"},
		{"fieldname": "main_cnae", "fieldtype": "Data", "label": "Main CNAE", "length": 255},
		{"fieldname": "legal_nature", "fieldtype": "Data", "label": "Legal Nature"},
		{"fieldname": "company_size", "fieldtype": "Select", "label": "Company Size", "options": "\n" + "\n".join(COMPANY_SIZES)},
		{"fieldname": "is_mei", "fieldtype": "Select", "label": "Is MEI?", "options": "\nYes\nNo"},
		{"fieldname": "activity_start_date", "fieldtype": "Date", "label": "Activity Start Date"},
		{"fieldname": "years_in_business", "fieldtype": "Int", "label": "Years in Business", "read_only": 1},
		{"fieldname": "share_capital", "fieldtype": "Currency", "label": "Share Capital"},
		{"fieldname": "registry_col", "fieldtype": "Column Break"},
		{"fieldname": "registry_phone", "fieldtype": "Data", "label": "Registry Phone", "options": "Phone"},
		{"fieldname": "registry_email", "fieldtype": "Data", "label": "Registry Email"},
		{"fieldname": "neighborhood", "fieldtype": "Data", "label": "Neighborhood"},
		{"fieldname": "city", "fieldtype": "Data", "label": "City"},
		{"fieldname": "state", "fieldtype": "Data", "label": "State (UF)", "length": 2},
		{"fieldname": "cnpj_checked_at", "fieldtype": "Datetime", "label": "CNPJ Checked At", "read_only": 1},
	]
	previous = insert_after
	for field in fields:
		field["insert_after"] = previous
		previous = field["fieldname"]
	return fields


# Ordem dos campos na seção do painel lateral do CRM (pedido do dono: UF, Cidade, Bairro, Porte, Início, Capital, MEI)
SIDE_PANEL_FIELDS = [
	"cnpj",
	"legal_name",
	"trade_name",
	"registration_status",
	"main_cnae",
	"legal_nature",
	"company_size",
	"is_mei",
	"activity_start_date",
	"years_in_business",
	"share_capital",
	"state",
	"city",
	"neighborhood",
	"registry_phone",
	"registry_email",
	"cnpj_checked_at",
]
