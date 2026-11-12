"""QI Tech (QI DTVM, IaaS) HTTP client (ADR 0007 §10, credtech#375). Pure Python (requests + PyJWT), no Frappe.

Built from the public documentation (docs.qitech.com.br/documentation/iaas, read 11/11/2026):

- Hosts: one per profile, and an API key works only on its profile's host (manager-api, consultant-api,
  assignor-api; `.sandbox.` in the name for the sandbox). The admin types the host of the profile CredTech's
  client received.
- Authentication (introducao/teste_de_autenticacao): every request carries `API-CLIENT-KEY: <api key>` and
  `AUTHORIZATION: <JWT>` (raw token, no "Bearer"). The JWT is signed with the client's EC P-521 private key (ES512,
  troca_de_chaves) with header {"alg", "typ": "JWT"} and claims exactly:
    timestamp   UTC "YYYY-MM-DDTHH:MM:SS" (within 10 minutes of QI's clock; new for each request)
    method      uppercase
    uri         the path WITH its query string, exactly as sent
    payload_md5 md5 hex of the exact body bytes (only when there is a body)
  Bodies and answers are plain JSON (no encoded body in IaaS).
- Errors (introducao/erros): {"title", "description", "translation", "code"}; code prefix MIT/CIT/AIT = auth on
  the manager/consultant/assignor host; a missing permission is a 401, not a 403; 404 QIT000404 = path not exposed on
  this host (usually the wrong profile host). Rate limits and maintenance are not documented.
- Lists: `page` (from 0) + `limit`, answer {"data": [...], "page", "limit", "is_last_page"}.
"""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests

from .base import (
	TIMEOUT,
	BaseClient,
	ErpAuthError,
	ErpError,
	ErpMaintenance,
	ErpRateLimited,
	Token,
	json_or_none,
)

ALGORITHMS = ("ES512", "ES384", "ES256", "RS256")
DEFAULT_ALGORITHM = "ES512"
PING = "/authentication_test"
ASSIGNORS = "/assignor_registry/assignor_registries"
ASSIGNOR = "/assignor_registry/assignor_registry"
LIST_LIMIT = 50  # maximum of the assignor registry list
MAX_PAGES = 2000
# The API key and the signed requests only ever go to QI Tech's own hosts
HOST_SUFFIXES = (".qidtvm.com.br", ".qitech.com.br")

# Error codes that are about the access, not the request (suffix after the 3-letter host prefix)
AUTH_SUFFIXES = {"000007", "000008", "000009", "000010", "000011", "000012", "000013", "000014", "000015", "000016",
	"000017", "000018", "000020"}


def timestamp(now: datetime | None = None) -> str:
	return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S")


def error_parts(response) -> tuple[str, str]:
	"""(code, text) of a QI error envelope; the Portuguese `translation` is what the admin reads."""
	body = json_or_none(response)
	if isinstance(body, dict):
		text = body.get("translation") or body.get("description") or body.get("title") or ""
		return str(body.get("code") or ""), str(text)[:300]
	return "", (getattr(response, "text", "") or "").strip()[:300]


class QiTechClient(BaseClient):
	def __init__(self, base_url: str, api_key: str, private_key: str, algorithm: str = DEFAULT_ALGORITHM,
			session=None, clock=None, sleep=None, token=None, on_token=None, now=None):
		super().__init__(base_url, session=session, clock=clock, sleep=sleep, token=token, on_token=on_token)
		self.api_key = (api_key or "").strip()
		self.private_key = (private_key or "").strip()
		self.algorithm = algorithm or DEFAULT_ALGORITHM
		if self.algorithm not in ALGORITHMS:
			raise ErpError(f"Unknown QI Tech signing algorithm {self.algorithm}.", "config")
		self.now = now  # injectable UTC clock for the signature timestamp
		parts = urlsplit(self.base_url)
		if parts.scheme != "https" or not (parts.hostname or "").endswith(HOST_SUFFIXES) or parts.port not in (None, 443):
			raise ErpError("The QI Tech API URL must be https on a QI Tech host (…qidtvm.com.br).", "config")

	# No token: each request is signed. The base class keeps calling these, so they are trivial
	def _login(self) -> Token:
		if not (self.api_key and self.private_key):
			raise ErpAuthError("Fill the QI Tech API key and private key in the settings.")
		# Finite: the shared token cache (settings._store_token) stores the expiry as a TTL
		return Token("signed", self.clock() + 24 * 3600)

	def sign(self, method: str, uri: str, body: bytes | None) -> str:
		import jwt

		claims = {"timestamp": timestamp(self.now() if self.now else None), "method": method.upper(), "uri": uri}
		if body is not None:
			claims["payload_md5"] = hashlib.md5(body).hexdigest()
		try:
			return jwt.encode(claims, self.private_key, algorithm=self.algorithm, headers={"typ": "JWT"})
		except (ValueError, TypeError, jwt.PyJWTError) as e:
			# Never echo the key; only say it does not work
			raise ErpAuthError(f"The QI Tech private key could not sign the request ({type(e).__name__}).") from None

	def _send(self, method: str, path: str, params: dict | None = None, body=None, **_kwargs):
		# The signed uri must be the path with the query string exactly as sent: build the URL here, never params=
		uri = f"{path}?{urlencode(params)}" if params else path
		data = json.dumps(body).encode() if body is not None else None
		headers = {
			"API-CLIENT-KEY": self.api_key,
			"AUTHORIZATION": self.sign(method, uri, data),
			"Accept": "application/json",
		}
		if data is not None:
			headers["Content-Type"] = "application/json"
		self._throttle()
		self.calls += 1
		try:
			return self.session.request(method, f"{self.base_url}{uri}", headers=headers, timeout=TIMEOUT, data=data)
		except requests.RequestException as e:
			raise ErpError("Could not reach QI Tech. Try again in a few minutes.", "network") from e

	def _check(self, response):
		status = response.status_code
		if 200 <= status < 300:
			return
		code, text = error_parts(response)
		label = f"{text} ({code})" if code else text
		if status == 429:
			raise ErpRateLimited(f"QI Tech asked to wait before calling again. {label}".strip(), 60)
		if status >= 500:
			raise ErpMaintenance(f"QI Tech is unavailable ({status}). {label}".strip(), status)
		if status in (401, 403) or (status == 400 and code[3:] == "000009"):
			raise ErpAuthError(f"QI Tech refused the access: {label}", status)
		if status == 404 and code == "QIT000404":
			raise ErpError(
				"This QI Tech host does not offer this operation: check that the API URL is the host of your profile "
				f"(manager, consultant or assignor). {label}", "error", status,
			)
		raise ErpError(f"QI Tech answered {status}: {label or '-'}", "error", status)

	def get(self, path: str, params: dict | None = None):
		return json_or_none(self.request("GET", path, params=params))

	def post(self, path: str, body: dict):
		response = self.request("POST", path, body=body)
		return response.status_code, json_or_none(response)

	# Operations -------------------------------------------------------------------------------------------------
	def ping(self) -> dict:
		body = self.get(PING) or {}
		return {"status": "ok" if body.get("success") else "unexpected", "endpoint": PING}

	def pages(self, path: str, params: dict | None = None, limit: int = LIST_LIMIT):
		seen = set()
		for page in range(MAX_PAGES):
			body = self.get(path, {**(params or {}), "page": page, "limit": limit}) or {}
			rows = body.get("data") or []
			mark = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
			if rows and mark in seen:
				raise ErpError("QI Tech returned the same page twice.", "pagination")
			seen.add(mark)
			yield from rows
			if body.get("is_last_page") is not False or not rows:
				return
		raise ErpError(f"The QI Tech list passed {MAX_PAGES} pages.", "pagination")

	def assignors(self, **filters):
		return self.pages(ASSIGNORS, {k: v for k, v in filters.items() if v})

	def find_assignor(self, document_number: str) -> dict | None:
		return next(iter(self.assignors(document_number=document_number)), None)

	def register_assignor(self, payload: dict) -> dict:
		_status, body = self.post(ASSIGNOR, payload)
		return body or {}
