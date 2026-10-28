"""HTTP para endereços que vêm do usuário (domínio do e-mail do lead), sem SSRF (credtech#183). Sem Frappe.

- Só http/https, nas portas 80 e 443, para nomes de domínio (IP digitado é recusado).
- O nome é resolvido e TODOS os endereços precisam ser públicos; e o endereço de fato conectado é conferido de
  novo no socket (protege contra DNS rebinding: o nome resolve público na conferência e interno na conexão).
- Redirecionamentos seguidos à mão, no máximo 3, cada destino conferido do mesmo jeito.
- Timeouts curtos e um PRAZO TOTAL por busca (um servidor que manda 1 byte por vez não segura o worker), corpo
  limitado, sem proxy do ambiente, User-Agent identificado.
"""

import ipaddress
import socket
import time
import zlib
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

USER_AGENT = "HubTech-CompanyFinder/1.0 (+contato@credtechdigital.com.br)"
TIMEOUT = (3, 5)  # conexão, leitura
DEADLINE = 15  # segundos para a busca inteira, com redirecionamentos e corpo
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
PORTS = {"http": 80, "https": 443}
ALLOWED_PORTS = (None, 80, 443)
REDIRECT_CODES = {301, 302, 303, 307, 308}


class BlockedAddress(Exception):
	"""Destino recusado: esquema, porta, IP não público ou redirecionamentos demais."""


class TooSlow(TimeoutError):
	"""A busca passou do prazo total."""


# IPv6 que carregam um IPv4 dentro (NAT64 e 6to4): vale o IPv4 de dentro
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip(value: str) -> bool:
	try:
		ip = ipaddress.ip_address(value.split("%", 1)[0])
	except ValueError:
		return False
	if isinstance(ip, ipaddress.IPv6Address):
		if ip.ipv4_mapped:
			ip = ip.ipv4_mapped
		elif ip.sixtofour:
			ip = ip.sixtofour
		elif ip.teredo:
			ip = ip.teredo[1]
		elif ip in _NAT64:
			ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
	return ip.is_global and not ip.is_multicast


def _is_ip_literal(host: str) -> bool:
	try:
		ipaddress.ip_address(host.strip("[]"))
		return True
	except ValueError:
		return False


def check_url(url: str) -> str:
	"""Confere esquema, porta e nome; devolve o host. Não resolve o nome."""
	parts = urlsplit(url)
	scheme = (parts.scheme or "").lower()
	if scheme not in PORTS:
		raise BlockedAddress(f"scheme {scheme!r}")
	if parts.username or parts.password:
		raise BlockedAddress("credentials in url")
	try:
		port = parts.port
	except ValueError:
		raise BlockedAddress("bad port") from None
	if port not in ALLOWED_PORTS:
		raise BlockedAddress(f"port {port}")
	host = (parts.hostname or "").rstrip(".").lower()
	if not host or "." not in host or _is_ip_literal(host) or host.endswith((".local", ".localhost", ".internal")):
		raise BlockedAddress(f"host {host!r}")
	return host


def resolve_public(host: str, port: int) -> None:
	"""Todos os endereços do nome precisam ser públicos (falha rápido, antes de conectar)."""
	try:
		infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except OSError as e:
		raise BlockedAddress(f"dns {host}: {e}") from None
	addresses = {info[4][0] for info in infos}
	if not addresses or not all(is_public_ip(a) for a in addresses):
		raise BlockedAddress(f"non-public address for {host}")


# Conferência no socket conectado: vale para a conexão de verdade, mesmo que o DNS mude entre a resolução e a conexão
def _checked(sock):
	peer = sock.getpeername()[0]
	if not _peer_allowed(peer):
		sock.close()
		raise BlockedAddress(f"connected to non-public address {peer}")
	return sock


def _peer_allowed(peer: str) -> bool:
	return is_public_ip(peer)


class _SafeHTTPConnection(HTTPConnection):
	def _new_conn(self):
		return _checked(super()._new_conn())


class _SafeHTTPSConnection(HTTPSConnection):
	def _new_conn(self):
		return _checked(super()._new_conn())


class _SafeHTTPPool(HTTPConnectionPool):
	ConnectionCls = _SafeHTTPConnection


class _SafeHTTPSPool(HTTPSConnectionPool):
	ConnectionCls = _SafeHTTPSConnection


class _SafeAdapter(HTTPAdapter):
	def init_poolmanager(self, *args, **kwargs):
		super().init_poolmanager(*args, **kwargs)
		self.poolmanager.pool_classes_by_scheme = {"http": _SafeHTTPPool, "https": _SafeHTTPSPool}

	def proxy_manager_for(self, *args, **kwargs):
		raise BlockedAddress("proxies are not used")


def _session() -> requests.Session:
	session = requests.Session()
	session.trust_env = False  # sem HTTP(S)_PROXY nem .netrc do ambiente
	# sem compressão: o corpo é lido cru (read1) e contado em bytes; o que vier comprimido mesmo assim é aberto em
	# _decoded (credtech#186: a Fase 3 lê o texto da página)
	session.headers.update(
		{"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9", "Accept-Encoding": "identity"}
	)
	adapter = _SafeAdapter(max_retries=0)
	session.mount("http://", adapter)
	session.mount("https://", adapter)
	return session


class Page:
	def __init__(self, url, status, headers, body, redirects):
		self.url = url
		self.status = status
		self.headers = headers
		self.body = body
		self.redirects = redirects

	@property
	def text(self) -> str:
		charset = requests.utils.get_encoding_from_headers(self.headers) or "utf-8"
		return self.body.decode(charset, errors="replace")


def _decoded(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
	"""Abre gzip/deflate (corpo possivelmente cortado no limite); outra compressão fica como veio."""
	encoding = (encoding or "").strip().lower()
	if encoding not in ("gzip", "x-gzip", "deflate"):
		return body
	try:
		opener = zlib.decompressobj(16 + zlib.MAX_WBITS if "gzip" in encoding else zlib.MAX_WBITS)
		return opener.decompress(body, max_bytes)
	except zlib.error:
		return body


def fetch(url: str, method: str = "GET", max_bytes: int = MAX_BYTES, max_redirects: int = MAX_REDIRECTS) -> Page:
	"""Busca `url` seguindo até `max_redirects` redirecionamentos seguros. Corpo cortado em `max_bytes`."""
	session = _session()
	redirects = []
	ends_at = time.monotonic() + DEADLINE

	def remaining() -> float:
		left = ends_at - time.monotonic()
		if left <= 0:
			raise TooSlow(f"over {DEADLINE}s")
		return left

	try:
		for _hop in range(max_redirects + 1):
			host = check_url(url)
			scheme = urlsplit(url).scheme.lower()
			resolve_public(host, urlsplit(url).port or PORTS[scheme])
			left = remaining()
			timeout = (min(TIMEOUT[0], left), min(TIMEOUT[1], left))
			response = session.request(method, url, timeout=timeout, allow_redirects=False, stream=True)
			try:
				if response.status_code in REDIRECT_CODES and response.headers.get("Location"):
					redirects.append(url)
					url = urljoin(url, response.headers["Location"])
					if response.status_code == 303:
						method = "GET"
					continue
				body = b""
				if method != "HEAD":
					# read1 volta depois de UMA leitura do socket: o prazo é conferido entre elas
					while len(body) < max_bytes:
						remaining()
						chunk = response.raw.read1(16 * 1024)
						if not chunk:
							break
						body += chunk
					body = _decoded(body[:max_bytes], response.headers.get("Content-Encoding"), max_bytes)
				return Page(url, response.status_code, response.headers, body, redirects)
			finally:
				response.close()
		raise BlockedAddress("too many redirects")
	finally:
		session.close()
