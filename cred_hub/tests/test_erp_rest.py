"""Generic configurable REST connector (ADR 0007 §6/§8/§9, credtech#375) against canned answers.
Pure: `python -m pytest cred_hub/tests/test_erp_rest.py` (no Frappe, nothing leaves the machine)."""

import copy
import json
import socket
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from cred_hub.email_company import safe_http
from cred_hub.erp import connectors, rest, safe_api
from cred_hub.erp.base import ErpAuthError, ErpError, ErpMaintenance, ErpRateLimited
from cred_hub.erp.connectors.base import TITLE_KEYS
from cred_hub.erp.connectors.rest import GENERIC
from cred_hub.tests.test_erp_clients import Clock, FakeResponse, FakeSession

SECRETS = {"client_id": "acme-client", "client_secret": "s3cr3t-value-xyz", "tenant": "tenant-42"}

SAMPLE = {
	"system": "Acme ERP",
	"base_url": "https://api.acme-erp.com.br/v2",
	"auth": {
		"type": "oauth2", "token_url": "https://auth.acme-erp.com.br/oauth/token", "client_id": "{secret:client_id}",
		"client_secret": "{secret:client_secret}", "scope": "titulos",
	},
	"headers": {"X-Tenant": "{secret:tenant}"},
	"pace": {"min_interval": 0, "report_cooldown": 300},
	"endpoints": {
		"ping": {"path": "/health"},
		"overdue_titles": {
			"path": "/titulos",
			"query": {"situacao": "aberto", "vencimentoAte": "{until}"},
			"date_format": "br",
			"items": "data.items",
			"pagination": {"type": "page", "page_param": "page", "size_param": "size", "size": 2, "start": 1,
				"last_path": "data.last"},
			"fields": {
				"erp_id": "id", "number": "numero", "due_date": "vencimento", "amount": "valor",
				"sacado_document": "sacado.documento", "sacado_name": "sacado.nome",
				"cedente_document": "cedente.documento", "cedente_name": "cedente.nome", "situation": "status",
			},
			"outcomes": {"paid": ["LIQUIDADO"], "repurchased": ["RECOMPRADO"]},
		},
		"settled_titles": {
			"path": "/titulos/liquidados",
			"query": {"de": "{since}", "ate": "{until}"},
			"items": "",
			"pagination": {"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 2},
			"fields": {"erp_id": "id", "due_date": "vencimento", "paid_date": "pagamento", "paid_amount": "valorPago",
				"situation": "status"},
			"outcomes": {"repurchased": ["recomprado"]},
		},
		"sacados": {"path": "/sacados", "items": "itens", "pagination": {"type": "cursor", "cursor_param": "cursor",
			"cursor_path": "proximo"}, "document": "doc", "name": "razao"},
		"cedentes": {"path": "/cedentes", "items": "itens", "pagination": {"type": "next_link", "next_path": "links.next"},
			"document": "doc", "name": "razao"},
		"register_cedente": {
			"path": "/cedentes",
			"body": {"cedente": "{{payload}}", "origem": "hubtech"},
			"id_path": "data.id",
			"already": {"status": [409], "contains": ["já cadastrado"]},
			"lookup": {"path": "/cedentes", "query": {"documento": "{document}"}, "items": "itens", "id_path": "id",
				"document": "doc"},
			"fields": [
				{"erp_field": "documento", "label": "CPF/CNPJ", "transform": "digits", "required": True,
					"source": "organization.cnpj"},
				{"erp_field": "razaoSocial", "label": "Legal name", "transform": "trim", "required": True,
					"source": "organization.legal_name|organization.organization_name"},
				{"erp_field": "observacao", "label": "Note"},
			],
		},
	},
}

TOKEN = FakeResponse(200, {"access_token": "tok-1", "expires_in": 3600})
ITEM = {
	"id": 501, "numero": "NF-9", "vencimento": "2026-10-01", "valor": "1.587,65", "status": "ABERTO",
	"sacado": {"documento": "12.345.678/0001-95", "nome": "Sacado SA"},
	"cedente": {"documento": "23.268.852/0001-60", "nome": "Cedente LTDA"},
}


def config(**changes) -> rest.RestConfig:
	data = copy.deepcopy(SAMPLE)
	for key, value in changes.items():
		if value is None:
			data["endpoints"].pop(key, None)
		elif key in data["endpoints"]:
			data["endpoints"][key] = value
		else:
			data[key] = value
	return rest.RestConfig.parse(data)


def generic(routes, cfg=None, clock=None, secrets=None):
	clock = clock or Clock()
	cls = connectors.get(GENERIC).with_config(cfg or config())
	session = FakeSession({("POST", "/oauth/token"): [TOKEN], **routes})
	client = cls.make_client(
		{"rest_config": (cfg or config()).data, "rest_secrets": SECRETS if secrets is None else secrets},
		session=session, clock=clock, sleep=clock.sleep,
	)
	return cls(client), session


def page(items, last=False):
	return FakeResponse(200, {"data": {"items": items, "last": last}})


class TestConfig(unittest.TestCase):
	def test_sample_is_valid_and_capabilities_follow_the_endpoints(self):
		cls = connectors.get(GENERIC).with_config(config())
		caps = cls.capabilities
		self.assertTrue(caps.overdue_titles and caps.settled_titles and caps.register_cedente)
		self.assertTrue(caps.sacado_registry and caps.cedente_registry)
		self.assertFalse(caps.lists)
		self.assertEqual(cls.pace.report_cooldown, 300)
		only_ping = connectors.get(GENERIC).with_config(config(overdue_titles=None, settled_titles=None, sacados=None,
			cedentes=None, register_cedente=None))
		self.assertFalse(any(vars(only_ping.capabilities).values()))

	def test_unconfigured_connector_offers_nothing(self):
		self.assertFalse(any(vars(connectors.get(GENERIC).capabilities).values()))
		self.assertEqual(connectors.get(GENERIC).catalog(), [])

	def test_overdue_without_settled_is_refused(self):
		with self.assertRaisesRegex(rest.ConfigError, "both"):
			config(settled_titles=None)

	def test_https_only_and_no_ip_or_internal_names(self):
		for url in ("http://api.acme.com.br", "https://10.0.0.5/api", "https://localhost/api", "https://[::1]/api",
				"https://erp.internal/api", "https://api.acme.com.br:8080", "https://user:pw@api.acme.com.br"):
			with self.subTest(url), self.assertRaises(rest.ConfigError):
				config(base_url=url)
		self.assertEqual(config(base_url="https://api.acme.com.br:8443/v1/").base_url, "https://api.acme.com.br:8443/v1")

	def test_secrets_are_references_never_values(self):
		with self.assertRaisesRegex(rest.ConfigError, "secret"):
			config(auth={"type": "bearer", "token": "abc123"})
		spec = copy.deepcopy(SAMPLE["endpoints"]["sacados"])
		spec["path"] = "/sacados/{secret:client_secret}"
		with self.assertRaisesRegex(rest.ConfigError, "secrets go only"):
			config(sacados=spec)
		register = copy.deepcopy(SAMPLE["endpoints"]["register_cedente"])
		register["body"] = {"key": "{secret:client_secret}"}
		with self.assertRaisesRegex(rest.ConfigError, "secrets go only"):
			config(register_cedente=register)

	def test_credential_looking_literals_are_refused(self):
		for name in ("X-Api-Key", "X-Auth-Token", "X-Signature", "X-Client-Secret", "X-Password"):
			with self.subTest(name), self.assertRaisesRegex(rest.ConfigError, "credential"):
				config(headers={name: "abc123"})
		spec = copy.deepcopy(SAMPLE["endpoints"]["sacados"])
		spec["query"] = {"api_key": "abc123"}
		with self.assertRaisesRegex(rest.ConfigError, "credential"):
			config(sacados=spec)
		# A header that is not a credential keeps its literal value
		self.assertEqual(config(headers={"X-Tenant": "acme"}).data["headers"], {"X-Tenant": "acme"})

	def test_secret_in_the_query_is_sent_and_masked(self):
		spec = copy.deepcopy(SAMPLE["endpoints"]["sacados"])
		spec["query"] = {"api_key": "{secret:api_key}"}
		cfg = config(sacados=spec)
		self.assertIn("api_key", cfg.secret_names())
		conn, session = generic({("GET", "/v2/sacados"): [FakeResponse(200, {"itens": [], "proximo": None})]}, cfg,
			secrets={**SECRETS, "api_key": "qk-998877"})
		list(conn.sacados())
		self.assertEqual(session.sent[-1]["params"]["api_key"], "qk-998877")
		self.assertNotIn("qk-998877", conn.client.redact("failed with qk-998877"))

	def test_placeholders_are_checked_per_operation(self):
		spec = copy.deepcopy(SAMPLE["endpoints"]["overdue_titles"])
		spec["query"]["desde"] = "{since}"
		with self.assertRaisesRegex(rest.ConfigError, "since"):
			config(overdue_titles=spec)

	def test_forbidden_headers(self):
		for name in ("Host", "X-Forwarded-For", "Proxy-Authorization", "Transfer-Encoding", "Authorization"):
			with self.subTest(name), self.assertRaises(rest.ConfigError):
				config(headers={name: "x"})
		with self.assertRaises(rest.ConfigError):
			config(headers={"X-Ok": "a\r\nHost: evil"})

	def test_date_formats(self):
		day = date(2026, 3, 9)
		self.assertEqual(rest.format_date(day, "iso"), "2026-03-09")
		self.assertEqual(rest.format_date(day, "br"), "09/03/2026")
		self.assertEqual(rest.format_date(day, "compact"), "20260309")
		self.assertEqual(rest.format_date(day, "%d-%m-%Y"), "09-03-2026")
		self.assertEqual(rest.format_date(day, "iso_datetime"), "2026-03-09T00:00:00")
		self.assertTrue(rest.format_date(day, "epoch").isdigit())
		with self.assertRaises(rest.ConfigError):
			rest.format_date(day, "%s %x")

	def test_json_path_and_flatten(self):
		data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x": None}
		self.assertEqual(rest.json_path(data, "a.b[1].c"), 2)
		self.assertIsNone(rest.json_path(data, "a.b[5].c"))
		self.assertIsNone(rest.json_path(data, "a.z"))
		self.assertEqual(rest.json_path([1, 2], ""), [1, 2])
		self.assertEqual(rest.flatten(data), {"a.b[0].c": 1, "a.b[1].c": 2, "x": None})
		with self.assertRaises(rest.ConfigError):
			rest.path_tokens("a..b")

	def test_template_drops_the_address_and_keeps_secret_names_only(self):
		template = config().template()
		self.assertNotIn("base_url", template)
		text = json.dumps(template)
		for value in SECRETS.values():
			self.assertNotIn(value, text)
		self.assertIn("{secret:client_secret}", text)
		# The sample gets its token from another host than the API: loading needs the admin to confirm that host
		with self.assertRaises(rest.TokenHostNotConfirmed) as caught:
			rest.RestConfig.from_template(json.dumps(template), "https://erp.other-client.com.br/api")
		self.assertEqual(caught.exception.host, "auth.acme-erp.com.br")
		loaded = rest.RestConfig.from_template(json.dumps(template), "https://erp.other-client.com.br/api",
			confirm_token_host="auth.acme-erp.com.br")
		self.assertEqual(loaded.base_url, "https://erp.other-client.com.br/api")
		self.assertEqual(loaded.data["endpoints"], config().data["endpoints"])
		self.assertEqual(loaded.secret_names(), ["client_id", "client_secret", "tenant"])

	def test_template_token_url_on_the_api_host_keeps_only_the_path(self):
		auth = {**SAMPLE["auth"], "token_url": "https://api.acme-erp.com.br/oauth/token"}
		template = config(auth=auth).template()
		self.assertEqual(template["auth"]["token_url"], "/oauth/token")
		loaded = rest.RestConfig.from_template(template, "https://erp.other-client.com.br/api")
		self.assertEqual(loaded.data["auth"]["token_url"], "https://erp.other-client.com.br/oauth/token")

	def test_template_scrubs_credential_looking_literals(self):
		cfg = config()
		cfg.data["headers"]["X-Api-Key"] = "literal-key-123"  # what an old or hand-edited config could hold
		template = cfg.template()
		self.assertEqual(template["headers"]["X-Api-Key"], "{secret:X-Api-Key}")
		self.assertNotIn("literal-key-123", json.dumps(template))

	def test_token_cache_scope_follows_the_credentials(self):
		cls = connectors.get(GENERIC)
		one = cls.cache_scope({"rest_config": SAMPLE, "rest_secrets": SECRETS})
		self.assertTrue(one.startswith("rest:"))
		self.assertEqual(one, cls.cache_scope({"rest_config": SAMPLE, "rest_secrets": dict(SECRETS)}))
		rotated = cls.cache_scope({"rest_config": SAMPLE, "rest_secrets": {**SECRETS, "client_secret": "new-one-456"}})
		self.assertNotEqual(one, rotated)
		other_api = cls.cache_scope({"rest_config": {**SAMPLE, "base_url": "https://api.other.com.br"},
			"rest_secrets": SECRETS})
		self.assertNotEqual(one, other_api)
		self.assertNotIn("s3cr3t", one)

	def test_lookup_needs_the_document_path(self):
		register = copy.deepcopy(SAMPLE["endpoints"]["register_cedente"])
		register["lookup"].pop("document")
		with self.assertRaisesRegex(rest.ConfigError, "lookup.document"):
			config(register_cedente=register)

	def test_missing_secrets_are_named(self):
		cls = connectors.get(GENERIC)
		self.assertEqual(cls.missing_credentials({}), ["REST configuration"])
		self.assertEqual(cls.missing_credentials({"rest_config": SAMPLE, "rest_secrets": {"tenant": "t"}}),
			["client_id", "client_secret"])
		with self.assertRaises(ErpAuthError):
			generic({}, secrets={"tenant": "t"})


class TestTitles(unittest.TestCase):
	def test_common_shape_mapping_and_date_placeholder(self):
		conn, session = generic({("GET", "/v2/titulos"): [page([ITEM], last=True)]})
		titles = list(conn.overdue_titles(date(2026, 11, 1)))
		self.assertEqual(len(titles), 1)
		title = titles[0]
		self.assertEqual(set(title), TITLE_KEYS)
		self.assertEqual(title["erp"], GENERIC)
		self.assertEqual(title["erp_id"], "501")
		self.assertEqual(title["due_date"], date(2026, 10, 1))
		self.assertEqual(title["amount"], Decimal("1587.65"))
		self.assertEqual(title["sacado_document"], "12345678000195")
		self.assertEqual(title["outcome"], "open")
		self.assertEqual(title["raw"]["sacado.nome"], "Sacado SA")
		sent = session.sent[-1]
		self.assertEqual(sent["params"]["vencimentoAte"], "01/11/2026")
		self.assertEqual(sent["params"]["page"], 1)
		self.assertEqual(sent["params"]["size"], 2)
		self.assertEqual(sent["headers"]["Authorization"], "Bearer tok-1")
		self.assertEqual(sent["headers"]["X-Tenant"], "tenant-42")

	def test_outcome_mapping_is_case_insensitive_with_default(self):
		rows = [{"id": 1, "vencimento": "2026-01-01", "status": "Recomprado"}, {"id": 2, "vencimento": "2026-01-01",
			"status": "BAIXADO"}]
		conn, session = generic({("GET", "/v2/titulos/liquidados"): [FakeResponse(200, rows), FakeResponse(200, [])]})
		titles = list(conn.settled_titles(date(2026, 11, 1), date(2026, 11, 11)))
		self.assertEqual([t["outcome"] for t in titles], ["repurchased", "paid"])
		self.assertEqual(session.sent[-1]["params"]["de"], "2026-11-01")

	def test_item_without_id_stops(self):
		conn, _session = generic({("GET", "/v2/titulos"): [page([{"vencimento": "2026-01-01"}], last=True)]})
		with self.assertRaisesRegex(ErpError, "erp_id"):
			list(conn.overdue_titles(date(2026, 11, 1)))


class TestPagination(unittest.TestCase):
	def test_page_ends_on_last_flag(self):
		conn, session = generic({("GET", "/v2/titulos"): [
			page([ITEM, {**ITEM, "id": 502}]), page([{**ITEM, "id": 503}, {**ITEM, "id": 504}], last=True),
		]})
		ids = [t["erp_id"] for t in conn.overdue_titles(date(2026, 11, 1))]
		self.assertEqual(ids, ["501", "502", "503", "504"])
		self.assertEqual([s["params"]["page"] for s in session.sent if s["path"] == "/v2/titulos"], [1, 2])

	def test_page_ends_on_short_or_empty_page(self):
		conn, _session = generic({("GET", "/v2/titulos"): [page([ITEM, {**ITEM, "id": 502}]), page([{**ITEM, "id": 9}])]})
		self.assertEqual(len(list(conn.overdue_titles(date(2026, 11, 1)))), 3)

	def test_api_ignoring_the_page_param_never_loops(self):
		# The same page forever (FakeSession repeats its last answer)
		conn, _session = generic({("GET", "/v2/titulos"): [page([ITEM, {**ITEM, "id": 502}])]})
		with self.assertRaisesRegex(ErpError, "same page"):
			list(conn.overdue_titles(date(2026, 11, 1)))

	def test_max_pages(self):
		cfg = config(limits={"max_pages": 3})
		answers = [page([{**ITEM, "id": n}, {**ITEM, "id": n + 100}]) for n in range(10)]
		conn, _session = generic({("GET", "/v2/titulos"): answers}, cfg)
		with self.assertRaisesRegex(ErpError, "3 pages"):
			list(conn.overdue_titles(date(2026, 11, 1)))

	def test_offset(self):
		rows = [{"id": n, "vencimento": "2026-01-01", "status": "pago"} for n in range(3)]
		conn, session = generic({("GET", "/v2/titulos/liquidados"): [FakeResponse(200, rows[:2]), FakeResponse(200, rows[2:])]})
		self.assertEqual(len(list(conn.settled_titles(date(2026, 11, 1), date(2026, 11, 2)))), 3)
		self.assertEqual([s["params"]["offset"] for s in session.sent[1:]], [0, 2])

	def test_cursor_ends_and_repeated_cursor_stops(self):
		conn, session = generic({("GET", "/v2/sacados"): [
			FakeResponse(200, {"itens": [{"doc": "12.345.678/0001-95", "razao": "A"}], "proximo": "c2"}),
			FakeResponse(200, {"itens": [{"doc": "647.121.478-22", "razao": "B"}], "proximo": None}),
		]})
		rows = list(conn.sacados())
		self.assertEqual([r["CPF/CNPJ"] for r in rows], ["12345678000195", "64712147822"])
		self.assertEqual(rows[0]["Nome"], "A")
		self.assertEqual(session.sent[-1]["params"]["cursor"], "c2")
		conn, _session = generic({("GET", "/v2/sacados"): [
			FakeResponse(200, {"itens": [{"doc": "1"}], "proximo": "c2"}),
			FakeResponse(200, {"itens": [{"doc": "2"}], "proximo": "c2"}),
			FakeResponse(200, {"itens": [{"doc": "3"}], "proximo": "c2"}),
		]})
		with self.assertRaisesRegex(ErpError, "same cursor"):
			list(conn.sacados())

	def test_next_link_same_host_only_and_no_loop(self):
		conn, session = generic({("GET", "/v2/cedentes"): [
			FakeResponse(200, {"itens": [{"doc": "1"}], "links": {"next": "/v2/cedentes?page=2"}}),
			FakeResponse(200, {"itens": [{"doc": "2"}], "links": {"next": None}}),
		]})
		self.assertEqual(len(list(conn.cedentes())), 2)
		self.assertEqual(session.sent[-1]["query"], "page=2")
		conn, _session = generic({("GET", "/v2/cedentes"): [
			FakeResponse(200, {"itens": [{"doc": "1"}], "links": {"next": "https://evil.example.com/steal"}}),
		]})
		with self.assertRaisesRegex(ErpError, "another address"):
			list(conn.cedentes())
		conn, _session = generic({("GET", "/v2/cedentes"): [
			FakeResponse(200, {"itens": [{"doc": "1"}], "links": {"next": "/v2/cedentes?page=2"}}),
			FakeResponse(200, {"itens": [{"doc": "2"}], "links": {"next": "/v2/cedentes?page=2"}}),
		]})
		with self.assertRaisesRegex(ErpError, "same next link"):
			list(conn.cedentes())


class TestAuth(unittest.TestCase):
	def test_oauth2_token_is_reused_then_renewed(self):
		clock = Clock()
		conn, session = generic({("GET", "/v2/health"): [FakeResponse(200, {"ok": True})]}, clock=clock)
		conn.ping()
		conn.ping()
		self.assertEqual([s["path"] for s in session.sent].count("/oauth/token"), 1)
		login = session.sent[0]
		self.assertEqual(login["data"]["grant_type"], "client_credentials")
		self.assertEqual(login["data"]["scope"], "titulos")
		clock.now += 3600
		conn.ping()
		self.assertEqual([s["path"] for s in session.sent].count("/oauth/token"), 2)

	def test_shared_token_cache_is_used(self):
		from cred_hub.erp.base import Token

		cfg = config()
		stored = []
		session = FakeSession({("GET", "/v2/health"): [FakeResponse(200, {})], ("POST", "/oauth/token"): [TOKEN]})
		cls = connectors.get(GENERIC).with_config(cfg)
		client = cls.make_client({"rest_config": cfg.data, "rest_secrets": SECRETS}, session=session,
			token=Token("cached", 10**12), on_token=stored.append)
		cls(client).ping()
		self.assertEqual([s["path"] for s in session.sent], ["/v2/health"])
		self.assertEqual(session.sent[0]["headers"]["Authorization"], "Bearer cached")

	def test_401_renews_once_then_is_a_credential_error(self):
		conn, session = generic({("GET", "/v2/health"): [FakeResponse(401, {"message": "expired"}), FakeResponse(200, {})]})
		conn.ping()
		self.assertEqual([s["path"] for s in session.sent].count("/oauth/token"), 2)
		conn, _session = generic({("GET", "/v2/health"): [FakeResponse(401, {"message": "no"})]})
		with self.assertRaises(ErpAuthError):
			conn.ping()

	def test_static_auth_kinds(self):
		cases = (
			({"type": "bearer", "token": "{secret:token}"}, "Authorization", "Bearer tk-123456"),
			({"type": "header", "header": "X-Api-Key", "value": "{secret:token}"}, "X-Api-Key", "tk-123456"),
			({"type": "basic", "username": "{secret:user}", "password": "{secret:token}"}, "Authorization",
				"Basic dXNyOnRrLTEyMzQ1Ng=="),
		)
		for auth, header, value in cases:
			with self.subTest(auth["type"]):
				cfg = config(auth=auth, headers={})
				conn, session = generic({("GET", "/v2/health"): [FakeResponse(200, {})]}, cfg,
					secrets={"token": "tk-123456", "user": "usr"})
				conn.ping()
				self.assertEqual(session.sent[-1]["headers"][header], value)
				self.assertNotIn("/oauth/token", [s["path"] for s in session.sent])

	def test_static_auth_works_with_the_shared_token_cache(self):
		cfg = config(auth={"type": "bearer", "token": "{secret:token}"}, headers={})
		stored = []
		cls = connectors.get(GENERIC).with_config(cfg)
		session = FakeSession({("GET", "/v2/health"): [FakeResponse(200, {})]})
		client = cls.make_client({"rest_config": cfg.data, "rest_secrets": {"token": "tk-123456"}}, session=session,
			on_token=stored.append)
		cls(client).ping()
		self.assertEqual(stored[0].value, "static")
		self.assertLess(stored[0].expires_at, 10**11)  # finite: settings._store_token turns it into a TTL
		self.assertNotIn("tk-123456", stored[0].value)

	def test_oauth2_refused_login(self):
		session_routes = {("GET", "/v2/health"): [FakeResponse(200, {})]}
		cfg = config()
		cls = connectors.get(GENERIC).with_config(cfg)
		session = FakeSession({("POST", "/oauth/token"): [FakeResponse(400, {"error": "invalid_client"})], **session_routes})
		client = cls.make_client({"rest_config": cfg.data, "rest_secrets": SECRETS}, session=session)
		with self.assertRaises(ErpAuthError) as caught:
			cls(client).ping()
		self.assertNotIn("s3cr3t", str(caught.exception))

	def test_odd_expires_in_never_breaks_the_login(self):
		for value in ("soon", None, "", "1e400", -5, [1]):
			with self.subTest(value):
				answer = FakeResponse(200, {"access_token": "tok-x", "expires_in": value})
				cfg = config()
				cls = connectors.get(GENERIC).with_config(cfg)
				session = FakeSession({("POST", "/oauth/token"): [answer], ("GET", "/v2/health"): [FakeResponse(200, {})]})
				clock = Clock()
				client = cls.make_client({"rest_config": cfg.data, "rest_secrets": SECRETS}, session=session, clock=clock)
				cls(client).ping()
				self.assertTrue(clock.now < client.token.expires_at <= clock.now + 24 * 3600)

	def test_access_token_and_basic_pair_are_masked(self):
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(200, {})]})
		conn.ping()
		self.assertEqual(conn.client.redact("echo tok-1"), "echo ***")
		cfg = config(auth={"type": "basic", "username": "{secret:user}", "password": "{secret:token}"}, headers={})
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(200, {})]}, cfg, secrets={"token": "tk-123456", "user": "usr"})
		conn.ping()
		self.assertEqual(conn.client.redact("Basic dXNyOnRrLTEyMzQ1Ng=="), "Basic ***")

	def test_shared_pace_is_used_per_call(self):
		class Pace:
			def __init__(self):
				self.waits = []

			def wait(self, interval):
				self.waits.append(interval)

		pace = Pace()
		cfg = config(pace={"min_interval": 2})
		cls = connectors.get(GENERIC).with_config(cfg)
		session = FakeSession({("POST", "/oauth/token"): [TOKEN], ("GET", "/v2/health"): [FakeResponse(200, {})]})
		client = cls.make_client({"rest_config": cfg.data, "rest_secrets": SECRETS}, session=session, pace=pace)
		cls(client).ping()
		self.assertEqual(pace.waits, [2.0, 2.0])  # login + ping

	def test_redis_pace_waits_for_the_slot_of_another_worker(self):
		from cred_hub.erp.connectors.rest import RedisPace

		class Cache:
			def __init__(self):
				self.taken = 2  # another worker holds the slot for the next 2 attempts
				self.calls = []

			def make_key(self, key):
				return f"site|{key}"

			def set(self, key, value, px=None, nx=None):
				self.calls.append((key, px, nx))
				self.taken -= 1
				return self.taken < 0

			def pttl(self, key):
				return 400

		cache, slept = Cache(), []
		RedisPace("rest:abc", cache=cache, sleep=slept.append).wait(1.5)
		self.assertEqual(slept, [0.4, 0.4])
		self.assertEqual(cache.calls[-1], ("site|cred_hub_erp_pace:rest:abc", 1500, True))

	def test_listing_has_a_total_deadline(self):
		clock = Clock()
		answers = [page([{**ITEM, "id": n}, {**ITEM, "id": n + 100}]) for n in range(5)]
		conn, _ = generic({("GET", "/v2/titulos"): answers}, clock=clock)
		titles = conn.overdue_titles(date(2026, 11, 1))
		next(titles)
		clock.now += rest.RUN_DEADLINE + 1
		with self.assertRaisesRegex(ErpError, "30 minutes"):
			list(titles)


class TestErrors(unittest.TestCase):
	def test_maintenance_rate_limit_and_errors(self):
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(503, {"message": "manutenção"})]})
		with self.assertRaises(ErpMaintenance):
			conn.ping()
		limited = FakeResponse(429, {"message": "slow down"})
		limited.headers = {"Retry-After": "42"}
		conn, _ = generic({("GET", "/v2/health"): [limited]})
		with self.assertRaises(ErpRateLimited) as caught:
			conn.ping()
		self.assertEqual(caught.exception.wait_seconds, 42)
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(400, {"message": "bad tenant-42 s3cr3t-value-xyz"})]})
		with self.assertRaises(ErpError) as caught:
			conn.ping()
		self.assertNotIn("s3cr3t-value-xyz", str(caught.exception))
		self.assertNotIn("tenant-42", str(caught.exception))

	def test_not_json_and_wrong_items_path(self):
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(200, None, text="<html>")]})
		with self.assertRaisesRegex(ErpError, "not JSON"):
			conn.ping()
		conn, _ = generic({("GET", "/v2/titulos"): [FakeResponse(200, {"data": {"items": {"x": 1}}})]})
		with self.assertRaisesRegex(ErpError, "list of items"):
			list(conn.overdue_titles(date(2026, 11, 1)))

	def test_redaction_of_query_and_secrets(self):
		conn, _ = generic({})
		text = conn.client.redact("GET https://api.acme-erp.com.br/v2/x?token=abc&b=1 with s3cr3t-value-xyz")
		self.assertEqual(text, "GET https://api.acme-erp.com.br/v2/x?*** with ***")

	def test_ping_result_has_no_secret_nor_query(self):
		conn, _ = generic({("GET", "/v2/health"): [FakeResponse(200, {"token": "x"})]})
		result = conn.ping()
		self.assertEqual(set(result), {"status", "endpoint", "ms"})
		self.assertEqual(result["endpoint"], "/health")


class Pending:
	def __init__(self):
		self.keys = set()

	def get(self, key):
		return key in self.keys

	def set(self, key):
		self.keys.add(key)


PAYLOAD = {"documento": "23268852000160", "razaoSocial": "Cedente LTDA"}


class TestRegisterCedente(unittest.TestCase):
	def test_catalog_and_suggested_de_para_come_from_the_config(self):
		cls = connectors.get(GENERIC).with_config(config())
		self.assertEqual([f["erp_field"] for f in cls.catalog()], ["documento", "razaoSocial", "observacao"])
		self.assertEqual(cls.catalog()[0]["required"], "yes")
		self.assertEqual([r["erp_field"] for r in cls.default_rules()], ["documento", "razaoSocial"])

	def test_existing_cedente_is_not_sent(self):
		conn, session = generic({("GET", "/v2/cedentes"): [FakeResponse(200, {"itens": [
			{"id": "C-1", "doc": "11.111.111/0001-11"}, {"id": "C-7", "doc": "23.268.852/0001-60"}]})]})
		outcome = conn.send_registration(PAYLOAD, "23268852000160", Pending())
		self.assertEqual((outcome.status, outcome.code), ("already", "C-7"))
		self.assertEqual(session.sent[-1]["params"]["documento"], "23268852000160")
		self.assertNotIn("POST", [s["method"] for s in session.sent if s["path"] == "/v2/cedentes"])

	def test_lookup_answer_with_other_documents_is_not_a_match(self):
		# A system that ignores the filter and returns its whole registry must not block the registration
		conn, _session = generic({
			("GET", "/v2/cedentes"): [FakeResponse(200, {"itens": [{"id": "C-1", "doc": "11.111.111/0001-11"}, {"id": "C-2"}]})],
			("POST", "/v2/cedentes"): [FakeResponse(201, {"data": {"id": 9002}})],
		})
		outcome = conn.send_registration(PAYLOAD, "23268852000160", Pending())
		self.assertEqual((outcome.status, outcome.code), ("created", "9002"))

	def test_created_with_body_template_and_id(self):
		conn, session = generic({
			("GET", "/v2/cedentes"): [FakeResponse(200, {"itens": []})],
			("POST", "/v2/cedentes"): [FakeResponse(201, {"data": {"id": 9001}})],
		})
		outcome = conn.send_registration(PAYLOAD, "23268852000160", Pending())
		self.assertEqual((outcome.status, outcome.code), ("created", "9001"))
		self.assertEqual(session.sent[-1]["json"], {"cedente": PAYLOAD, "origem": "hubtech"})

	def test_already_signals(self):
		for answer in (FakeResponse(409, {"message": "conflict"}), FakeResponse(422, {"message": "CNPJ já cadastrado"}),
				FakeResponse(200, {"message": "Cedente já cadastrado"})):
			with self.subTest(answer.status_code):
				conn, _ = generic({("GET", "/v2/cedentes"): [FakeResponse(404, {})], ("POST", "/v2/cedentes"): [answer]})
				self.assertEqual(conn.send_registration(PAYLOAD, "23268852000160", Pending()).status, "already")

	def test_sent_without_code_is_never_sent_twice(self):
		pending = Pending()
		conn, session = generic({
			("GET", "/v2/cedentes"): [FakeResponse(200, {"itens": []})],
			("POST", "/v2/cedentes"): [FakeResponse(202, {"message": "recebido"})],
		})
		self.assertEqual(conn.send_registration(PAYLOAD, "23268852000160", pending).status, "sent")
		self.assertEqual(conn.send_registration(PAYLOAD, "23268852000160", pending).status, "pending")
		self.assertEqual([s["method"] for s in session.sent if s["path"] == "/v2/cedentes"].count("POST"), 1)

	def test_refusal_is_an_error(self):
		conn, _ = generic({("GET", "/v2/cedentes"): [FakeResponse(200, {"itens": []})],
			("POST", "/v2/cedentes"): [FakeResponse(400, {"message": "email inválido"})]})
		with self.assertRaisesRegex(ErpError, "email inválido"):
			conn.send_registration(PAYLOAD, "23268852000160", Pending())


# SSRF rules of ADR 0007 §6 on the real session (DNS and transport replaced, nothing leaves the machine) ---------


class FakeRaw:
	def __init__(self, body: bytes):
		self.body = body

	def read1(self, n):
		chunk, self.body = self.body[:n], self.body[n:]
		return chunk


class FakeHttpResponse:
	def __init__(self, status=200, body=b"{}", headers=None):
		self.status_code = status
		self.headers = headers or {}
		self.raw = FakeRaw(body)

	def close(self):
		pass


class FakeTransport:
	def __init__(self, answers):
		self.answers = list(answers)
		self.sent = []

	def request(self, method, url, **kwargs):
		self.sent.append((method, url, kwargs))
		return self.answers.pop(0)


def public_dns(host, port):
	return None


def addrinfo(*ips):
	return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


class TestSafeApi(unittest.TestCase):
	def session(self, answers, **kwargs):
		return safe_api.SafeApiSession(transport=FakeTransport(answers), resolve=kwargs.pop("resolve", public_dns), **kwargs)

	def test_only_https(self):
		with self.assertRaises(safe_api.BlockedAddress):
			self.session([]).request("GET", "http://api.acme.com.br/x")

	def test_ip_literals_and_internal_names_are_refused(self):
		for url in ("https://127.0.0.1/", "https://169.254.169.254/latest/meta-data", "https://[::1]/",
				"https://metadata.google.internal/", "https://db.local/", "https://api.acme.com.br:6379/"):
			with self.subTest(url), self.assertRaises(safe_api.BlockedAddress):
				self.session([]).request("GET", url)

	def test_names_resolving_to_private_metadata_or_loopback_are_refused(self):
		for ip in ("10.0.0.8", "172.17.0.2", "192.168.1.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "::1",
				"fd00::1", "::ffff:10.0.0.1"):
			with self.subTest(ip), patch.object(socket, "getaddrinfo", addrinfo(ip)), self.assertRaises(safe_api.BlockedAddress):
				safe_api.SafeApiSession(transport=FakeTransport([])).request("GET", "https://api.acme.com.br/")
		# One public and one private address: refused too (the connection could pick either)
		with patch.object(socket, "getaddrinfo", addrinfo("8.8.8.8", "10.1.1.1")), self.assertRaises(safe_api.BlockedAddress):
			safe_api.SafeApiSession(transport=FakeTransport([])).request("GET", "https://api.acme.com.br/")

	def test_public_name_goes_through(self):
		transport = FakeTransport([FakeHttpResponse(200, b'{"ok": true}')])
		with patch.object(socket, "getaddrinfo", addrinfo("8.8.8.8")):
			response = safe_api.SafeApiSession(transport=transport).request("GET", "https://api.acme.com.br/x",
				params={"a": 1})
		self.assertEqual(response.json(), {"ok": True})
		_method, _url, kwargs = transport.sent[0]
		self.assertFalse(kwargs["allow_redirects"])
		self.assertEqual(kwargs["params"], {"a": 1})

	def test_connected_address_is_checked_again_on_the_socket(self):
		class Sock:
			closed = False

			def getpeername(self):
				return ("10.0.0.9", 443)

			def close(self):
				self.closed = True

		sock = Sock()
		with self.assertRaises(safe_api.BlockedAddress):
			safe_http._checked(sock)
		self.assertTrue(sock.closed)
		transport = safe_api.SafeApiSession()._transport()
		self.assertIsInstance(transport.get_adapter("https://api.acme.com.br/"), safe_http._SafeAdapter)
		self.assertFalse(transport.trust_env)

	def test_redirects_are_not_followed_by_default(self):
		answers = [FakeHttpResponse(302, b"", {"Location": "https://api.acme.com.br/other"})]
		with self.assertRaisesRegex(safe_api.BlockedAddress, "redirects"):
			self.session(answers).request("GET", "https://api.acme.com.br/x")

	def test_redirect_to_another_host_never_carries_the_credentials(self):
		# A -> B: B is never called, so it never sees the token, even when redirects are allowed
		for location in ("https://collector.evil.com/x", "https://internal-api.acme.com.br/x",
				"https://api.acme.com.br:8443/x", "http://api.acme.com.br/x"):
			with self.subTest(location):
				transport = FakeTransport([FakeHttpResponse(302, b"", {"Location": location}), FakeHttpResponse()])
				session = safe_api.SafeApiSession(transport=transport, resolve=public_dns, max_redirects=3)
				with self.assertRaisesRegex(safe_api.BlockedAddress, "another host"):
					session.request("GET", "https://api.acme.com.br/x", headers={"X-Api-Key": "tk-123456"})
				self.assertEqual([url for _m, url, _k in transport.sent], ["https://api.acme.com.br/x"])

	def test_allowed_same_host_redirect_is_checked_again(self):
		transport = FakeTransport([FakeHttpResponse(301, b"", {"Location": "/v3/x"}), FakeHttpResponse(200, b'{"ok": 1}')])
		calls = []

		def resolve(host, port):
			calls.append(host)

		response = safe_api.SafeApiSession(transport=transport, resolve=resolve, max_redirects=1).request(
			"GET", "https://api.acme.com.br/x")
		self.assertEqual(response.json(), {"ok": 1})
		self.assertEqual(calls, ["api.acme.com.br", "api.acme.com.br"])
		self.assertEqual(transport.sent[1][1], "https://api.acme.com.br/v3/x")

	def test_response_size_is_limited(self):
		answers = [FakeHttpResponse(200, b"x" * 5000)]
		with self.assertRaises(safe_api.TooLarge):
			self.session(answers, max_bytes=1000).request("GET", "https://api.acme.com.br/x")

	def test_gzip_answer_is_decoded_and_the_limit_counts_decoded_bytes(self):
		import gzip

		body = json.dumps({"itens": [{"doc": "1"}] * 10}).encode()
		answers = [FakeHttpResponse(200, gzip.compress(body), {"Content-Encoding": "gzip"})]
		response = self.session(answers).request("GET", "https://api.acme.com.br/x")
		self.assertEqual(response.json()["itens"][0], {"doc": "1"})
		bomb = gzip.compress(b"0" * 5_000_000)  # ~5 KB on the wire, 5 MB once opened
		self.assertLess(len(bomb), 1000 * 10)
		answers = [FakeHttpResponse(200, bomb, {"Content-Encoding": "gzip"})]
		with self.assertRaises(safe_api.TooLarge):
			self.session(answers, max_bytes=100_000).request("GET", "https://api.acme.com.br/x")
		answers = [FakeHttpResponse(200, b"x", {"Content-Encoding": "br"})]
		with self.assertRaises(safe_api.TooLarge):
			self.session(answers).request("GET", "https://api.acme.com.br/x")

	def test_header_allow_list(self):
		for name in ("Host", "host", "X-Forwarded-For", "Forwarded", "Proxy-Authorization", "Connection",
				"Content-Length", "X-Real-IP", "Bad Header"):
			with self.subTest(name), self.assertRaises(safe_api.BlockedAddress):
				self.session([FakeHttpResponse()]).request("GET", "https://api.acme.com.br/x", headers={name: "v"})
		with self.assertRaises(safe_api.BlockedAddress):
			self.session([FakeHttpResponse()]).request("GET", "https://api.acme.com.br/x", headers={"X-A": "1\r\nHost: b"})
		transport = FakeTransport([FakeHttpResponse()])
		safe_api.SafeApiSession(transport=transport, resolve=public_dns).request(
			"GET", "https://api.acme.com.br/x", headers={"X-Api-Key": "k", "API-CLIENT-KEY": "k2"})
		self.assertEqual(transport.sent[0][2]["headers"], {"X-Api-Key": "k", "API-CLIENT-KEY": "k2"})

	def test_blocked_address_becomes_a_safe_erp_error(self):
		cfg = config()
		cls = connectors.get(GENERIC).with_config(cfg)
		client = cls.make_client({"rest_config": cfg.data, "rest_secrets": SECRETS})
		self.assertIsInstance(client.session, safe_api.SafeApiSession)
		with patch.object(socket, "getaddrinfo", addrinfo("169.254.169.254")), self.assertRaises(ErpError) as caught:
			cls(client).ping()
		self.assertEqual(caught.exception.code, "blocked")
		self.assertNotIn("s3cr3t", str(caught.exception))


if __name__ == "__main__":
	unittest.main()
