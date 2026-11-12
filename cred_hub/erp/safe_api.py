"""HTTP session for APIs whose address the client company types (generic REST connector, ADR 0007 §6). No Frappe.

The call leaves our server, which also hosts the database, Redis and the internal network, so every request:

- goes only to https, on port 443 (or 8443, common for ERP APIs), to a host NAME (a typed IP is refused);
- resolves the name and requires EVERY address to be public (no private, loopback, link-local, cloud metadata or
  internal ranges), and checks the address actually connected again on the socket (DNS rebinding) - the checks and
  the socket guard are the ones of `cred_hub.email_company.safe_http`, reused here;
- does not follow redirects unless the configuration allows N hops, each one checked the same way;
- has a connect/read timeout, a total deadline and a maximum response size;
- sends only allowed header names (never Host, proxy, forwarding or hop-by-hop headers) and no CR/LF in values;
- never uses the environment's proxy or .netrc.

The object mimics the small part of `requests.Session.request` the ERP clients use, so the clients stay testable
with the same fake sessions as the native connectors.
"""

import json
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from cred_hub.email_company import safe_http

BlockedAddress = safe_http.BlockedAddress

USER_AGENT = "HubTech-ERP-Connector/1.0 (+contato@credtechdigital.com.br)"
HTTPS_PORTS = (None, 443, 8443)
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 30
DEADLINE = 90  # whole request, body included
MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3  # upper bound for what a configuration may allow; default is 0

# Header names a configuration may never set: routing, proxy, hop-by-hop and framing headers
FORBIDDEN_HEADERS = frozenset({
	"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length",
	"expect", "forwarded", "via", "x-real-ip", "x-original-url", "x-rewrite-url", "x-http-method-override",
	"x-http-method", "x-method-override", "cookie", "accept-encoding",
})
FORBIDDEN_PREFIXES = ("proxy-", "x-forwarded-", "sec-", "x-envoy-", "x-amzn-", "x-goog-", "metadata")
_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


def check_header(name: str, value) -> str:
	"""The header name if allowed; raises BlockedAddress otherwise."""
	lower = (name or "").strip().lower()
	if not _TOKEN.match(lower) or lower in FORBIDDEN_HEADERS or lower.startswith(FORBIDDEN_PREFIXES):
		raise BlockedAddress(f"header {name!r} is not allowed")
	if any(c in str(value) for c in "\r\n\0"):
		raise BlockedAddress(f"header {name!r} has a line break")
	return name.strip()


def check_url(url: str) -> str:
	"""https only, allowed port, public-looking host name (no DNS yet). Returns the host."""
	parts = urlsplit(url or "")
	if (parts.scheme or "").lower() != "https":
		raise BlockedAddress("only https is allowed")
	try:
		port = parts.port
	except ValueError:
		raise BlockedAddress("bad port") from None
	if port not in HTTPS_PORTS:
		raise BlockedAddress(f"port {port} is not allowed")
	if parts.username or parts.password:
		raise BlockedAddress("credentials in url")
	host = parts.hostname or ""
	# Same host rules as the e-mail finder (no IP literal, no .local/.internal), on the bare host
	return safe_http.check_url(f"https://[{host}]/" if ":" in host else f"https://{host}/")


def check_destination(url: str):
	"""check_url + every resolved address public."""
	host = check_url(url)
	safe_http.resolve_public(host, urlsplit(url).port or 443)
	return host


class SafeResponse:
	def __init__(self, url, status_code, headers, body: bytes, redirects):
		self.url = url
		self.status_code = status_code
		self.headers = headers
		self.content = body
		self.redirects = redirects

	@property
	def text(self) -> str:
		charset = requests.utils.get_encoding_from_headers(self.headers) or "utf-8"
		return self.content.decode(charset, errors="replace")

	def json(self):
		return json.loads(self.text)


class TooLarge(Exception):
	pass


class SafeApiSession:
	"""`request(method, url, headers=, params=, json=, data=, timeout=)` with the rules of the module docstring.

	`transport` is the underlying requests.Session (default: safe_http's session, whose adapter re-checks the
	connected address on the socket and refuses proxies). Tests inject a fake transport; `resolve` is injectable
	for the DNS tests.
	"""

	def __init__(self, max_bytes: int = MAX_BYTES, max_redirects: int = 0, deadline: float = DEADLINE,
			transport=None, resolve=None):
		self.max_bytes = int(max_bytes or MAX_BYTES)
		self.max_redirects = max(0, min(int(max_redirects or 0), MAX_REDIRECTS))
		self.deadline = float(deadline or DEADLINE)
		self.transport = transport
		self.resolve = resolve or safe_http.resolve_public

	def _transport(self):
		if self.transport is None:
			self.transport = safe_http._session()
			self.transport.headers["User-Agent"] = USER_AGENT
		return self.transport

	def _check(self, url):
		host = check_url(url)
		self.resolve(host, urlsplit(url).port or 443)

	def request(self, method, url, headers=None, params=None, timeout=None, **kwargs):
		headers = {check_header(k, v): str(v) for k, v in (headers or {}).items()}
		ends_at = time.monotonic() + self.deadline

		def remaining() -> float:
			left = ends_at - time.monotonic()
			if left <= 0:
				raise requests.Timeout(f"over {self.deadline}s")
			return left

		redirects = []
		for _hop in range(self.max_redirects + 1):
			self._check(url)
			left = remaining()
			connect, read = timeout if isinstance(timeout, tuple) else (CONNECT_TIMEOUT, timeout or READ_TIMEOUT)
			response = self._transport().request(
				method, url, headers=headers, params=params, timeout=(min(connect, left), min(read, left)),
				allow_redirects=False, stream=True, **kwargs,
			)
			try:
				if response.status_code in safe_http.REDIRECT_CODES and response.headers.get("Location"):
					if len(redirects) >= self.max_redirects:
						raise BlockedAddress("redirects are not followed")
					redirects.append(url)
					url = urljoin(url, response.headers["Location"])
					params = None  # the Location carries its own query
					if response.status_code == 303:
						method, kwargs = "GET", {}
					continue
				body = b""
				while True:
					remaining()
					chunk = response.raw.read1(64 * 1024)
					if not chunk:
						break
					body += chunk
					if len(body) > self.max_bytes:
						raise TooLarge(f"response over {self.max_bytes} bytes")
				return SafeResponse(url, response.status_code, response.headers, body, redirects)
			finally:
				response.close()
		raise BlockedAddress("too many redirects")
