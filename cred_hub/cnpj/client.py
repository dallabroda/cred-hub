"""Consulta de CNPJ: OpenCNPJ (principal) com reserva na BrasilAPI. Sem Frappe, para poder testar isolado."""

import logging

import requests

from cred_hub.cnpj.mapping import from_brasilapi, from_opencnpj, only_digits

logger = logging.getLogger(__name__)

SOURCES = (
	("OpenCNPJ", "https://api.opencnpj.org/{cnpj}", from_opencnpj),
	("BrasilAPI", "https://brasilapi.com.br/api/cnpj/v1/{cnpj}", from_brasilapi),
)
TIMEOUT = 10


VIACEP = "https://viacep.com.br/ws/{cep}/json/"


def ibge_for_cep(cep, session=None) -> str:
	"""Código IBGE do município pelo CEP (ViaCEP, grátis). "" quando não acha: nunca derruba o enriquecimento."""
	digits = only_digits(cep)
	if len(digits) != 8:
		return ""
	try:
		resp = (session or requests).get(VIACEP.format(cep=digits), timeout=TIMEOUT, headers={"Accept": "application/json"})
		return only_digits((resp.json() or {}).get("ibge")) if resp.status_code == 200 else ""
	except Exception:  # noqa: BLE001 - best effort: a CEP lookup must never break the CNPJ enrichment
		return ""


class CnpjNotFound(Exception):
	"""Nenhuma fonte conhece o CNPJ."""


class CnpjUnavailable(Exception):
	"""As fontes falharam (rede, limite de consultas, erro do serviço)."""


def fetch(cnpj, session=None) -> dict:
	"""Devolve os dados no formato de `mapping.from_*`. Levanta CnpjNotFound ou CnpjUnavailable."""
	digits = only_digits(cnpj)
	http = session or requests
	not_found = 0
	errors = []
	for name, url, convert in SOURCES:
		try:
			resp = http.get(url.format(cnpj=digits), timeout=TIMEOUT, headers={"Accept": "application/json"})
		except requests.RequestException as e:
			errors.append(f"{name}: {e.__class__.__name__}")
			continue
		if resp.status_code == 404:
			not_found += 1
			continue
		if resp.status_code != 200:
			errors.append(f"{name}: HTTP {resp.status_code}")
			continue
		try:
			data = convert(resp.json())
		except (ValueError, KeyError, TypeError, AttributeError) as e:
			# Resposta fora do formato esperado: tenta a próxima fonte
			errors.append(f"{name}: resposta inesperada ({e})")
			logger.warning("Resposta inesperada de %s para %s: %s", name, digits, e)
			continue
		if not data.get("ibge_code"):
			data["ibge_code"] = ibge_for_cep((data.get("address") or {}).get("pincode"), http)
		return data
	if not_found == len(SOURCES):
		raise CnpjNotFound(digits)
	raise CnpjUnavailable("; ".join(errors) or "sem resposta")
