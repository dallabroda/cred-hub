"""Every field the ERP accepts in the cedente registration, so the company can map each one (credtech#367).

Pure Python. The de-para (Cred Hub ERP Field Map rows) stores only the fields the company sends; this catalog lists
all of them, with the transform each one needs (the company picks the CRM source, never the format).

Kinds:
  DATA -> filled from a CRM field or a fixed value
  LIST -> a name picked from one of the ERP's own lists (operator, collector...); the id is looked up when sending
  AUTO -> filled by the sending step itself (master contract number, sector by the CNAE, manager, tax regime)

Smart Securities: POST /smartsecurities/cedente, "exemplos de chamadas api/smart/smart_securities_api.md".
"""

from . import SMART, WBA
from .mapper import AUTO, DEFAULT_RULES

DATA, LIST, AUTO_KIND = "data", "list", "auto"
REQUIRED, PF = "yes", "pf"  # pf: required by the ERP only for a person (CPF)


def _data(erp_field, label, transform="", required=""):
	return {"erp_field": erp_field, "label": label, "kind": DATA, "transform": transform, "required": required}


def _list(erp_field, label, resource, id_key):
	return {"erp_field": erp_field, "label": label, "kind": LIST, "transform": "", "required": "", "resource": resource, "id_key": id_key}


def _auto(erp_field, label):
	return {"erp_field": erp_field, "label": label, "kind": AUTO_KIND, "transform": "", "required": ""}


def _phone(position):
	n = position + 1
	return [
		_data(f"telefones[{position}].ddd", f"Phone {n} area code", "phone_ddd"),
		_data(f"telefones[{position}].numero", f"Phone {n}", "phone_number"),
		_data(f"telefones[{position}].nomeContato", f"Phone {n} contact name", "ascii"),
	]


def _address(suffix, label, required):
	flag = REQUIRED if required else ""
	return [
		_data(f"cepEndereco{suffix}", f"ZIP code ({label})", "int", flag),
		_data(f"logradouroEndereco{suffix}", f"Street ({label})", "street", flag),
		_data(f"numeroEndereco{suffix}", f"Number ({label})", "street_number", flag),
		_data(f"complementoEndereco{suffix}", f"Complement ({label})", "ascii"),
		_data(f"bairroEndereco{suffix}", f"District ({label})", "ascii", flag),
		_data(f"codigoMunicipioIbgeEndereco{suffix}", f"IBGE city code ({label})", "int", flag),
	]


SMART_FIELDS = [
	_data("cpfCnpj", "CPF/CNPJ", "digits", REQUIRED),
	_data("razaoSocial", "Legal name", "ascii", REQUIRED),
	_data("nomeFantasia", "Trade name", "ascii"),
	_data("clienteDesde", "Client since", "br_date", REQUIRED),
	_auto("numeroContratoMae", "Master contract number (next number from the Smart)"),
	_data("emailPrincipal", "Main e-mail", "lower", REQUIRED),
	_auto("idSetor", "Sector (from the CNAE, by the Smart's sector names)"),
	_auto("idGerente", "Manager (card owner, else the default manager)"),
	_auto("idRegimeTributario", "Tax regime (from the Receita)"),
	_data("dataFundacao", "Foundation date", "br_date"),
	_data("capitalSocial", "Share capital", "br_money"),
	_data("inscricaoEstadual", "State registration", "trim"),
	_data("cnae", "CNAE", "cnae_code"),
	_data("dataNascimento", "Birth date (person)", "br_date", PF),
	_list("idEstadoCivil", "Marital status (person)", "estado-civil", "idEstadoCivil"),
	_list("idFonteCaptacao", "Acquisition source", "fonte-captacao", "idFonteCaptacao"),
	_list("idGrupoAnaliseVadu", "Vadu analysis group", "grupo-analise-vadu", "idGrupoAnaliseVadu"),
	_list("idOperador", "Operator", "operador", "idOperador"),
	_data("cpfCnpjOperador", "Operator CPF/CNPJ", "digits"),
	_list("idCaptador", "Originator", "captador", "idCaptador"),
	_data("cpfCnpjCaptador", "Originator CPF/CNPJ", "digits"),
	_list("idControlador", "Controller", "controlador", "idControlador"),
	_data("cpfCnpjControlador", "Controller CPF/CNPJ", "digits"),
	_list("idResponsavelCobranca", "Collection officer", "responsavel-cobranca", "idResponsavelCobranca"),
	_data("cpfCnpjResponsavelCobranca", "Collection officer CPF/CNPJ", "digits"),
	*_address("Principal", "main", True),
	_data("enderecoCobrancaIgualAoEnderecoPrincipal", "Billing address = main", "int"),
	*_address("Cobranca", "billing", False),
	*_phone(0),
	*_phone(1),
]


def _from_rules(rules):
	return [
		{
			"erp_field": r["erp_field"],
			"label": r["label"],
			"kind": AUTO_KIND if r["source"] == AUTO else DATA,
			"transform": r["transform"],
			"required": REQUIRED if r["required"] else "",
		}
		for r in rules
	]


# WBA: the fields of its registration that the default de-para already covers (no wider documentation yet)
CATALOG = {SMART: SMART_FIELDS, WBA: _from_rules(DEFAULT_RULES[WBA])}


def fields(provider) -> list[dict]:
	return CATALOG.get(provider) or []


def field(provider, erp_field) -> dict | None:
	return next((f for f in fields(provider) if f["erp_field"] == erp_field), None)
