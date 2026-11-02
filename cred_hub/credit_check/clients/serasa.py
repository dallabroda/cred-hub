"""Consulta ao Serasa Experian (Relatório Avançado PJ) com o contrato do próprio cliente (credtech#316).

Conferido contra a homologação em 02/11/2026 (credenciais de homologação da CredTech):
- Login: `POST /security/iam/v1/client-identities/login` com `Authorization: Basic base64(client_id:client_secret)`;
  responde 201 com `accessToken` (Bearer), `tokenType` e `expiresIn`. Atenção: `expiresIn` veio como horário
  (segundos desde 1970), não como duração.
- Consulta: `GET /credit-services/business-information-report/v1/reports?reportName=<relatório>` com o CNPJ no
  cabeçalho `X-Document-Id` e `Authorization: Bearer <token>`. Sem o cabeçalho: 412 pedindo o documento; CNPJ que a
  base não conhece (na homologação, só os CNPJs de teste): 404 `DOCUMENT_NOT_FOUND`.
- O retorno é `reports[]`, um relatório por item (`reportName`), com cadastro, quadro societário, negativações, fatos,
  score e limite de crédito sugerido. Guardamos o JSON inteiro: o dossiê mostra tudo, sem tirar nem pôr.

Nunca retorna sucesso simulado: sem credenciais, a chamada falha com um erro explícito.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import requests

DEFAULT_TIMEOUT = 60
ENVIRONMENTS = {
	"Homologation": "https://uat-api.serasaexperian.com.br",
	"Production": "https://api.serasaexperian.com.br",
}
DEFAULT_REPORT = "RELATORIO_AVANCADO_PJ_PME"
LOGIN_PATH = "/security/iam/v1/client-identities/login"
REPORT_PATH = "/credit-services/business-information-report/v1/reports"
# Renova o token um pouco antes de vencer
TOKEN_MARGIN_SECONDS = 60


class SerasaNotConfigured(Exception):
	"""O cliente ainda não cadastrou o client id e o client secret do contrato Serasa."""


class SerasaError(Exception):
	"""Erro na comunicação com o Serasa Experian."""


class SerasaDocumentNotFound(SerasaError):
	"""A Serasa não tem o documento consultado (na homologação, só os CNPJs de teste existem)."""


def _digits(value: str) -> str:
	return "".join(ch for ch in str(value or "") if ch.isdigit())


def token_expiry(expires_in: Any, now: float | None = None) -> float:
	"""Horário (segundos desde 1970) em que o token vence. A Serasa devolve `expiresIn` como horário absoluto;
	se um dia vier como duração (poucos milhares de segundos), soma ao agora."""
	now = time.time() if now is None else now
	try:
		value = float(expires_in)
	except (TypeError, ValueError):
		return now + 600
	return value if value > 10**9 else now + value


def _error_message(response) -> str:
	"""A Serasa responde erro como `[{"code": "...", "message": "..."}]`."""
	try:
		body = response.json()
	except ValueError:
		return response.text[:300]
	items = body if isinstance(body, list) else [body]
	messages = [str(item.get("message") or item) for item in items if isinstance(item, dict)]
	return " ".join(messages)[:400] or response.text[:300]


class SerasaClient:
	def __init__(
		self,
		client_id: str,
		client_secret: str,
		environment: str = "Homologation",
		token: str = "",
		token_expires: float = 0,
		session=None,
		timeout: int = DEFAULT_TIMEOUT,
	):
		self.client_id = (client_id or "").strip()
		self.client_secret = (client_secret or "").strip()
		self.base_url = ENVIRONMENTS.get(environment or "Homologation", ENVIRONMENTS["Homologation"])
		self.token = token or ""
		self.token_expires = float(token_expires or 0)
		self.session = session or requests
		self.timeout = timeout

	def login(self) -> str:
		if not self.client_id or not self.client_secret:
			raise SerasaNotConfigured(
				"Serasa Experian não está configurado: informe o client id e o client secret do contrato "
				"em Configurações > Bureaus de Crédito antes de consultar."
			)
		basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
		try:
			response = self.session.post(
				f"{self.base_url}{LOGIN_PATH}",
				headers={"Content-Type": "application/json", "Authorization": f"Basic {basic}"},
				timeout=self.timeout,
			)
		except requests.RequestException as e:
			raise SerasaError(f"Falha de rede ao entrar no Serasa: {e}") from e
		if response.status_code in (401, 403):
			raise SerasaError("O Serasa recusou o client id e o client secret.")
		if not response.ok:
			raise SerasaError(f"Erro {response.status_code} ao entrar no Serasa: {_error_message(response)}")
		try:
			body = response.json()
		except ValueError as e:
			raise SerasaError(f"Resposta não-JSON do Serasa no login: {response.text[:200]}") from e
		token = body.get("accessToken")
		if not token:
			raise SerasaError("O Serasa não devolveu o token de acesso.")
		self.token = token
		self.token_expires = token_expiry(body.get("expiresIn"))
		return token

	def _bearer(self) -> str:
		if not self.token or self.token_expires - TOKEN_MARGIN_SECONDS <= time.time():
			self.login()
		return self.token

	def fetch_report(self, cnpj: str, report_name: str = DEFAULT_REPORT) -> dict[str, Any]:
		"""O relatório do CNPJ, exatamente como a Serasa devolveu."""
		digits = _digits(cnpj)
		if len(digits) != 14:
			raise ValueError(f"CNPJ inválido para consulta ao Serasa: {cnpj}")
		report_name = (report_name or DEFAULT_REPORT).strip()

		response = self._get_report(digits, report_name)
		if response.status_code == 401:
			# token recusado (vencido antes da hora): entra de novo uma vez
			self.token = ""
			response = self._get_report(digits, report_name)

		if response.status_code == 404:
			raise SerasaDocumentNotFound(_error_message(response))
		if response.status_code in (401, 403):
			raise SerasaError("O Serasa recusou a consulta: o contrato pode não incluir este relatório.")
		if not response.ok:
			raise SerasaError(f"Erro {response.status_code} na consulta ao Serasa: {_error_message(response)}")
		try:
			data = response.json()
		except ValueError as e:
			raise SerasaError(f"Resposta não-JSON do Serasa: {response.text[:200]}") from e
		return data

	def _get_report(self, digits: str, report_name: str):
		try:
			return self.session.get(
				f"{self.base_url}{REPORT_PATH}",
				params={"reportName": report_name},
				headers={
					"Authorization": f"Bearer {self._bearer()}",
					"Accept": "application/json",
					"X-Document-Id": digits,
				},
				timeout=self.timeout,
			)
		except requests.RequestException as e:
			raise SerasaError(f"Falha de rede ao consultar o Serasa: {e}") from e
