"""Integração sob demanda com o Serasa Experian (Relato PJ) — bureau final e tarifado.

Regra de Segurança Arquitetural:
Consultas ao Serasa (tarifadas por relatório completo, R$ 25 a R$ 40, pagas com a chave/contrato
do próprio cliente) só podem ser acionadas quando o lead estiver estritamente 'Pre-Approved'
(Aprovado no Pré-Filtro) da esteira de baixo custo.

Nunca retorna sucesso simulado: sem credenciais reais configuradas, a chamada falha com um erro
explícito — "Solicitado" só aparece quando o Serasa de fato respondeu.
"""

from __future__ import annotations

from typing import Any
import requests

DEFAULT_TIMEOUT = 30


class BureauGateBlocked(Exception):
	"""Disparada quando uma consulta a birô caro é tentada sem aprovação prévia no pré-filtro."""


class SerasaNotConfigured(Exception):
	"""Disparada quando o cliente ainda não configurou a URL/token do contrato Serasa."""


class SerasaError(Exception):
	"""Erro na comunicação com o Serasa Experian."""


def assert_pre_approved_for_bureau(verdict: str, cnpj: str) -> None:
	"""Garante que o CNPJ passou na esteira preliminar antes de consumir birô caro."""
	if verdict != "Pre-Approved":
		raise BureauGateBlocked(
			f"Consulta ao birô final bloqueada para o CNPJ {cnpj}: parecer atual é '{verdict}'. "
			"Apenas leads com 'Pre-Approved' podem ser consultados para evitar custos desnecessários."
		)


def trigger_serasa_relato(
	cnpj: str,
	user: str,
	verdict: str,
	endpoint_url: str = "",
	token: str = "",
	timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
	"""Aciona a chamada real ao Serasa Relato PJ, com trava de pré-aprovação.

	`endpoint_url` e `token` vêm do contrato do próprio cliente com o Serasa (Cred Hub Credit
	Settings) — cada cliente tem um endpoint próprio conforme seu plano. Sem os dois, a função
	falha explicitamente — nunca finge que a consulta foi enviada.
	"""
	assert_pre_approved_for_bureau(verdict, cnpj)

	endpoint_url = (endpoint_url or "").strip()
	token = (token or "").strip()
	if not endpoint_url or not token:
		raise SerasaNotConfigured(
			"Serasa Experian não está configurado: informe o endpoint e o token do contrato em "
			"Configurações > Bureaus de Crédito antes de solicitar o Relato PJ."
		)

	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
		"Accept": "application/json",
	}
	payload = {"cnpj": cnpj, "relato": "PJ", "requested_by": user}

	try:
		response = requests.post(endpoint_url, json=payload, headers=headers, timeout=timeout)
	except requests.RequestException as e:
		raise SerasaError(f"Falha de rede ao consultar o Serasa: {e}") from e

	if response.status_code in (401, 403):
		raise SerasaError("Token do Serasa não autorizado ou expirado.")
	if not response.ok:
		raise SerasaError(f"Erro {response.status_code} na consulta ao Serasa: {response.text[:300]}")

	try:
		data = response.json()
	except ValueError as e:
		raise SerasaError(f"Resposta não-JSON do Serasa: {response.text[:200]}") from e

	return {
		"status": data.get("status") or "Solicitado",
		"provider": "Serasa Experian",
		"service": "Relato PJ",
		"cnpj": cnpj,
		"requested_by": user,
		"authorized": True,
		"protocol": data.get("protocolo") or data.get("protocol") or "",
		"raw": data,
	}
