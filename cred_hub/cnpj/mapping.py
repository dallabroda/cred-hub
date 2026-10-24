"""Regras puras de CNPJ (sem Frappe): limpeza, dígitos verificadores, números no formato
brasileiro e conversão das respostas da OpenCNPJ e da BrasilAPI para um formato único."""

import re
from decimal import Decimal, InvalidOperation

_PESOS_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_PESOS_2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def only_digits(value) -> str:
	return re.sub(r"\D", "", str(value or ""))


def is_valid_cnpj(value) -> bool:
	digits = only_digits(value)
	if len(digits) != 14 or digits == digits[0] * 14:
		return False

	def check_digit(base, weights):
		rest = sum(int(d) * w for d, w in zip(base, weights)) % 11
		return "0" if rest < 2 else str(11 - rest)

	first = check_digit(digits[:12], _PESOS_1)
	second = check_digit(digits[:12] + first, _PESOS_2)
	return digits[12:] == first + second


def format_cnpj(value) -> str:
	"""00.000.000/0000-00 quando tiver 14 dígitos; senão devolve o texto como veio."""
	digits = only_digits(value)
	if len(digits) != 14:
		return str(value or "").strip()
	return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def parse_brl_number(value):
	"""Converte números vindos das APIs: 205431960490.52, "205431960490,52", "1.234.567,89".

	Devolve float ou None. Corrige o erro antigo `could not convert string to float` (issue #58).
	"""
	if value is None or value == "":
		return None
	if isinstance(value, (int, float)):
		return float(value)
	text = str(value).strip().replace("R$", "").replace(" ", "")
	if "," in text:
		# Formato brasileiro: ponto separa milhar, vírgula separa decimais
		text = text.replace(".", "").replace(",", ".")
	try:
		return float(Decimal(text))
	except (InvalidOperation, ValueError):
		return None


def _phone(ddd, numero) -> str:
	return f"({ddd}) {numero}" if ddd and numero else (numero or "")


# Porte da Receita resumido (decisão do dono, 24/10/2026). Valores em inglês no banco; pt-BR no .po:
# ME -> ME, EPP -> EPP, Other size -> Demais, Not informed -> Não informado
COMPANY_SIZES = ("ME", "EPP", "Other size", "Not informed")
_SIZE_BY_CODE = {"01": "ME", "1": "ME", "03": "EPP", "3": "EPP", "05": "Other size", "5": "Other size"}


def company_size(code=None, text=None) -> str:
	"""Código (01/03/05) ou texto da Receita -> ME, EPP, Other size ou Not informed."""
	by_code = _SIZE_BY_CODE.get(str(code).strip()) if code not in (None, "") else None
	if by_code:
		return by_code
	label = str(text or "").upper()
	if "(ME)" in label or label.startswith("MICRO"):
		return "ME"
	if "EPP" in label or "PEQUENO PORTE" in label:
		return "EPP"
	if "DEMAIS" in label:
		return "Other size"
	return "Not informed"


def _yes_no(value) -> str:
	"""S/N, true/false da Receita -> Yes/No (traduzido Sim/Não). Só "S"/true é sim: a Receita deixa a opção
	vazia para quem nunca optou (ex.: S/A); vazio só quando a fonte não traz o campo (None)."""
	if value is None:
		return ""
	if isinstance(value, bool):
		return "Yes" if value else "No"
	return "Yes" if str(value).strip().upper() in ("S", "SIM", "TRUE", "1") else "No"


def _cep(value) -> str:
	digits = only_digits(value)
	return f"{digits[:5]}-{digits[5:]}" if len(digits) == 8 else str(value or "").strip()


def _clean(*parts) -> list[str]:
	return [" ".join(str(p).split()) for p in parts if p and str(p).strip()]


def _address(tipo, logradouro, numero, complemento, bairro, municipio, uf, cep) -> dict:
	"""Partes do endereço para criar o Address do Frappe (bairro vai junto do complemento na linha 2)."""
	rua = " ".join(_clean(tipo, logradouro))
	return {
		"address_line1": ", ".join(_clean(rua, numero)),
		"address_line2": ", ".join(_clean(complemento, bairro)),
		"city": (municipio or "").strip(),
		"state": (uf or "").strip(),
		"pincode": _cep(cep),
	}


def _partners(lista, nome_key, qualificacao_key, documento_key):
	partners = []
	for socio in lista or []:
		nome = (socio.get(nome_key) or "").strip()
		if not nome:
			continue
		partners.append(
			{
				"name": nome,
				"qualification": (socio.get(qualificacao_key) or "").strip(),
				"document": (socio.get(documento_key) or "").strip(),
			}
		)
	return partners


def from_opencnpj(data: dict) -> dict:
	cnaes = data.get("cnaes") or []
	principal = next((c for c in cnaes if c.get("is_principal")), cnaes[0] if cnaes else {})
	telefone = next((t for t in data.get("telefones") or [] if not t.get("is_fax")), {})
	return {
		"legal_name": data.get("razao_social") or "",
		"trade_name": data.get("nome_fantasia") or "",
		"registration_status": (data.get("situacao_cadastral") or "").upper(),
		"activity_start_date": data.get("data_inicio_atividade") or None,
		"main_cnae": " - ".join(p for p in (principal.get("codigo") or data.get("cnae_principal"), principal.get("descricao")) if p),
		"legal_nature": data.get("natureza_juridica") or "",
		"company_size": company_size(text=data.get("porte_empresa")),
		"is_mei": _yes_no(data.get("opcao_mei")),
		"share_capital": parse_brl_number(data.get("capital_social")),
		"registry_phone": _phone(telefone.get("ddd"), telefone.get("numero")),
		"registry_email": (data.get("email") or "").lower(),
		"neighborhood": (data.get("bairro") or "").strip(),
		"city": (data.get("municipio") or "").strip(),
		"state": (data.get("uf") or "").strip(),
		"address": _address(data.get("tipo_logradouro"), data.get("logradouro"), data.get("numero"),
		                    data.get("complemento"), data.get("bairro"), data.get("municipio"), data.get("uf"), data.get("cep")),
		"partners": _partners(data.get("QSA") or data.get("qsa"), "nome_socio", "qualificacao_socio", "cnpj_cpf_socio"),
		"source": "OpenCNPJ",
	}


def from_brasilapi(data: dict) -> dict:
	tel = only_digits(data.get("ddd_telefone_1"))
	return {
		"legal_name": data.get("razao_social") or "",
		"trade_name": data.get("nome_fantasia") or "",
		"registration_status": (data.get("descricao_situacao_cadastral") or "").upper(),
		"activity_start_date": data.get("data_inicio_atividade") or None,
		"main_cnae": " - ".join(
			p for p in (str(data.get("cnae_fiscal") or "").zfill(7) if data.get("cnae_fiscal") else "",
			            data.get("cnae_fiscal_descricao")) if p),
		"legal_nature": data.get("natureza_juridica") or "",
		"company_size": company_size(code=data.get("codigo_porte"), text=data.get("porte")),
		"is_mei": _yes_no(data.get("opcao_pelo_mei")),
		"share_capital": parse_brl_number(data.get("capital_social")),
		"registry_phone": _phone(tel[:2], tel[2:]) if len(tel) > 2 else "",
		"registry_email": (data.get("email") or "").lower(),
		"neighborhood": (data.get("bairro") or "").strip(),
		"city": (data.get("municipio") or "").strip(),
		"state": (data.get("uf") or "").strip(),
		"address": _address(data.get("descricao_tipo_de_logradouro"), data.get("logradouro"), data.get("numero"),
		                    data.get("complemento"), data.get("bairro"), data.get("municipio"), data.get("uf"), data.get("cep")),
		"partners": _partners(data.get("qsa"), "nome_socio", "qualificacao_socio", "cnpj_cpf_do_socio"),
		"source": "BrasilAPI",
	}
