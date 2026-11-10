"""WBA WEB external API client (credtech#367).

Spec: WBA "Documentação de API externa" (Jan/2026) + Postman/cURL samples.
- Login: POST /oauth/token, form-urlencoded password grant, header `Tenant-ID`. Token lives 600 s.
- Every data call sends `Tenant-ID` + Bearer.
- Lists are paginated with `paginacao.page` (0-based) and `pageSize` (max 100).
- Títulos (recebíveis) can only be listed per borderô; there is no status, due-date or settlement filter.
- Settlements live in the accounting ledger ("Consulta de lançamentos contábeis", conta corrente): each entry has
  the borderô number, the document number, the sacado and the ledger account. Which account means "paid by the
  debtor" or "repurchased" is set up by each client in WBA, so the client picks them; nothing is guessed
  (`cred_hub.erp.wba_ledger`). The ledger answers a plain list with no page count.
"""

from datetime import date

import requests

from .base import TIMEOUT, BaseClient, ErpAuthError, ErpError, Token, error_text, json_or_none
from .formats import br_date, digits, to_date, to_decimal

DEFAULT_BASE_URL = "https://new-api.wba.com.br"
PAGE_SIZE = 100
CADASTRO_RECEIVED = "Requisição Recebida."
LEDGER_PATH = "/v1/external/financeiro/contacorrente/filter/lista"
INCOME, OUTGOING = "ENTRADA", "SAIDA"
REVERTED = "REVERT"
# The ledger has no page count: stop on a short page, and never loop forever if WBA ignores the page number
LEDGER_MAX_PAGES = 2000


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
		if not docs and not kinds:
			raise ValueError("find_people needs at least one document or kind (an empty filter lists everyone)")
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
		if not wanted:
			# An empty filter would list the whole registry
			return None
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

	# Accounting ledger (conta corrente): where settlements and repurchases are recorded
	def list_entries(self, period_from: date, period_to: date, category: str = INCOME, bordero_numbers=(),
			documents=(), page_size: int = PAGE_SIZE):
		"""Ledger entries paid between the two dates (both required by WBA). Yields raw entries."""
		if not (period_from and period_to):
			raise ValueError("list_entries needs both period dates")
		body = {"periodoDe": br_date(period_from), "periodoAte": br_date(period_to), "categoria": category}
		if bordero_numbers:
			body["numerosBordero"] = [int(n) for n in bordero_numbers]
		if documents:
			body["numerosDocumento"] = [str(d) for d in documents]
		first_ids = set()
		for page in range(LEDGER_MAX_PAGES):
			data = self.request("POST", LEDGER_PATH, json={**body, "paginaAtual": page, "itensPagina": page_size}).json()
			rows = data if isinstance(data, list) else (data or {}).get("list") or []
			if rows:
				# The same first entry twice means WBA ignored the page number: stop instead of looping
				marker = rows[0].get("id")
				if marker and marker in first_ids:
					raise ErpError("WBA returned the same ledger page twice; the listing was stopped.", "api")
				first_ids.add(marker)
			yield from rows
			if len(rows) < page_size:
				return
		raise ErpError("The WBA ledger has more pages than expected; narrow the period.", "api")

	def ledger_accounts(self, period_from: date, period_to: date) -> list[dict]:
		"""Ledger accounts used by income entries in the period, for the client to pick which one means "paid by the
		debtor", "partial payment" or "repurchased" (each WBA client names its own accounts)."""
		accounts = {}
		for raw in self.list_entries(period_from, period_to):
			entry = normalize_entry(raw)
			key = entry["account_id"] or entry["account"]
			if not key:
				continue
			found = accounts.setdefault(
				key, {"id": entry["account_id"], "name": entry["account"], "group": entry["group"], "entries": 0}
			)
			found["entries"] += 1
		return sorted(accounts.values(), key=lambda a: (-a["entries"], a["name"] or ""))

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
		# The ledger points to títulos by borderô number + document number
		"bordero_number": _int(item.get("numeroBordero") or bordero.get("numeroBordero")),
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


def _int(value) -> int | None:
	text = digits(value)
	return int(text) if text else None


def _truthy(value) -> bool:
	if isinstance(value, bool):
		return value
	return str(value or "").strip().lower() in ("true", "1", "s", "sim", "y", "yes")


def normalize_entry(item: dict) -> dict:
	"""WBA ledger entry -> the fields the settlement matching needs."""
	return {
		"erp_id": item.get("id") or "",
		"event": item.get("idEvento") or "",
		"date": to_date(item.get("data")),
		"amount": to_decimal(item.get("valor")),
		"category": (item.get("categoria") or "").upper(),
		"bordero_number": _int(item.get("numeroBordero")),
		"document": str(item.get("documento") or "").strip(),
		"bank_number": str(item.get("numeroBancario") or "").strip(),
		"sacado_document": digits(item.get("documentoSacado")),
		"cedente_document": digits(item.get("documentoCedente")),
		"account_id": item.get("idContaEvento") or "",
		"account": item.get("contaEvento") or "",
		"group": item.get("grupoConta") or "",
		"reverted": (item.get("situacao") or "").upper() == REVERTED,
		# Only a real "true" blocks: "false", "N" or "0" as text must not hide a settlement
		"blocked": _truthy(item.get("bloqueado")),
		"note": item.get("observacao") or "",
	}
