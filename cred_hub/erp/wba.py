"""WBA WEB external API client (credtech#367).

Spec: WBA "Documentação de API externa" (Jan/2026) + Postman/cURL samples.
- Login: POST /oauth/token, form-urlencoded password grant, header `Tenant-ID`. Token lives 600 s.
- Every data call sends `Tenant-ID` + Bearer.
- Lists are paginated with `paginacao.page` (0-based) and `pageSize` (max 100).
- Títulos (recebíveis) can only be listed per borderô; there is no status, due-date or settlement filter.
"""

from datetime import date

import requests

from .base import TIMEOUT, BaseClient, ErpAuthError, ErpError, Token, error_text, json_or_none
from .formats import br_date, digits, to_date, to_decimal

DEFAULT_BASE_URL = "https://new-api.wba.com.br"
PAGE_SIZE = 100
CADASTRO_RECEIVED = "Requisição Recebida."


class WbaClient(BaseClient):
	provider = "WBA"

	def __init__(self, tenant_id: str, username: str, password: str, base_url: str = DEFAULT_BASE_URL, **kwargs):
		super().__init__(base_url or DEFAULT_BASE_URL, **kwargs)
		self.tenant_id = (tenant_id or "").strip()
		self.username = username or ""
		self.password = password or ""

	def _headers(self) -> dict:
		return {**super()._headers(), "Tenant-ID": self.tenant_id, "Content-Type": "application/json"}

	def _login(self) -> Token:
		if not (self.tenant_id and self.username and self.password):
			raise ErpAuthError("Fill the WBA tenant, user and password in the settings.")
		self.calls += 1
		try:
			response = self.session.request(
				"POST",
				f"{self.base_url}/oauth/token",
				data={"username": self.username, "password": self.password, "grant_type": "password"},
				headers={"Tenant-ID": self.tenant_id, "Accept": "application/json"},
				timeout=TIMEOUT,
			)
		except requests.RequestException as e:
			raise ErpError("Could not reach WBA. Try again in a few minutes.", "network") from e
		body = json_or_none(response) or {}
		if response.status_code != 200 or not body.get("access_token"):
			raise ErpAuthError(
				f"WBA refused the login: {error_text(response) or response.status_code}", response.status_code
			)
		lifetime = int(body.get("expires_in") or 600)
		return Token(body["access_token"], self.clock() + max(lifetime - self.TOKEN_MARGIN, 30))

	def _check(self, response):
		if response.status_code >= 400:
			raise ErpError(f"WBA error {response.status_code}: {error_text(response)}", "http", response.status_code)
		body = json_or_none(response)
		if isinstance(body, dict) and body.get("error") is True:
			raise ErpError(f"WBA error: {body.get('message') or 'no message'}", "api", body.get("errorHttpStatusCode"))

	def _pages(self, path: str, body: dict, list_key: str, order_by: str | None = None, page_size: int = PAGE_SIZE):
		page = 0
		while True:
			payload = {
				**body,
				"paginacao": {"page": page, "pageSize": page_size, "orderByField": order_by, "orderDescending": False},
			}
			data = self.request("POST", path, json=payload).json() or {}
			yield from data.get(list_key) or []
			page += 1
			if page >= int(data.get("totalPages") or 0):
				return

	# Cadastro (cedente and sacado share the same endpoints)
	def find_people(self, documents=(), kinds=()) -> list[dict]:
		"""Registered people by CPF/CNPJ (and/or tipoCadastro like CEDENTE, SACADO)."""
		docs = [digits(d) for d in documents if digits(d)]
		body = {}
		cnpjs = [d for d in docs if len(d) != 11]
		cpfs = [d for d in docs if len(d) == 11]
		if cnpjs:
			body["listCnpj"] = cnpjs
		if cpfs:
			body["listCpf"] = cpfs
		if kinds:
			body["listTipoCadastro"] = list(kinds)
		return list(self._pages("/v1/external/cadastro/filter", body, "list", order_by="CODIGO"))

	def find_person(self, document) -> dict | None:
		wanted = digits(document)
		for person in self.find_people([wanted]):
			if digits(person.get("cnpj") or person.get("cpf")) == wanted:
				return person
		return None

	def send_people(self, people: list[dict]) -> str:
		"""Asynchronous upload: WBA only answers "Requisição Recebida." (no id). Confirm later with find_person."""
		response = self.request("POST", "/v1/private/cs/cadastro/pessoa", json=people)
		body = json_or_none(response)
		return body if isinstance(body, str) else (getattr(response, "text", "") or "").strip('" ')

	# Borderôs and títulos
	def list_borderos(self, created_from: date | None = None, created_to: date | None = None, cedentes=(),
			states=()):
		body = {}
		if created_from:
			body["dataCriacaoDe"] = br_date(created_from)
		if created_to:
			body["dataCriacaoAte"] = br_date(created_to)
		if cedentes:
			body["listDocumentoCedente"] = [digits(d) for d in cedentes]
		if states:
			body["listEstadoBordero"] = list(states)
		yield from self._pages("/v1/external/bordero/filter", body, "listBordero", order_by="BORDERO_DATA_CRIACAO")

	def list_receivables(self, bordero_id: str, operation_ids=()):
		body = {"idBordero": bordero_id}
		if operation_ids:
			body["listIdOperacao"] = list(operation_ids)
		yield from self._pages(
			"/v1/external/recebivel/filter", body, "listRecebivel", order_by="RECEBIVEL_NUMERO_DOCUMENTO"
		)

	def ping(self) -> dict:
		"""Login + the cheapest read, to prove the user has API access (profile API_CADASTRO)."""
		self.authenticate()
		data = self.request(
			"POST",
			"/v1/external/cadastro/filter",
			json={"paginacao": {"page": 0, "pageSize": 1, "orderByField": "CODIGO", "orderDescending": True}},
		).json() or {}
		return {"people": int(data.get("totalItems") or 0)}


def normalize_receivable(item: dict, bordero: dict | None = None) -> dict:
	"""WBA recebível -> the common título shape used by the collection sync."""
	bordero = bordero or {}
	return {
		"erp": "WBA",
		"erp_id": item.get("idRecebivel"),
		"operation": item.get("idOperacao"),
		"bordero": item.get("idBordero") or bordero.get("idBordero"),
		"number": str(item.get("numeroDocumento") or ""),
		"kind": item.get("tipoRecebivel"),
		"sacado_document": digits(item.get("documentoSacado")),
		"sacado_name": item.get("nomeSacado") or "",
		"cedente_document": digits(bordero.get("documentoCedente")),
		"cedente_name": bordero.get("razaoSocialCedente") or "",
		"issue_date": to_date(item.get("dataEmissao")),
		"due_date": to_date(item.get("dataVencimento")),
		"amount": to_decimal(item.get("valor")),
		"collection_mode": item.get("modoCobranca"),
		# WBA exposes no status, settlement or boleto line on recebíveis
		"situation": "",
		"digitable_line": "",
	}
