"""SSRF do HTTP que segue o domínio do e-mail do lead (credtech#184). Sem rede externa: um servidor local faz o papel
do site, e o DNS é simulado. Roda no bench ou com `python -m unittest cred_hub.tests.test_email_company_safe_http`."""

import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from unittest.mock import patch

from cred_hub.email_company import safe_http
from cred_hub.email_company.safe_http import BlockedAddress

REAL_GETADDRINFO = socket.getaddrinfo


class _Handler(BaseHTTPRequestHandler):
	routes: ClassVar[dict] = {}

	def do_GET(self):
		status, headers, body = self.routes.get(self.path, (404, {}, b"nope"))
		self.send_response(status)
		for name, value in headers.items():
			self.send_header(name, value)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		try:
			self.wfile.write(body)
		except (BrokenPipeError, ConnectionResetError):
			pass  # o cliente parou de ler no limite de tamanho

	def log_message(self, *args):
		pass


class TestSafeHttp(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
		cls.port = cls.server.server_address[1]
		threading.Thread(target=cls.server.serve_forever, daemon=True).start()

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()

	def setUp(self):
		_Handler.routes = {}
		# DNS simulado: nomes .test apontam para o que o teste disser
		self.dns = {}

		def getaddrinfo(host, port, *args, **kwargs):
			if host in self.dns:
				return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.dns[host], port))]
			return REAL_GETADDRINFO(host, port, *args, **kwargs)

		for target in (
			patch("socket.getaddrinfo", side_effect=getaddrinfo),
			patch.object(safe_http, "ALLOWED_PORTS", (None, 80, 443, self.port)),
		):
			target.start()
			self.addCleanup(target.stop)

	def url(self, host, path="/"):
		return f"http://{host}:{self.port}{path}"

	def allow_test_server(self):
		"""Libera só o 127.0.0.1 do servidor de teste, como se fosse um site público."""
		real = safe_http.is_public_ip
		allowed = patch.object(safe_http, "is_public_ip", side_effect=lambda ip: ip == "127.0.0.1" or real(ip))
		allowed.start()
		self.addCleanup(allowed.stop)

	# ---------- Endereços ----------

	def test_public_ip_policy(self):
		for ip in (
			"127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
			"255.255.255.255", "224.0.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
			# IPv4 interno dentro de IPv6: NAT64, 6to4
			"64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::", "2002:a00:1::",
			"not-an-ip",
		):
			with self.subTest(ip=ip):
				self.assertFalse(safe_http.is_public_ip(ip))
		for ip in ("8.8.8.8", "200.160.2.3", "2001:4860:4860::8888", "64:ff9b::808:808"):
			with self.subTest(ip=ip):
				self.assertTrue(safe_http.is_public_ip(ip))

	def test_url_rules(self):
		for url in (
			"ftp://acme.com.br/", "file:///etc/passwd", "gopher://acme.com.br/", "http://acme.com.br:8080/",
			"http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/", "http://2130706433/",
			"http://user:senha@acme.com.br/", "http://localhost/", "http://intranet/", "http://srv.local/",
			"http://x.internal/",
		):
			with self.subTest(url=url), self.assertRaises(BlockedAddress):
				safe_http.check_url(url)
		self.assertEqual(safe_http.check_url("https://www.Acme.com.br/"), "www.acme.com.br")

	def test_name_resolving_to_private_address_is_refused(self):
		for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254"):
			self.dns["interno.test"] = ip
			with self.subTest(ip=ip), self.assertRaises(BlockedAddress):
				safe_http.fetch(self.url("interno.test"))

	def test_dns_rebinding_is_caught_on_the_socket(self):
		# A conferência do nome diz "público", mas a conexão cai no 127.0.0.1: o socket conectado é recusado
		_Handler.routes["/"] = (200, {}, b"segredo interno")
		self.dns["rebind.test"] = "127.0.0.1"
		with patch.object(safe_http, "resolve_public"), self.assertRaises(BlockedAddress):
			safe_http.fetch(self.url("rebind.test"))

	# ---------- Redirecionamentos ----------

	def test_redirect_to_internal_address_is_refused(self):
		self.allow_test_server()
		self.dns["site.test"] = "127.0.0.1"
		self.dns["interno.test"] = "10.0.0.5"
		for location in (
			"http://169.254.169.254/latest/meta-data/",
			"http://interno.test/",
			f"http://site.test:{self.port + 1}/",
			"file:///etc/passwd",
		):
			_Handler.routes["/"] = (302, {"Location": location}, b"")
			with self.subTest(location=location), self.assertRaises(BlockedAddress):
				safe_http.fetch(self.url("site.test"))

	def test_follows_at_most_three_redirects(self):
		self.allow_test_server()
		self.dns["site.test"] = "127.0.0.1"
		for i in range(5):
			_Handler.routes[f"/{i}"] = (301, {"Location": self.url("site.test", f"/{i + 1}")}, b"")
		_Handler.routes["/5"] = (200, {}, b"fim")
		_Handler.routes["/2"] = (200, {}, b"ok")
		page = safe_http.fetch(self.url("site.test", "/0"))
		self.assertEqual((page.status, page.text, len(page.redirects)), (200, "ok", 2))
		_Handler.routes["/2"] = (301, {"Location": self.url("site.test", "/3")}, b"")
		with self.assertRaises(BlockedAddress):
			safe_http.fetch(self.url("site.test", "/0"))

	def test_body_is_capped(self):
		self.allow_test_server()
		self.dns["site.test"] = "127.0.0.1"
		_Handler.routes["/"] = (200, {}, b"x" * (2 * 1024 * 1024))
		page = safe_http.fetch(self.url("site.test"), max_bytes=1000)
		self.assertEqual(len(page.body), 1000)

	def test_total_deadline_stops_a_slow_server(self):
		# Um servidor que manda 1 byte por vez nunca estoura o timeout de leitura, mas a busca tem prazo total
		self.allow_test_server()
		self.dns["lento.test"] = "127.0.0.1"

		def do_GET(handler):
			handler.send_response(200)
			handler.send_header("Content-Length", "100000")
			handler.end_headers()
			try:
				for _ in range(100):
					handler.wfile.write(b"x")
					handler.wfile.flush()
					time.sleep(0.3)
			except (BrokenPipeError, ConnectionResetError):
				pass

		started = time.monotonic()
		with patch.object(_Handler, "do_GET", do_GET), patch.object(safe_http, "DEADLINE", 2):
			with self.assertRaises(safe_http.TooSlow):
				safe_http.fetch(self.url("lento.test"))
		self.assertLess(time.monotonic() - started, 4)

	def test_identified_user_agent_and_no_env_proxy(self):
		self.allow_test_server()
		self.dns["site.test"] = "127.0.0.1"
		seen = {}

		def do_GET(handler):
			seen["ua"] = handler.headers.get("User-Agent")
			handler.send_response(200)
			handler.send_header("Content-Length", "0")
			handler.end_headers()

		with (
			patch.object(_Handler, "do_GET", do_GET),
			patch.dict("os.environ", {"HTTP_PROXY": "http://10.0.0.9:3128", "http_proxy": "http://10.0.0.9:3128"}),
		):
			safe_http.fetch(self.url("site.test"))
		self.assertEqual(seen["ua"], safe_http.USER_AGENT)


if __name__ == "__main__":
	unittest.main()
