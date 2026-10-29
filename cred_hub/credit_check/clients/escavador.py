"""Cliente HTTP para Escavador API (v2) — Pesquisa unificada de processos judiciais por CNPJ.

Varre tribunais estaduais (TJs), federais (TRFs), trabalhistas (TRTs) e tribunais superiores (STJ/TST)
em um único endpoint. Classifica polo passivo (réu), execuções fiscais/cíveis e pedidos de falência.
"""

from __future__ import annotations

import re
from typing import Any
import requests

from cred_hub.cnpj.mapping import only_digits

DEFAULT_BASE_URL = "https://api.escavador.com"
DEFAULT_TIMEOUT = 15

RE_BANKRUPTCY = re.compile(r"fal[eê]ncia|recupera[cç][aã]o judicial|recupera[cç][aã]o extrajudicial|concordata", re.I)
RE_EXECUTION = re.compile(r"execu[cç][aã]o fiscal|execu[cç][aã]o de t[ií]tulo|monit[oó]ria|execu[cç][aã]o por quantia certa", re.I)


class EscavadorError(Exception):
	"""Erro base na comunicação com o Escavador."""


class EscavadorAuthError(EscavadorError):
	"""Token de autenticação inválido ou expirado (401)."""


class EscavadorRateLimitError(EscavadorError):
	"""Limite de requisições excedido na API do Escavador (429)."""


def fetch_lawsuits(
	cnpj: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
	"""Consulta os processos judiciais vinculados ao CNPJ e classifica os riscos.

	Retorna dicionário estruturado com totais, processos como réu, falências e execuções críticas.
	"""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta jurídica: {cnpj}")
	if not token or not token.strip():
		raise EscavadorAuthError("Token do Escavador não informado.")

	url = f"{base_url.rstrip('/')}/api/v2/envolvido/processos"
	headers = {
		"Authorization": f"Bearer {token.strip()}",
		"Accept": "application/json",
		"User-Agent": "CredHub-CreditEnrichment/1.0",
	}
	params = {"cpf_cnpj": digits}

	try:
		response = requests.get(url, headers=headers, params=params, timeout=timeout)
	except requests.RequestException as e:
		raise EscavadorError(f"Falha de conexão com a API do Escavador: {e}") from e

	if response.status_code == 401:
		raise EscavadorAuthError("Token de API do Escavador não autorizado ou expirado.")
	if response.status_code == 429:
		raise EscavadorRateLimitError("Limite de requisições do Escavador excedido.")
	if response.status_code == 404:
		# Nenhum processo encontrado para este CNPJ
		return _empty_result(digits)
	if not response.ok:
		raise EscavadorError(f"Erro {response.status_code} na API do Escavador: {response.text[:300]}")

	try:
		payload = response.json()
	except ValueError as e:
		raise EscavadorError(f"Resposta inválida da API do Escavador (não-JSON): {response.text[:200]}") from e

	return parse_escavador_payload(payload, digits)


def parse_escavador_payload(payload: dict[str, Any], digits: str) -> dict[str, Any]:
	"""Analisa o JSON retornado pelo Escavador e extrai indicadores de risco."""
	items = payload.get("items") or []
	envolvido = payload.get("envolvido_encontrado") or {}
	total_envolvido = envolvido.get("quantidade_processos") or len(items)

	defendant_count = 0
	plaintiff_count = 0
	other_count = 0
	has_bankruptcy = False
	highest_value = 0.0
	critical_lawsuits: list[dict[str, Any]] = []

	for item in items:
		numero_cnj = item.get("numero_cnj") or ""
		titulo_polo = item.get("titulo_polo") or ""
		polo = (item.get("polo") or "").upper()
		status = item.get("status") or "EM_ANDAMENTO"
		assuntos = item.get("assuntos") or []
		assuntos_text = " ".join(assuntos) if isinstance(assuntos, list) else str(assuntos)
		full_text = f"{titulo_polo} {assuntos_text}"

		# Classificação de polo (réu x autor)
		is_defendant = polo in ("PASSIVO", "REU", "EXECUTADO", "RECLAMADO", "EMBARGADO")
		is_plaintiff = polo in ("ATIVO", "AUTOR", "EXEQUENTE", "RECLAMANTE", "EMBARGANTE")

		if not polo:
			# Inferência por título: se o nome/CNPJ está após o 'X' ou 'VS', costuma ser réu
			if re.search(r"\b(vs\.?|x)\s+.*" + re.escape(digits), titulo_polo, re.I):
				is_defendant = True
			elif re.search(re.escape(digits) + r".*\b(vs\.?|x)", titulo_polo, re.I):
				is_plaintiff = True

		if is_defendant:
			defendant_count += 1
		elif is_plaintiff:
			plaintiff_count += 1
		else:
			other_count += 1

		# Detecção de falência / recuperação judicial
		is_bankruptcy = bool(RE_BANKRUPTCY.search(full_text))
		if is_bankruptcy:
			has_bankruptcy = True

		# Detecção de execução / cobrança forçada
		is_execution = bool(RE_EXECUTION.search(full_text))

		# Extração de valor da causa (se disponível)
		valor = _extract_lawsuit_value(item)
		if valor > highest_value:
			highest_value = valor

		# Alertas críticos
		if is_bankruptcy or (is_defendant and is_execution) or (is_defendant and valor >= 10000.0):
			fontes = item.get("fontes") or []
			sigla_tribunal = fontes[0].get("sigla") if fontes and isinstance(fontes[0], dict) else ""
			critical_lawsuits.append({
				"numero_cnj": numero_cnj,
				"titulo": titulo_polo,
				"polo": "PASSIVO" if is_defendant else polo or "OUTRO",
				"tribunal": sigla_tribunal,
				"assunto": assuntos[0] if assuntos else "",
				"is_bankruptcy": is_bankruptcy,
				"is_execution": is_execution,
				"valor": valor,
				"status": status,
			})

	return {
		"cnpj": digits,
		"total_count": total_envolvido,
		"defendant_count": defendant_count,
		"plaintiff_count": plaintiff_count,
		"other_count": other_count,
		"highest_value": highest_value,
		"has_bankruptcy_recovery": has_bankruptcy,
		"critical_lawsuits": critical_lawsuits,
		"items_analyzed": len(items),
		"raw_envolvido": envolvido,
	}


def _extract_lawsuit_value(item: dict[str, Any]) -> float:
	"""Extrai o valor numérico da causa quando informado no processo."""
	raw_valor = item.get("valor_causa") or item.get("valor")
	if isinstance(raw_valor, (int, float)):
		return float(raw_valor)
	if isinstance(raw_valor, str):
		# Ex: "R$ 45.820,50" -> 45820.50
		cleaned = re.sub(r"[^\d,\.]", "", raw_valor)
		if "," in cleaned and "." in cleaned:
			cleaned = cleaned.replace(".", "").replace(",", ".")
		elif "," in cleaned:
			cleaned = cleaned.replace(",", ".")
		try:
			return float(cleaned)
		except ValueError:
			return 0.0
	return 0.0


def _empty_result(digits: str) -> dict[str, Any]:
	return {
		"cnpj": digits,
		"total_count": 0,
		"defendant_count": 0,
		"plaintiff_count": 0,
		"other_count": 0,
		"highest_value": 0.0,
		"has_bankruptcy_recovery": False,
		"critical_lawsuits": [],
		"items_analyzed": 0,
		"raw_envolvido": {},
	}
