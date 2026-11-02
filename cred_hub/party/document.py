"""Regras puras de CPF e de tipo de documento, sem Frappe
(`python -m pytest cred_hub/tests/test_party_document.py`).

Modelo do dono (27/10/2026): Organização é empresa (CNPJ) e Contato é pessoa (CPF). Um documento só
conta como CPF ou CNPJ quando os dígitos verificadores conferem.
"""

from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits

CPF, CNPJ = "cpf", "cnpj"


def is_valid_cpf(value) -> bool:
	digits = only_digits(value)
	if len(digits) != 11 or digits == digits[0] * 11:
		return False
	for size in (9, 10):
		total = sum(int(d) * (size + 1 - i) for i, d in enumerate(digits[:size]))
		if digits[size] != str((total * 10 % 11) % 10):
			return False
	return True


def format_cpf(value) -> str:
	"""000.000.000-00 quando tiver 11 dígitos; senão devolve o texto como veio."""
	digits = only_digits(value)
	if len(digits) != 11:
		return str(value or "").strip()
	return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def document_kind(value) -> str:
	"""CPF, CNPJ ou "" (vazio ou dígitos verificadores que não conferem)."""
	if is_valid_cpf(value):
		return CPF
	if is_valid_cnpj(value):
		return CNPJ
	return ""
