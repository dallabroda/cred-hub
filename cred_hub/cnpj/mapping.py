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


def _address(tipo, logradouro, numero, complemento, bairro) -> str:
	rua = " ".join(p for p in (tipo, logradouro) if p).strip()
	partes = [p for p in (rua, numero, complemento, bairro) if p and str(p).strip()]
	return ", ".join(str(p).strip() for p in partes)


def _socios(lista, nome_key, qualificacao_key, documento_key):
	socios = []
	for socio in lista or []:
		nome = (socio.get(nome_key) or "").strip()
		if not nome:
			continue
		socios.append(
			{
				"nome": nome,
				"qualificacao": (socio.get(qualificacao_key) or "").strip(),
				"documento": (socio.get(documento_key) or "").strip(),
			}
		)
	return socios


def from_opencnpj(data: dict) -> dict:
	cnaes = data.get("cnaes") or []
	principal = next((c for c in cnaes if c.get("is_principal")), cnaes[0] if cnaes else {})
	telefone = next((t for t in data.get("telefones") or [] if not t.get("is_fax")), {})
	return {
		"razao_social": data.get("razao_social") or "",
		"nome_fantasia": data.get("nome_fantasia") or "",
		"situacao_cadastral": (data.get("situacao_cadastral") or "").upper(),
		"data_abertura": data.get("data_inicio_atividade") or None,
		"cnae_principal": " - ".join(p for p in (principal.get("codigo") or data.get("cnae_principal"), principal.get("descricao")) if p),
		"natureza_juridica": data.get("natureza_juridica") or "",
		"porte": data.get("porte_empresa") or "",
		"capital_social": parse_brl_number(data.get("capital_social")),
		"telefone": _phone(telefone.get("ddd"), telefone.get("numero")),
		"email": (data.get("email") or "").lower(),
		"endereco": _address(data.get("tipo_logradouro"), data.get("logradouro"), data.get("numero"),
		                     data.get("complemento"), data.get("bairro")),
		"municipio": data.get("municipio") or "",
		"uf": data.get("uf") or "",
		"cep": data.get("cep") or "",
		"socios": _socios(data.get("QSA") or data.get("qsa"), "nome_socio", "qualificacao_socio", "cnpj_cpf_socio"),
		"fonte": "OpenCNPJ",
	}


def from_brasilapi(data: dict) -> dict:
	tel = only_digits(data.get("ddd_telefone_1"))
	return {
		"razao_social": data.get("razao_social") or "",
		"nome_fantasia": data.get("nome_fantasia") or "",
		"situacao_cadastral": (data.get("descricao_situacao_cadastral") or "").upper(),
		"data_abertura": data.get("data_inicio_atividade") or None,
		"cnae_principal": " - ".join(
			p for p in (str(data.get("cnae_fiscal") or "").zfill(7) if data.get("cnae_fiscal") else "",
			            data.get("cnae_fiscal_descricao")) if p),
		"natureza_juridica": data.get("natureza_juridica") or "",
		"porte": data.get("porte") or "",
		"capital_social": parse_brl_number(data.get("capital_social")),
		"telefone": _phone(tel[:2], tel[2:]) if len(tel) > 2 else "",
		"email": (data.get("email") or "").lower(),
		"endereco": _address(data.get("descricao_tipo_de_logradouro"), data.get("logradouro"), data.get("numero"),
		                     data.get("complemento"), data.get("bairro")),
		"municipio": data.get("municipio") or "",
		"uf": data.get("uf") or "",
		"cep": data.get("cep") or "",
		"socios": _socios(data.get("qsa"), "nome_socio", "qualificacao_socio", "cnpj_cpf_do_socio"),
		"fonte": "BrasilAPI",
	}
