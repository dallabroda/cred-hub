"""Cliente HTTP para a Direct Data (DirectD) API v3 — apenas os produtos de Crédito e Processos
usados nesta esteira (https://directd.com.br/central-de-ajuda/apis/introducao).

Escopo desta integração (pedido do dono, 29/10/2026): só 3 produtos das categorias "Crédito" e
"Processos" do catálogo DirectD (confirmados na doc pública, não as ~140 APIs restantes):
1. Score de Crédito - QUOD (Crédito) — GET /api/Score — score Quod PF/PJ, R$ 1,98/consulta.
2. Protestos Nacional - IEPTB Básica (Crédito) — GET /api/ProtestosBasica — protestos em
   cartório em TODO o Brasil (a Infosimples/CENPROT já integrada só cobre SP), R$ 2,95/consulta.
3. Processos Judiciais - Simplificada (Processos) — GET /api/ProcessosJudiciaisSimplificada —
   processos em tribunais estaduais, federais e trabalhistas, R$ 1,50/consulta. Resultado
   convertido para o mesmo formato do cliente do Escavador (clients/escavador.py) para reaproveitar
   as mesmas regras de falência/execução/réu em rules.py sem duplicar lógica.
"""

from __future__ import annotations

from typing import Any
import requests

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.clients.escavador import RE_BANKRUPTCY, RE_EXECUTION

DEFAULT_BASE_URL = "https://apiv3.directd.com.br"
DEFAULT_TIMEOUT = 20


class DirectDError(Exception):
	"""Erro base na comunicação com a DirectD."""


class DirectDAuthError(DirectDError):
	"""Token de autenticação inválido (401)."""


def _get_directd(endpoint: str, digits: str, token: str, base_url: str, timeout: int) -> dict[str, Any]:
	if not token or not token.strip():
		raise DirectDAuthError("Token da DirectD não informado.")

	url = f"{base_url.rstrip('/')}/api/{endpoint.lstrip('/')}"
	headers = {
		"Authorization": f"Bearer {token.strip()}",
		"Accept": "application/json",
	}
	params = {"CNPJ": digits}

	try:
		response = requests.get(url, headers=headers, params=params, timeout=timeout)
	except requests.RequestException as e:
		raise DirectDError(f"Falha de rede ao consultar {endpoint} na DirectD: {e}") from e

	if response.status_code in (401, 403):
		raise DirectDAuthError(f"Token da DirectD não autorizado para {endpoint}.")
	if not response.ok:
		raise DirectDError(f"Erro {response.status_code} na DirectD ({endpoint}): {response.text[:300]}")

	try:
		data = response.json()
	except ValueError as e:
		raise DirectDError(f"Resposta não-JSON da DirectD ({endpoint}): {response.text[:200]}") from e

	return data


def fetch_score(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta o Score de Crédito (Quod, via DirectD) da pessoa jurídica."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de score: {cnpj}")

	data = _get_directd("Score", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	pj = retorno.get("pessoaJuridica") or {}

	return {
		"score": pj.get("score"),
		"faixa_score": pj.get("faixaScore") or "",
		"motivos": pj.get("motivos") or [],
		"indicadores_negocio": pj.get("indicadoresNegocio") or [],
	}


def _to_float(value: Any) -> float:
	"""Converte valores monetários que a DirectD às vezes devolve como string formatada."""
	if isinstance(value, (int, float)):
		return float(value)
	if isinstance(value, str):
		cleaned = value.replace("R$", "").strip()
		if "," in cleaned and "." in cleaned:
			cleaned = cleaned.replace(".", "").replace(",", ".")
		elif "," in cleaned:
			cleaned = cleaned.replace(",", ".")
		try:
			return float(cleaned)
		except ValueError:
			return 0.0
	return 0.0


def fetch_protests_nacional(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta Protestos Nacional - IEPTB Básica (cobertura nacional, ao contrário do CENPROT-SP
	já integrado via Infosimples)."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de protestos: {cnpj}")

	data = _get_directd("ProtestosBasica", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}

	return {
		"has_protests": bool(retorno.get("constamProtestos")),
		"protests_count": int(retorno.get("numeroTotalProtestos") or 0),
		"total_value": _to_float(retorno.get("valorTotalProtestos")),
		"cartorios_distintos": int(retorno.get("quantidadeCartoriosDistintos") or 0),
		"cidades_distintas": int(retorno.get("quantidadeCidadesDistintas") or 0),
	}


def merge_protest_sources(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
	"""Combina dois sinais de protesto (ex.: CENPROT-SP via Infosimples + Protestos Nacional via
	DirectD) da forma mais conservadora: OU lógico e maior contagem, para nunca perder um protesto
	que só uma das fontes tenha encontrado."""
	return {
		"has_protests": bool(a.get("has_protests")) or bool(b.get("has_protests")),
		"protests_count": max(int(a.get("protests_count") or 0), int(b.get("protests_count") or 0)),
	}


def parse_directd_processos_payload(payload: dict[str, Any], digits: str) -> dict[str, Any]:
	"""Converte o retorno de ProcessosJudiciaisSimplificada para o mesmo formato usado pelo
	cliente do Escavador (total_count, defendant_count, highest_value, has_bankruptcy_recovery,
	critical_lawsuits), para reaproveitar as regras de risco já existentes em rules.py."""
	retorno = payload.get("retorno") or {}
	processos = retorno.get("processos") or []

	defendant_count = 0
	plaintiff_count = 0
	other_count = 0
	has_bankruptcy = False
	highest_value = 0.0
	critical_lawsuits: list[dict[str, Any]] = []

	for item in processos:
		numero = item.get("numeroProcesso") or ""
		classe = item.get("classe") or ""
		assunto = item.get("assuntoPrincipal") or ""
		full_text = f"{classe} {assunto}"
		status = item.get("status") or ""
		tribunal = item.get("tribunal") or ""
		valor = float(item.get("valorAcao") or 0.0)

		partes = item.get("partes") or []
		is_defendant = any((p.get("polo") or "").upper() in ("PASSIVO", "REU", "EXECUTADO") and p.get("documento") == digits for p in partes)
		is_plaintiff = any((p.get("polo") or "").upper() in ("ATIVO", "AUTOR", "EXEQUENTE") and p.get("documento") == digits for p in partes)
		if not partes:
			# Sem identificação de partes: trata como indeterminado (nunca assume favorável)
			is_defendant = False
			is_plaintiff = False

		if is_defendant:
			defendant_count += 1
		elif is_plaintiff:
			plaintiff_count += 1
		else:
			other_count += 1

		is_bankruptcy = bool(RE_BANKRUPTCY.search(full_text))
		if is_bankruptcy:
			has_bankruptcy = True
		is_execution = bool(RE_EXECUTION.search(full_text))

		if valor > highest_value:
			highest_value = valor

		if is_bankruptcy or (is_defendant and is_execution) or (is_defendant and valor >= 10000.0):
			critical_lawsuits.append({
				"numero_cnj": numero,
				"titulo": assunto,
				"polo": "PASSIVO" if is_defendant else "OUTRO",
				"tribunal": tribunal,
				"assunto": assunto,
				"is_bankruptcy": is_bankruptcy,
				"is_execution": is_execution,
				"valor": valor,
				"status": status,
			})

	return {
		"cnpj": digits,
		"total_count": retorno.get("totalProcessos") or len(processos),
		"defendant_count": defendant_count,
		"plaintiff_count": plaintiff_count,
		"other_count": other_count,
		"highest_value": highest_value,
		"has_bankruptcy_recovery": has_bankruptcy,
		"critical_lawsuits": critical_lawsuits,
		"items_analyzed": len(processos),
		"raw_envolvido": {},
	}


def fetch_lawsuits_directd(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta processos judiciais (Processos Judiciais Simplificada) via DirectD."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de processos: {cnpj}")

	data = _get_directd("ProcessosJudiciaisSimplificada", digits, token, base_url, timeout)
	return parse_directd_processos_payload(data, digits)


def merge_lawsuit_sources(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
	"""Combina dois resultados 'formato Escavador' (ex.: Escavador + DirectD Processos) da forma
	mais conservadora: cada contagem/alerta usa o maior valor ou o OU lógico entre as fontes, para
	nunca perder um sinal de risco que uma das duas tenha encontrado."""
	if a == b:
		return a
	return {
		"cnpj": a.get("cnpj") or b.get("cnpj"),
		"total_count": max(a.get("total_count") or 0, b.get("total_count") or 0),
		"defendant_count": max(a.get("defendant_count") or 0, b.get("defendant_count") or 0),
		"plaintiff_count": max(a.get("plaintiff_count") or 0, b.get("plaintiff_count") or 0),
		"other_count": max(a.get("other_count") or 0, b.get("other_count") or 0),
		"highest_value": max(a.get("highest_value") or 0.0, b.get("highest_value") or 0.0),
		"has_bankruptcy_recovery": bool(a.get("has_bankruptcy_recovery")) or bool(b.get("has_bankruptcy_recovery")),
		"critical_lawsuits": (a.get("critical_lawsuits") or []) + (b.get("critical_lawsuits") or []),
		"items_analyzed": (a.get("items_analyzed") or 0) + (b.get("items_analyzed") or 0),
		"raw_envolvido": a.get("raw_envolvido") or b.get("raw_envolvido") or {},
	}
