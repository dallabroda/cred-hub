"""Import de-para (credtech#367): every column the ERP returns -> the CRM field the company chooses.

Two maps in Cred Hub ERP Settings (rows of Cred Hub ERP Import Map: erp_column -> crm_field):
- títulos: a column of the ERP's open títulos -> a field of the título (CRM Deal Title). Applied on every sync (the ERP
  owns the título); the identity (ERP id, due date, sacado, cedente) is always read by the sync itself and is not
  offered here;
- sacados: a column of the ERP's sacado registry -> organization.<field> (sacado with CNPJ) or contact.<field> (CPF).
  Only empty fields are filled: what the user or the Localizar wrote is never overwritten.

The column names the ERP returned are kept in the settings (erp_columns), so the screen offers all of them, including
the ones a given ERP account has and the documentation does not list.
"""

import json
import re
import unicodedata

from .formats import digits, to_date, to_decimal

TITLE, SACADO, CEDENTE = "title", "sacado", "cedente"
TITLE_ROW = "CRM Deal Title"
ORG, CONTACT = "CRM Organization", "Contact"

# Seen in the Smart documentation (emaberto and the sacado registry); the real columns are added by each sync
SMART_TITLE_COLUMNS = [
	"ID_TITULO", "DOCUMENTO", "CONF", "CR", "M", "TIPO", "CPF_CNPJ_SACADO", "SACADO", "CPF_CNPJ_CEDENTE", "CEDENTE",
	"DATA_EMISSAO", "ORIGINAL", "VENCIMENTO", "SITUACAO", "OP", "VALOR", "VALOR_TOTAL", "LINHA_DIGITAVEL",
	"NOSSO_NUMERO", "DATA_INADIMPLENCIA",
]
SMART_SACADO_COLUMNS = [
	"ID", "Nome", "CPF/CNPJ", "ENDERECO", "CEP", "Cidade", "UF", "E-mail", "Telefone", "SMS", "Data Cadastro",
	"ID Grupo Economico", "Grupo Economico",
]
# Read by the sync itself (identity of the título and of its parties): shown as automatic, never mapped
SMART_TITLE_FIXED = {
	"ID_TITULO": "erp_title_id",
	"VENCIMENTO": "due_date",
	"CPF_CNPJ_SACADO": "sacado",
	"SACADO": "sacado",
	"CPF_CNPJ_CEDENTE": "cedente",
	"CEDENTE": "cedente",
}
SMART_SACADO_FIXED = {"CPF/CNPJ": "document", "Nome": "name"}
# GET /cedente (documented columns; the real ones are added by each sync)
SMART_CEDENTE_COLUMNS = [
	"ID_CEDENTE", "NOME", "CPF_CNPJ", "ENDERECO", "CEP", "CIDADE", "UF", "EMAIL", "TELEFONE", "GERENTE", "OPERADOR",
	"CAPTADOR", "CONTROLADOR", "RESPONSAVEL_CONFIRMACAO", "RESPONSAVEL_COBRANCA", "FATOR_%", "ADVALOREM_%", "SETOR",
	"FONTE_CAPTACAO", "GRUPO_ECONOMICO", "ID_GRUPO_ECONOMICO", "DATA_CADASTRO", "PRIMEIRA_OPERACAO",
	"VENCIMENTO_CONTRATO", "BLOQUEADO", "LIMITE_GLOBAL", "RISCO_ATUAL", "SALDO",
]
SMART_CEDENTE_FIXED = {"CPF_CNPJ": "document", "NOME": "name"}

# Starting point; each company edits it (the sync already fills number, issue date, amounts, digitable line...)
DEFAULT_TITLE_MAP = {"Smart Securities": [("TIPO", "erp_title_type")]}
# Empty: reading the sacado registry is extra ERP calls, so it starts only when the company maps a column
DEFAULT_SACADO_MAP = {}

# Título fields the company never fills from the ERP: identity, payments, closing and protest (the species is always
# picked by the operator, never read from the ERP's "type" of título: owner rule)
PROTECTED_TITLE_FIELDS = {
	"erp_source", "erp_title_id", "erp_run_id", "due_date", "sacado", "sacado_contact", "cedente", "closed_reason",
	"closed_on", "paid_amount", "species", "sent_at", "promise_date", "promise_amount", "erp_amount", "erp_situation",
	"erp_operation", "requires_attachment", "attachment",
}
PROTECTED_PREFIXES = ("protest_", "cenprot_")
VALUE_TYPES = {
	"Data", "Small Text", "Text", "Long Text", "Select", "Date", "Datetime", "Int", "Float", "Currency", "Percent",
	"Check", "Phone",
}


def column_key(name) -> str:
	"""Smart's sacado keys change spelling ("E-Mail"/"Email", "Data Cadastro"/"Data de Cadastro", accents): compare
	without accents, case, punctuation and the word "de"."""
	text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
	words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w != "de"]
	return "".join(words)


def value_of(row: dict, column: str):
	if column in row:
		return row[column]
	wanted = column_key(column)
	return next((v for k, v in row.items() if column_key(k) == wanted), None)


def convert(value, fieldtype: str, length: int = 140):
	"""ERP text -> the CRM field's type. None when there is nothing usable."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	if fieldtype in ("Date", "Datetime"):
		return to_date(value)
	if fieldtype in ("Currency", "Float", "Percent"):
		number = to_decimal(value)
		return float(number) if number is not None else None
	if fieldtype == "Int":
		text = digits(value)
		return int(text) if text else None
	if fieldtype == "Check":
		return 1 if str(value).strip().lower() in ("1", "s", "sim", "true", "yes", "x") else 0
	text = re.sub(r"\s+", " ", str(value)).strip()
	return text[:length] if fieldtype in ("Data", "Phone", "Select") else text


def title_values(row: dict, rules, fieldtypes: dict) -> dict:
	"""{título field: value} from one ERP título row. Rules to protected or unknown fields are ignored."""
	values = {}
	for rule in rules or []:
		target = _get(rule, "crm_field")
		if not allowed_title_field(target) or target not in fieldtypes:
			continue
		value = convert(value_of(row, _get(rule, "erp_column")), fieldtypes[target])
		if value is not None:
			values[target] = value
	return values


def sacado_values(row: dict, rules, kind: str, fieldtypes: dict) -> dict:
	"""{field: value} for the sacado's organization (kind "organization") or contact (kind "contact")."""
	values = {}
	for rule in rules or []:
		entity, _sep, target = (_get(rule, "crm_field") or "").partition(".")
		if entity != kind or target not in fieldtypes or target in values:
			continue
		value = convert(value_of(row, _get(rule, "erp_column")), fieldtypes[target])
		if value is not None:
			values[target] = value
	return values


def allowed_title_field(fieldname) -> bool:
	return bool(fieldname) and fieldname not in PROTECTED_TITLE_FIELDS and not fieldname.startswith(PROTECTED_PREFIXES)


def merge_columns(raw, kind: str, rows) -> tuple[str, bool]:
	"""Adds the columns of these ERP rows to the stored JSON. (new JSON, changed?)."""
	try:
		data = json.loads(raw) if raw else {}
	except ValueError:
		data = {}
	known = list(data.get(kind) or [])
	seen = {column_key(c) for c in known}
	for row in rows or []:
		for column in row:
			if column_key(column) not in seen:
				seen.add(column_key(column))
				known.append(column)
	changed = known != list(data.get(kind) or [])
	data[kind] = known
	return json.dumps(data, ensure_ascii=False), changed


def columns(raw, kind: str, provider: str) -> list[str]:
	"""Columns offered on the screen: the documented ones plus every one the ERP returned (no duplicates)."""
	try:
		seen = (json.loads(raw) if raw else {}).get(kind) or []
	except ValueError:
		seen = []
	documented = {TITLE: SMART_TITLE_COLUMNS, SACADO: SMART_SACADO_COLUMNS, CEDENTE: SMART_CEDENTE_COLUMNS}
	base = documented.get(kind, []) if provider == "Smart Securities" else []
	result, keys = [], set()
	for column in [*base, *seen]:
		if column_key(column) not in keys:
			keys.add(column_key(column))
			result.append(column)
	return result


def _get(rule, key):
	return rule.get(key) if isinstance(rule, dict) else getattr(rule, key, None)
