"""Cliente HTTP para Infosimples API (v2) — Regularidade Fiscal, CNDs e Protestos em Cartório.

Endpoints integrados:
1. PGFN — Dívida Ativa da União e FGTS (/receita-federal/pgfn-devedores)
2. CND Federal — Certidão Conjunta Receita Federal / Fazenda Nacional (/receita-federal/cnd)
3. CNDT Trabalhista — Banco Nacional de Devedores Trabalhistas - TST (/tst/cndt)
4. CENPROT — Consulta indicativa de títulos protestados em cartório (/protesto/cenprot-sp)
"""

from __future__ import annotations

import logging
from typing import Any
import requests

from cred_hub.cnpj.mapping import only_digits

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.infosimples.com"
DEFAULT_TIMEOUT = 20


class InfosimplesError(Exception):
	"""Erro base na comunicação com a Infosimples."""


class InfosimplesAuthError(InfosimplesError):
	"""Token de autenticação inválido ou sem saldo (401/402)."""


def fetch_fiscal_and_protests(
	cnpj: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	include_pgfn: bool = True,
	include_cnd: bool = True,
	include_cndt: bool = True,
	include_cenprot: bool = True,
) -> dict[str, Any]:
	"""Realiza as consultas fiscais e de cartório com isolamento de falhas por endpoint."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta fiscal: {cnpj}")
	if not token or not token.strip():
		raise InfosimplesAuthError("Token da Infosimples não informado.")

	result: dict[str, Any] = {
		"cnpj": digits,
		"pgfn": {"status": "Não consultado", "total_debt": 0.0, "inscriptions_count": 0, "inscriptions": []},
		"cnd_federal": {"status": "Não consultado", "type": "", "pdf_url": "", "is_regular": True},
		"cndt": {"status": "Não consultado", "is_regular": True, "pdf_url": ""},
		"cenprot": {"has_protests": False, "protests_count": 0, "cartorios": []},
		"errors": [],
	}

	# 1. PGFN Dívida Ativa da União
	if include_pgfn:
		try:
			result["pgfn"] = query_pgfn(digits, token, base_url, timeout)
		except Exception as e:
			logger.warning("Falha na consulta PGFN (%s): %s", digits, e)
			result["errors"].append({"service": "pgfn", "error": str(e)})

	# 2. CND Federal
	if include_cnd:
		try:
			result["cnd_federal"] = query_cnd_federal(digits, token, base_url, timeout)
		except Exception as e:
			logger.warning("Falha na consulta CND Federal (%s): %s", digits, e)
			result["errors"].append({"service": "cnd_federal", "error": str(e)})

	# 3. CNDT Trabalhista
	if include_cndt:
		try:
			result["cndt"] = query_cndt(digits, token, base_url, timeout)
		except Exception as e:
			logger.warning("Falha na consulta CNDT (%s): %s", digits, e)
			result["errors"].append({"service": "cndt", "error": str(e)})

	# 4. CENPROT Protestos
	if include_cenprot:
		try:
			result["cenprot"] = query_cenprot(digits, token, base_url, timeout)
		except Exception as e:
			logger.warning("Falha na consulta CENPROT (%s): %s", digits, e)
			result["errors"].append({"service": "cenprot", "error": str(e)})

	return result


def _post_infosimples(endpoint: str, digits: str, token: str, base_url: str, timeout: int) -> dict[str, Any]:
	url = f"{base_url.rstrip('/')}/api/v2/consultas/{endpoint.lstrip('/')}"
	headers = {
		"Authorization": f"Bearer {token.strip()}",
		"Content-Type": "application/json",
		"Accept": "application/json",
	}
	payload = {"cnpj": digits, "token": token.strip()}

	try:
		response = requests.post(url, json=payload, headers=headers, timeout=timeout)
	except requests.RequestException as e:
		raise InfosimplesError(f"Falha de rede ao consultar {endpoint}: {e}") from e

	if response.status_code in (401, 403):
		raise InfosimplesAuthError(f"Token não autorizado para o serviço {endpoint}.")
	if not response.ok:
		raise InfosimplesError(f"Erro {response.status_code} na consulta {endpoint}: {response.text[:200]}")

	try:
		data = response.json()
	except ValueError as e:
		raise InfosimplesError(f"Resposta não-JSON da Infosimples em {endpoint}: {response.text[:150]}") from e

	return data


def query_pgfn(digits: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta débitos na Dívida Ativa da União e FGTS."""
	data = _post_infosimples("receita-federal/pgfn-devedores", digits, token, base_url, timeout)
	items = data.get("data") or []
	if not items:
		return {
			"status": "Regular / Sem Débitos",
			"total_debt": 0.0,
			"inscriptions_count": 0,
			"inscriptions": [],
		}

	total_debt = 0.0
	inscriptions_list = []
	for item in items:
		val = float(item.get("valor_total_consolidado") or 0.0)
		total_debt += val
		for insc in item.get("inscricoes") or []:
			inscriptions_list.append({
				"numero": insc.get("numero_inscricao"),
				"receita": insc.get("receita_principal"),
				"valor": float(insc.get("valor_consolidado") or 0.0),
				"situacao": insc.get("situacao"),
				"unidade": insc.get("unidade_responsavel"),
			})

	status = "Inscrito na Dívida Ativa" if total_debt > 0 or inscriptions_list else "Regular"
	return {
		"status": status,
		"total_debt": total_debt,
		"inscriptions_count": len(inscriptions_list),
		"inscriptions": inscriptions_list,
	}


def query_cnd_federal(digits: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta a Certidão Conjunta Negativa de Débitos Federais (Receita e PGFN)."""
	data = _post_infosimples("receita-federal/cnd", digits, token, base_url, timeout)
	items = data.get("data") or []
	if not items:
		return {"status": "Não emitida / Desconhecida", "type": "", "pdf_url": "", "is_regular": True}

	first = items[0] if isinstance(items, list) else items
	cert_type = (first.get("tipo_certidao") or first.get("situacao") or "").upper()
	pdf_url = first.get("site_recibo") or first.get("pdf_url") or first.get("url_certidao") or ""

	is_regular = "POSITIVA" not in cert_type or "EFEITO" in cert_type or "NEGATIVA" in cert_type
	status_desc = "Negativa (Regular)" if "NEGATIVA" in cert_type and "EFEITO" not in cert_type else (
		"Positiva com efeito de Negativa" if "EFEITO" in cert_type else "Positiva (Com Débitos)"
	)

	return {
		"status": status_desc,
		"type": cert_type,
		"pdf_url": pdf_url,
		"is_regular": is_regular,
		"validade": first.get("validade") or "",
		"numero_certidao": first.get("numero_certidao") or "",
	}


def query_cndt(digits: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta débitos no Banco Nacional de Devedores Trabalhistas (TST)."""
	data = _post_infosimples("tst/cndt", digits, token, base_url, timeout)
	items = data.get("data") or []
	if not items:
		return {"status": "Regular", "is_regular": True, "pdf_url": ""}

	first = items[0] if isinstance(items, list) else items
	consta = bool(first.get("consta_bndt") or "POSITIVA" in str(first.get("tipo_certidao") or "").upper())
	pdf_url = first.get("pdf_url") or first.get("url_certidao") or ""

	return {
		"status": "Irregular (Consta no BNDT)" if consta else "Regular",
		"is_regular": not consta,
		"pdf_url": pdf_url,
		"numero_certidao": first.get("numero_certidao") or "",
	}


def query_cenprot(digits: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta indicativa de títulos protestados em cartório."""
	data = _post_infosimples("protesto/cenprot-sp", digits, token, base_url, timeout)
	items = data.get("data") or []
	if not items:
		return {"has_protests": False, "protests_count": 0, "cartorios": []}

	first = items[0] if isinstance(items, list) else items
	protestos_count = int(first.get("quantidade_protestos") or 0)
	has_protest = bool(first.get("consta_protesto") or protestos_count > 0)
	cartorios = first.get("cartorios") or []

	return {
		"has_protests": has_protest,
		"protests_count": protestos_count,
		"cartorios": cartorios,
	}
