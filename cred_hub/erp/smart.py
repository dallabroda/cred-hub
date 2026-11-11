"""Smart Securities (NDS Brasil) API client (credtech#367).

- Login: POST /oauth, JSON client_credentials. Token lives 1 h, no refresh token.
- 20 requests/minute for the whole account; some reports also demand a minimum interval
  (429 "A requisição deve ter um intervalo mínimo de 10 minuto(s)") and calling early restarts that timer.
- "Sistema em manutenção" comes as HTTP 500: stop, do not retry.
- GET lists are HAL, 1-based (`page`, `page_count`, `_embedded.<key>`); every value is a string, money in BR
  format, CPF/CNPJ masked.
- POST: money must be a BR string ("1.587,65"), dates dd/mm/yyyy, CEP a JSON int, empty optional keys omitted.
"""

import re
from urllib.parse import urlencode

import requests

from .base import (
	TIMEOUT,
	BaseClient,
	ErpAuthError,
	ErpError,
	ErpMaintenance,
	ErpRateLimited,
	Token,
	error_text,
	json_or_none,
)
from .formats import br_date, digits, to_date, to_decimal

DEFAULT_BASE_URL = "https://api.smartsecurities.com.br"
API = "/smartsecurities"
OPEN, PAID = "emaberto", "quitados"
OPEN_RANGE_START, OPEN_RANGE_END = "01/01/2000", "31/12/2099"
# A generic 429 (no minimum-interval message) is the 20/min limit: one pause, then one retry
RATE_LIMIT_PAUSE = 61
# "já existe", "já cadastrado", "existe um cedente"... but never "não existe"
_ALREADY_EXISTS = re.compile(r"(?<!n[aã]o )\bexiste\b|j[aá] cadastrad", re.IGNORECASE)
_MIN_INTERVAL = re.compile(r"intervalo m[ií]nimo de (\d+) minuto", re.IGNORECASE)


class SmartClient(BaseClient):
	provider = "Smart Securities"
	# 20 requests per minute -> one call every 3 s, with a small margin
	min_interval = 3.1

	def __init__(self, client_id: str, client_secret: str, base_url: str = DEFAULT_BASE_URL, **kwargs):
		super().__init__(base_url or DEFAULT_BASE_URL, **kwargs)
		self.client_id = (client_id or "").strip()
		self.client_secret = client_secret or ""

	def _login(self) -> Token:
		if not (self.client_id and self.client_secret):
			raise ErpAuthError("Fill the Smart Securities client ID and secret in the settings.")
		self._throttle()
		self.calls += 1
		try:
			response = self.session.request(
				"POST",
				f"{self.base_url}/oauth",
				json={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
				headers={"Content-Type": "application/json", "Accept": "application/json"},
				timeout=TIMEOUT,
			)
		except requests.RequestException as e:
			raise ErpError("Could not reach Smart Securities. Try again in a few minutes.", "network") from e
		self._raise_maintenance(response)
		body = json_or_none(response) or {}
		if response.status_code != 200 or not body.get("access_token"):
			raise ErpAuthError(
				f"Smart Securities refused the login: {error_text(response) or response.status_code}",
				response.status_code,
			)
		lifetime = int(body.get("expires_in") or 3600)
		return Token(body["access_token"], self.clock() + max(lifetime - self.TOKEN_MARGIN, 30))

	def _headers(self) -> dict:
		return {**super()._headers(), "Content-Type": "application/json"}

	@staticmethod
	def _raise_maintenance(response):
		if response.status_code == 500 and "manuten" in error_text(response).lower():
			raise ErpMaintenance("Smart Securities is under maintenance. The next run will try again.")

	def _check(self, response):
		self._raise_maintenance(response)
		if response.status_code == 429:
			text = error_text(response)
			match = _MIN_INTERVAL.search(text)
			wait = int(match.group(1)) * 60 if match else RATE_LIMIT_PAUSE
			raise ErpRateLimited(f"Smart Securities asked to wait: {text}", wait)
		if response.status_code >= 400:
			raise ErpError(
				f"Smart Securities error {response.status_code}: {error_text(response)}", "http", response.status_code
			)

	def request(self, method: str, path: str, **kwargs):
		try:
			return super().request(method, path, **kwargs)
		except ErpRateLimited as e:
			# Only the plain per-minute limit is worth waiting for; a report cooldown restarts if we call early
			if e.wait_seconds > RATE_LIMIT_PAUSE:
				raise
			self.sleep(e.wait_seconds)
			return super().request(method, path, **kwargs)

	def _pages(self, path: str, key: str, params: dict):
		page = 1
		while True:
			# Query built by hand: Smart only honours date filters with raw slashes (dd/mm/yyyy), not %2F
			query = urlencode({**params, "tipoSaida": "json", "page": page}, safe="/")
			data = self.request("GET", f"{path}?{query}").json() or {}
			yield from (data.get("_embedded") or {}).get(key) or []
			page_count = int(data.get("page_count") or 0)
			if page >= page_count:
				return
			page += 1

	# Títulos
	def titles(self, kind: str = OPEN, **filters):
		"""Yields raw rows of /titulo?tipo=<kind>. Date filters accept date objects or dd/mm/yyyy strings."""
		params = {"tipo": kind}
		for key, value in filters.items():
			if value in (None, ""):
				continue
			params[key] = br_date(value) if key.endswith(("Ini", "Fim")) and not key.startswith("valor") else value
		# Date ranges only filter when both ends are sent (proved 10/11/2026: Fim alone returned the whole portfolio)
		for prefix in {key[:-3] for key in params if key.endswith(("Ini", "Fim")) and not key.startswith("valor")}:
			params.setdefault(f"{prefix}Ini", OPEN_RANGE_START)
			params.setdefault(f"{prefix}Fim", OPEN_RANGE_END)
		yield from self._pages(f"{API}/titulo", "titulo", params)

	# Sacados
	def sacados(self, since=None, until=None):
		"""Yields raw rows of the sacado registry (/sacado) registered between the two dates (all when no date)."""
		params = {
			"dataIniSacado": br_date(since) if since else OPEN_RANGE_START,
			"dataFimSacado": br_date(until) if until else OPEN_RANGE_END,
		}
		yield from self._pages(f"{API}/sacado", "sacado", params)

	# Cedente
	def find_cedente(self, document) -> dict | None:
		wanted = digits(document)
		if not wanted:
			# Without cpfCnpjCedente Smart returns every cedente
			return None
		for row in self._pages(f"{API}/cedente", "cedente", {"cpfCnpjCedente": wanted}):
			if digits(row.get("CPF_CNPJ")) == wanted:
				return row
		return None

	def next_contract_number(self) -> int:
		data = self.request("GET", f"{API}/proximo-numero-contrato-mae-cedente").json() or {}
		number = data.get("proximoNumeroContratoMaeCedente")
		if number in (None, ""):
			embedded = (data.get("_embedded") or {}).get("proximo_numero_contrato_mae_cedente") or [{}]
			number = embedded[0].get("proximoNumeroContratoMaeCedente")
		if number in (None, ""):
			raise ErpError("Smart Securities did not return the next master contract number.", "api")
		return int(digits(number))

	def create_cedente(self, payload: dict) -> dict:
		"""POST /cedente. Smart has no update: an existing CPF/CNPJ answers 409 (or 500 mentioning it) -> duplicate."""
		body = {k: v for k, v in payload.items() if v not in (None, "", [], {})}
		try:
			response = self.request("POST", f"{API}/cedente", json=body)
		except ErpError as e:
			text = str(e).lower()
			if e.http_status == 409 or (e.http_status == 500 and _ALREADY_EXISTS.search(text)):
				return {"created": False, "duplicate": True, "id": None, "message": str(e)}
			raise
		data = json_or_none(response) or {}
		return {"created": True, "duplicate": False, "id": data.get("cedente_id"), "message": ""}

	def lookup(self, resource: str) -> list[dict]:
		"""Auxiliary lists used by the cedente form: setor, gerente, operador, captador, estado-civil, ..."""
		return list(self._pages(f"{API}/{resource}", resource.replace("-", "_"), {}))

	def ping(self) -> dict:
		self.authenticate()
		return {"sectors": len(self.lookup("setor"))}


def outcome_of(situation: str) -> str:
	"""SITUACAO -> open / paid / repurchased (repasse counts as repurchase, owner decision 10/11/2026)."""
	text = (situation or "").strip().lower()
	if text.startswith("quitado"):
		return "paid"
	if text.startswith(("recomprado", "repassado")):
		return "repurchased"
	return "open"


def normalize_title(row: dict) -> dict:
	"""Smart título row (emaberto or quitados) -> the common título shape used by the collection sync."""
	situation = row.get("SITUACAO") or ""
	return {
		"erp": "Smart Securities",
		"erp_id": str(row.get("ID_TITULO") or "").strip(),
		"operation": str(row.get("OP") or "").strip(),
		"number": str(row.get("DOCUMENTO") or row.get("NUMERO") or "").strip(),
		"our_number": str(row.get("NOSSO_NUMERO") or "").strip(),
		"kind": row.get("TIPO") or "",
		"sacado_document": digits(row.get("CPF_CNPJ_SACADO")),
		"sacado_name": (row.get("SACADO") or "").strip(),
		"cedente_document": digits(row.get("CPF_CNPJ_CEDENTE")),
		"cedente_name": (row.get("CEDENTE") or "").strip(),
		"issue_date": to_date(row.get("DATA_EMISSAO") or row.get("EMISSAO")),
		"original_due_date": to_date(row.get("ORIGINAL")),
		"due_date": to_date(row.get("VENCIMENTO")),
		"amount": to_decimal(row.get("VALOR") or row.get("VALOR_FACE_")),
		"situation": situation,
		"outcome": outcome_of(situation),
		"digitable_line": digits(row.get("LINHA_DIGITAVEL")),
		"paid_date": to_date(row.get("QUITACAO")),
		"paid_amount": to_decimal(row.get("VALOR_LIQUIDADO")),
		# Every column, for the título de-para chosen by the company (cred_hub.erp.import_map)
		"raw": row,
	}
