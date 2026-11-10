"""Field-mapping (de-para) engine: CRM records -> ERP payload (credtech#367).

Each rule says: ERP field <- CRM source, transform, default, required. Pure Python, so the same code serves the
preview, the automatic send with its "required fields" gate, and the tests.

Sources are "<entity>.<fieldname>": deal, organization, contact, address (the organization's Address, plus
"ibge_code" looked up from the CEP by the caller). Custom fields work the same way (e.g. "deal.custom_floating").
"a.x|b.y" falls back to the next source when the first is empty. Special sources:
  "today"   -> today's date
  "auto"    -> filled by the ERP step itself (e.g. Smart's next master contract number)
  ""        -> only the default value is used
ERP fields are dotted paths into the payload; "telefones[0].ddd" builds lists.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from . import formats

AUTO = "auto"

TRANSFORMS = {
	"": lambda v: v,
	"none": lambda v: v,
	"digits": formats.digits,
	"upper": lambda v: str(v).strip().upper(),
	"lower": lambda v: str(v).strip().lower(),
	"trim": lambda v: str(v).strip(),
	"ascii": formats.ascii_text,
	"br_money": formats.br_money,
	"decimal": lambda v: float(formats.to_decimal(v)) if formats.to_decimal(v) is not None else None,
	"int": lambda v: int(formats.digits(v)) if formats.digits(v) else None,
	"br_date": formats.br_date,
	"iso_date": formats.iso_date,
	"phone_ddd": lambda v: formats.split_phone(v)[0],
	"phone_number": lambda v: formats.split_phone(v)[1],
	"phone_ddd_int": lambda v: int(formats.split_phone(v)[0]) if formats.split_phone(v)[0] else None,
	"phone_number_int": lambda v: int(formats.split_phone(v)[1]) if formats.split_phone(v)[1] else None,
	"street": formats.street_name,
	"street_number": formats.street_number,
	"person_type": formats.person_type,
	"bool": lambda v: str(v).strip().lower() in ("1", "true", "yes", "sim", "s"),
}


@dataclass
class Rule:
	erp_field: str
	source: str = ""
	transform: str = ""
	default: str | None = None
	required: bool = False
	label: str = ""

	@classmethod
	def from_row(cls, row) -> "Rule":
		get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
		return cls(
			erp_field=(get("erp_field") or "").strip(),
			source=(get("source") or "").strip(),
			transform=(get("transform") or "").strip(),
			default=get("default_value"),
			required=bool(get("required")),
			label=get("label") or "",
		)


@dataclass
class Result:
	payload: dict = field(default_factory=dict)
	missing: list[Rule] = field(default_factory=list)
	auto: list[str] = field(default_factory=list)
	errors: list[str] = field(default_factory=list)

	@property
	def ok(self) -> bool:
		return not self.missing and not self.errors


def _empty(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip()) or value in ([], {})


def resolve(source: str, records: dict, today: date | None = None):
	"""Value of a source. "a.x|b.y" tries each alternative in order and returns the first filled one."""
	if not source:
		return None
	if "|" in source:
		for alternative in source.split("|"):
			value = resolve(alternative.strip(), records, today)
			if not _empty(value):
				return value
		return None
	if source == "today":
		return today or date.today()
	if source == AUTO:
		return None
	entity, _sep, fieldname = source.partition(".")
	record = records.get(entity) or {}
	if not fieldname:
		return None
	return record.get(fieldname) if isinstance(record, dict) else getattr(record, fieldname, None)


_PATH = re.compile(r"([^.\[\]]+)(?:\[(\d+)\])?")


def set_path(payload: dict, path: str, value):
	"""set_path(p, "endereco.cep", x); set_path(p, "telefones[0].ddd", x)."""
	parts = path.split(".")
	node = payload
	for position, part in enumerate(parts):
		match = _PATH.fullmatch(part)
		if not match:
			raise ValueError(f"Invalid ERP field path: {path}")
		key, index = match.group(1), match.group(2)
		last = position == len(parts) - 1
		if index is None:
			if last:
				node[key] = value
			else:
				node = node.setdefault(key, {})
			continue
		items = node.setdefault(key, [])
		index = int(index)
		while len(items) <= index:
			items.append({} if not last else None)
		if last:
			items[index] = value
		else:
			if not isinstance(items[index], dict):
				items[index] = {}
			node = items[index]


def build(rules, records: dict, today: date | None = None) -> Result:
	"""Apply the rules. Empty values are left out of the payload; empty required ones go to `missing`."""
	result = Result()
	for raw in rules:
		rule = raw if isinstance(raw, Rule) else Rule.from_row(raw)
		if not rule.erp_field:
			continue
		if rule.source == AUTO:
			result.auto.append(rule.erp_field)
			continue
		value = resolve(rule.source, records, today)
		if _empty(value) and not _empty(rule.default):
			value = rule.default
		if not _empty(value):
			transform = TRANSFORMS.get(rule.transform)
			if transform is None:
				result.errors.append(f"{rule.erp_field}: unknown transform '{rule.transform}'")
				continue
			try:
				value = transform(value)
			except (TypeError, ValueError, ArithmeticError) as e:
				result.errors.append(f"{rule.erp_field}: {e}")
				continue
		if _empty(value) and not _empty(rule.default):
			# The source had text but the transform found nothing in it (e.g. no street number): use the default as is
			value = rule.default
		if _empty(value):
			if rule.required:
				result.missing.append(rule)
			continue
		set_path(result.payload, rule.erp_field, value)
	return result


def _rule(erp_field, source="", transform="", default=None, required=False, label=""):
	return {
		"erp_field": erp_field,
		"source": source,
		"transform": transform,
		"default_value": default,
		"required": int(required),
		"label": label,
	}


# Starting point offered to every company; each one edits it in the settings.
DEFAULT_RULES = {
	"WBA": [
		_rule("documento", "organization.cnpj", "digits", required=True, label="CPF/CNPJ"),
		_rule("tipoPessoa", "organization.cnpj", "person_type", required=True, label="Person type"),
		_rule("cadastroAtivo", "", "bool", "1", label="Active"),
		_rule("pessoaJuridica.razaoSocial", "organization.legal_name|organization.organization_name", "trim", required=True, label="Legal name"),
		_rule("pessoaJuridica.situacaoCadastral", "", "upper", "ATIVA", label="Registration status"),
		_rule("cedente.limite", "deal.deal_value", "decimal", label="Credit limit"),
		_rule("cedente.floatingMinimo", "", "int", "1", label="Minimum floating (days)"),
		_rule("contato", "contact.full_name", "trim", required=True, label="Contact name"),
		_rule("email", "organization.registry_email|contact.email_id", "lower", required=True, label="Company e-mail"),
		_rule("emailContato", "contact.email_id", "lower", label="Contact e-mail"),
		_rule("telefone.ddd", "organization.registry_phone", "phone_ddd_int", label="Phone area code"),
		_rule("telefone.numero", "organization.registry_phone", "phone_number_int", label="Phone"),
		_rule("celular.ddd", "contact.mobile_no", "phone_ddd_int", label="Mobile area code"),
		_rule("celular.numero", "contact.mobile_no", "phone_number_int", label="Mobile"),
		_rule("endereco.cep", "address.pincode", "digits", required=True, label="ZIP code"),
		_rule("endereco.endereco", "address.address_line1", "street", required=True, label="Street"),
		_rule("endereco.numero", "address.address_line1", "street_number", "S/N", label="Number"),
		_rule("endereco.complemento", "address.address_line2", "trim", label="Complement"),
		_rule("endereco.bairro", "organization.neighborhood|address.address_line2", "trim", required=True, label="District"),
		_rule("endereco.cidade", "address.city", "trim", required=True, label="City"),
		_rule("endereco.uf", "address.state", "upper", required=True, label="State"),
		_rule("endereco.codigoIbge", "address.ibge_code", "digits", label="IBGE city code"),
	],
	"Smart Securities": [
		_rule("cpfCnpj", "organization.cnpj", "digits", required=True, label="CPF/CNPJ"),
		_rule("razaoSocial", "organization.legal_name|organization.organization_name", "ascii", required=True, label="Legal name"),
		_rule("nomeFantasia", "organization.trade_name", "ascii", label="Trade name"),
		_rule("clienteDesde", "today", "br_date", required=True, label="Client since"),
		_rule("numeroContratoMae", AUTO, label="Master contract number"),
		_rule("emailPrincipal", "organization.registry_email|contact.email_id", "lower", required=True, label="Main e-mail"),
		_rule("idSetor", "", "int", required=True, label="Sector (Smart ID)"),
		_rule("idGerente", "", "int", required=True, label="Manager (Smart ID)"),
		_rule("dataFundacao", "organization.activity_start_date", "br_date", label="Foundation date"),
		_rule("capitalSocial", "organization.share_capital", "br_money", label="Share capital"),
		_rule("cnae", "organization.main_cnae", "digits", label="CNAE"),
		_rule("cepEnderecoPrincipal", "address.pincode", "int", required=True, label="ZIP code"),
		_rule("logradouroEnderecoPrincipal", "address.address_line1", "street", required=True, label="Street"),
		_rule("numeroEnderecoPrincipal", "address.address_line1", "street_number", "1", required=True, label="Number"),
		_rule("complementoEnderecoPrincipal", "address.address_line2", "ascii", label="Complement"),
		_rule("bairroEnderecoPrincipal", "organization.neighborhood|address.address_line2", "ascii", required=True, label="District"),
		_rule("codigoMunicipioIbgeEnderecoPrincipal", "address.ibge_code", "int", required=True, label="IBGE city code"),
		_rule("enderecoCobrancaIgualAoEnderecoPrincipal", "", "int", "1", label="Billing address = main"),
		_rule("telefones[0].ddd", "contact.mobile_no", "phone_ddd", label="Contact phone area code"),
		_rule("telefones[0].numero", "contact.mobile_no", "phone_number", label="Contact phone"),
		_rule("telefones[0].nomeContato", "contact.full_name", "ascii", label="Contact name"),
	],
}


def finalize(provider: str, payload: dict) -> dict | list:
	"""ERP-specific last touches after the rules ran."""
	if provider == "WBA":
		# One endpoint for cedente and sacado: the filled sub-object decides the role
		payload.setdefault("cedente", {})
		payload.setdefault("sacado", None)
		if payload.get("tipoPessoa") == "PF":
			razao = (payload.pop("pessoaJuridica", None) or {}).get("razaoSocial")
			payload.setdefault("pessoaFisica", {"nome": razao} if razao else {})
		return [payload]
	if provider == "Smart Securities":
		# Smart needs "nomeContato" on every phone and rejects incomplete ones; address text without punctuation
		phones = [p for p in payload.get("telefones") or [] if p and p.get("ddd") and p.get("numero")]
		for phone in phones:
			phone.setdefault("nomeContato", "Financeiro")
		if phones:
			payload["telefones"] = phones
		else:
			payload.pop("telefones", None)
		for key in ("logradouroEnderecoPrincipal", "numeroEnderecoPrincipal"):
			if key in payload:
				payload[key] = formats.ascii_text(payload[key]) or ("1" if key.startswith("numero") else "")
		if "numeroEnderecoPrincipal" in payload and not formats.digits(payload["numeroEnderecoPrincipal"]):
			payload["numeroEnderecoPrincipal"] = "1"
		return payload
	raise ValueError(f"Unknown ERP: {provider}")
