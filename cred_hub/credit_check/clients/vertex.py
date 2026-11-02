"""Cliente do Google Vertex AI (Gemini) para o parecer de crédito (credtech#316).

Decisões:
- Vertex AI, nunca a Gemini API do AI Studio (docs/juridico/ia-provedores-lgpd.md).
- Modelo `gemini-3.8-flash` (dono, 02/11/2026). Mesma conta de serviço e projeto do SmartChat.
- Chamada REST `generateContent` com a credencial da conta de serviço (google-auth, que já vem no Frappe): nenhuma
  dependência nova. Resposta em JSON com esquema (`responseMimeType` + `responseSchema`), validada de novo aqui.
- Região: `global` usa `aiplatform.googleapis.com`; uma região (ex.: `southamerica-east1`) usa o endpoint regional.
- O custo de cada chamada é registrado: tokens de entrada, de saída e de raciocínio (que o Google cobra como saída).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_TIMEOUT = 180
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# Preço de lista em US$ por 1 milhão de tokens (página oficial de preços do Gemini, lida em 02/11/2026). O
# raciocínio é cobrado como saída. Usado só para registrar o custo de cada parecer, nunca para cobrar o cliente.
PRICES_USD_PER_MTOK = {
	"gemini-3.8-flash": {"input": 0.75, "output": 3.75},
	"gemini-3.6-flash": {"input": 0.75, "output": 3.75},
	"gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}


class VertexError(Exception):
	"""Falha na chamada ao Vertex AI."""


class VertexNotConfigured(VertexError):
	"""Falta projeto, região ou credencial do Vertex AI neste site."""


class VertexInvalidOutput(VertexError):
	"""O modelo respondeu, mas fora do formato pedido (JSON inválido ou cortado). Vale tentar de novo."""

	def __init__(self, message: str, result: VertexResult | None = None):
		super().__init__(message)
		self.result = result


@dataclass
class VertexResult:
	text: str
	finish_reason: str
	prompt_tokens: int
	output_tokens: int
	thinking_tokens: int
	model: str
	seconds: float

	@property
	def cost_usd(self) -> float:
		price = PRICES_USD_PER_MTOK.get(self.model)
		if not price:
			return 0.0
		billed_output = self.output_tokens + self.thinking_tokens
		return round((self.prompt_tokens * price["input"] + billed_output * price["output"]) / 1_000_000, 6)


def endpoint(project: str, location: str, model: str) -> str:
	host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
	return f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"


def service_account_token(credentials_path: str) -> str:
	"""Token de acesso (Bearer) da conta de serviço, pelo google-auth."""
	from google.auth.transport.requests import Request
	from google.oauth2 import service_account

	try:
		credentials = service_account.Credentials.from_service_account_file(credentials_path, scopes=list(SCOPES))
		credentials.refresh(Request())
	except FileNotFoundError as e:
		raise VertexNotConfigured(f"Arquivo da conta de serviço do Google não encontrado: {credentials_path}") from e
	except Exception as e:
		raise VertexError(f"Não foi possível autenticar no Google: {e}") from e
	return credentials.token


def _text_of(body: dict[str, Any]) -> tuple[str, str]:
	"""Texto da resposta (sem as partes de raciocínio) e o motivo de término."""
	candidates = body.get("candidates") or []
	if not candidates:
		feedback = body.get("promptFeedback") or {}
		raise VertexError(f"O Gemini não devolveu resposta ({feedback.get('blockReason') or 'sem candidatos'}).")
	candidate = candidates[0]
	parts = (candidate.get("content") or {}).get("parts") or []
	text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
	return text, candidate.get("finishReason") or ""


class VertexClient:
	def __init__(self, project: str, location: str, credentials_path: str, session=None, token_provider=None, timeout=DEFAULT_TIMEOUT):
		if not project or not location or not credentials_path:
			raise VertexNotConfigured("Vertex AI não está configurado neste site (projeto, região e conta de serviço).")
		self.project = project
		self.location = location
		self.credentials_path = credentials_path
		self.session = session or requests
		self.token_provider = token_provider or service_account_token
		self.timeout = timeout

	def generate_json(
		self,
		model: str,
		system: str,
		user_text: str,
		schema: dict[str, Any],
		max_output_tokens: int = 32768,
	) -> tuple[dict[str, Any], VertexResult]:
		"""Pede uma resposta em JSON no formato `schema` e devolve (objeto, metadados da chamada)."""
		body = {
			"systemInstruction": {"parts": [{"text": system}]},
			"contents": [{"role": "user", "parts": [{"text": user_text}]}],
			"generationConfig": {
				"responseMimeType": "application/json",
				"responseSchema": schema,
				"maxOutputTokens": max_output_tokens,
			},
		}
		started = time.monotonic()
		token = self.token_provider(self.credentials_path)
		url = endpoint(self.project, self.location, model)
		response = None
		for attempt in range(2):
			try:
				response = self.session.post(
					url,
					headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
					data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
					timeout=self.timeout,
				)
			except requests.RequestException as e:
				raise VertexError(f"Falha de rede ao chamar o Gemini: {e}") from e
			# 429 (limite) e 503 (sobrecarga): espera um pouco e tenta de novo uma vez
			if response.status_code in (429, 503) and attempt == 0:
				time.sleep(5)
				continue
			break
		if response.status_code == 404:
			raise VertexError(
				f"O modelo {model} não está disponível na região {self.location} do Vertex AI (404). "
				"Confira a região configurada."
			)
		if response.status_code in (401, 403):
			raise VertexError("O Google recusou a credencial do Vertex AI (sem permissão no projeto).")
		if not response.ok:
			raise VertexError(f"Erro {response.status_code} do Gemini: {response.text[:400]}")
		try:
			payload = response.json()
		except ValueError as e:
			raise VertexError(f"Resposta não-JSON do Vertex AI: {response.text[:200]}") from e

		text, finish_reason = _text_of(payload)
		usage = payload.get("usageMetadata") or {}
		result = VertexResult(
			text=text,
			finish_reason=finish_reason,
			prompt_tokens=int(usage.get("promptTokenCount") or 0),
			output_tokens=int(usage.get("candidatesTokenCount") or 0),
			thinking_tokens=int(usage.get("thoughtsTokenCount") or 0),
			model=model,
			seconds=round(time.monotonic() - started, 1),
		)
		if finish_reason == "MAX_TOKENS":
			raise VertexInvalidOutput("O parecer ficou maior que o limite de resposta e foi cortado.", result)
		try:
			return json.loads(text), result
		except ValueError as e:
			raise VertexInvalidOutput(f"O Gemini não devolveu um JSON válido (término: {finish_reason}).", result) from e
