"""Shared plumbing for the ERP clients (credtech#367): errors, token cache, throttling.

Pure Python (requests only). Clients take an injectable `session`, `clock` and `sleep` so tests never touch the
network or wait for real.
"""

import time

import requests

TIMEOUT = 60


class ErpError(Exception):
	"""Any failure talking to an ERP. `code` is a short machine label; the message is safe to show to an admin."""

	def __init__(self, message: str, code: str = "error", http_status: int | None = None):
		super().__init__(message)
		self.code = code
		self.http_status = http_status


class ErpAuthError(ErpError):
	def __init__(self, message: str, http_status: int | None = None):
		super().__init__(message, "auth", http_status)


class ErpMaintenance(ErpError):
	"""The ERP said it is under maintenance: stop now, try again on the next run."""

	def __init__(self, message: str = "The ERP is under maintenance.", http_status: int | None = 500):
		super().__init__(message, "maintenance", http_status)


class ErpRateLimited(ErpError):
	"""The ERP asked us to wait `wait_seconds` before calling again."""

	def __init__(self, message: str, wait_seconds: int, http_status: int | None = 429):
		super().__init__(message, "rate_limited", http_status)
		self.wait_seconds = wait_seconds


class Token:
	def __init__(self, value: str = "", expires_at: float = 0.0):
		self.value = value
		self.expires_at = expires_at

	def valid(self, now: float) -> bool:
		return bool(self.value) and now < self.expires_at


class BaseClient:
	"""HTTP wrapper with a bearer token that renews itself and an optional minimum gap between calls."""

	# Renew a bit before the ERP's own expiry so a request never leaves with a token about to die
	TOKEN_MARGIN = 60
	min_interval = 0.0

	def __init__(self, base_url: str, session=None, clock=None, sleep=None, token: Token | None = None, on_token=None):
		self.base_url = (base_url or "").rstrip("/")
		self.session = session or requests.Session()
		self.clock = clock or time.time  # wall clock: tokens are also cached across workers
		self.sleep = sleep or time.sleep
		self.token = token or Token()
		# Called with each new token, so the caller can share it between workers (Redis)
		self.on_token = on_token
		self._last_call = None
		self.calls = 0

	# Subclasses implement the ERP-specific login and error reading
	def _login(self) -> Token:  # pragma: no cover - abstract
		raise NotImplementedError

	def _headers(self) -> dict:
		return {"Authorization": f"Bearer {self.token.value}", "Accept": "application/json"}

	def _check(self, response):
		"""Raise for ERP-specific error envelopes. Default: nothing extra."""

	def authenticate(self) -> Token:
		self.token = self._login()
		if self.on_token:
			self.on_token(self.token)
		return self.token

	def _ensure_token(self):
		if not self.token.valid(self.clock()):
			self.authenticate()

	def _throttle(self):
		if self.min_interval and self._last_call is not None:
			wait = self.min_interval - (self.clock() - self._last_call)
			if wait > 0:
				self.sleep(wait)
		self._last_call = self.clock()

	def _send(self, method: str, path: str, **kwargs):
		self._throttle()
		self.calls += 1
		try:
			return self.session.request(
				method, f"{self.base_url}{path}", headers=self._headers(), timeout=TIMEOUT, **kwargs
			)
		except requests.RequestException as e:
			raise ErpError("Could not reach the ERP. Try again in a few minutes.", "network") from e

	def request(self, method: str, path: str, **kwargs):
		"""Authenticated call. A 401 renews the token once and repeats the call."""
		self._ensure_token()
		response = self._send(method, path, **kwargs)
		if response.status_code == 401:
			self.authenticate()
			response = self._send(method, path, **kwargs)
		if response.status_code == 401:
			raise ErpAuthError("The ERP refused the credentials. Check the user and password.", 401)
		self._check(response)
		return response


def json_or_none(response):
	try:
		return response.json()
	except ValueError:
		return None


def error_text(response, limit: int = 300) -> str:
	body = json_or_none(response)
	if isinstance(body, dict):
		text = body.get("detail") or body.get("message") or body.get("error_description") or body.get("error")
		if text and not isinstance(text, bool):
			return str(text)[:limit]
	if isinstance(body, list) and body:
		return "; ".join(str(item) for item in body)[:limit]
	return (getattr(response, "text", "") or "").strip()[:limit]
