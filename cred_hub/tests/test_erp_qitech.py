"""QI Tech connector (ADR 0007 §10, credtech#375) against canned answers shaped exactly like the public IaaS docs
(docs.qitech.com.br/documentation/iaas, read 11/11/2026). Pure: `python -m pytest cred_hub/tests/test_erp_qitech.py`
(no Frappe, nothing leaves the machine; the key pair is generated in memory)."""

import hashlib
import json
import unittest
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cred_hub.erp import connectors
from cred_hub.erp.base import ErpAuthError, ErpError, ErpMaintenance, ErpRateLimited
from cred_hub.erp.connectors.qitech import QITECH, normalize_pem
from cred_hub.erp.mapper import Rule, build
from cred_hub.tests.test_erp_clients import FakeResponse, FakeSession

_KEY = ec.generate_private_key(ec.SECP521R1())
PRIVATE_PEM = _KEY.private_bytes(
	serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
	serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()
API_KEY = "qi-api-key-7f3e9a"
NOW = datetime(2026, 11, 11, 15, 4, 5, tzinfo=timezone.utc)

# Verbatim from introducao/teste_de_autenticacao/endpoints_de_teste
PING_OK = FakeResponse(200, {"success": "Congrats!"})
# Verbatim error envelopes from introducao/teste_de_autenticacao and introducao/erros
INVALID_ENDPOINT = {"title": "Invalid endpoint", "description": "Invalid endpoint.",
	"translation": "O endpoint é invalido", "code": "MIT000015"}
NO_PERMISSION = {"title": "Manager does not have permission to access this endpoint",
	"description": "Manager does not have permission to access this endpoint",
	"translation": "Gestor nao tem permissão para acessar esse endpoint", "code": "MIT000017"}
# Shape of homologacao_cedente/consulta (assignor registry item), keys as documented
ASSIGNOR = {
	"assignor_registry_key": "c1d2e3f4-a5b6-4890-abcd-ef1234567890", "status": "registered",
	"name": "Cedente Exemplo Ltda", "document_number": "33.333.333/0001-91", "person_type": "legal_person",
	"email": "financeiro@cedente.com.br", "annual_revenues": 1200000,
	"address": {"street": "Rua Exemplo", "number": "100", "neighborhood": "Centro", "city": "São Paulo",
		"uf": "SP", "postal_code": "01310-100", "country": "BRA"},
}


def listing(rows, page=0, last=True, limit=50):
	return FakeResponse(200, {"data": rows, "page": page, "limit": limit, "is_last_page": last})


def qitech(routes, **values):
	cls = connectors.get(QITECH)
	session = FakeSession(routes)
	client = cls.make_client({
		"qitech_base_url": "https://assignor-api.sandbox.qidtvm.com.br", "qitech_api_key": API_KEY,
		"qitech_private_key": PRIVATE_PEM, **values,
	}, session=session)
	client.now = lambda: NOW
	return cls(client), session


def claims_of(sent):
	return jwt.decode(sent["headers"]["AUTHORIZATION"], PUBLIC_PEM, algorithms=["ES512"])


class TestSigning(unittest.TestCase):
	def test_get_is_signed_with_es512_over_method_uri_and_timestamp(self):
		conn, session = qitech({("GET", "/authentication_test"): [PING_OK]})
		self.assertEqual(conn.ping()["status"], "ok")
		sent = session.sent[0]
		self.assertEqual(sent["headers"]["API-CLIENT-KEY"], API_KEY)
		self.assertNotIn("Bearer", sent["headers"]["AUTHORIZATION"])
		header = jwt.get_unverified_header(sent["headers"]["AUTHORIZATION"])
		self.assertEqual((header["alg"], header["typ"]), ("ES512", "JWT"))
		self.assertEqual(claims_of(sent), {"timestamp": "2026-11-11T15:04:05", "method": "GET",
			"uri": "/authentication_test"})
		self.assertIsNone(sent.get("data"))

	def test_signed_uri_carries_the_query_string_exactly_as_sent(self):
		conn, session = qitech({("GET", "/assignor_registry/assignor_registries"): [listing([])]})
		conn.client.find_assignor("33.333.333/0001-91")
		sent = session.sent[0]
		self.assertEqual(claims_of(sent)["uri"], f"/assignor_registry/assignor_registries?{sent['query']}")
		self.assertEqual(sent["params"]["document_number"], "33.333.333%2F0001-91")

	def test_post_body_md5_is_over_the_exact_bytes_sent(self):
		conn, session = qitech({
			("GET", "/assignor_registry/assignor_registries"): [listing([])],
			("POST", "/assignor_registry/assignor_registry"): [FakeResponse(201, {**ASSIGNOR, "status": "pending_registry"})],
		})
		conn.send_registration({"name": "Cedente Exemplo Ltda", "document_number": "33.333.333/0001-91"}, "33333333000191")
		sent = session.sent[-1]
		claims = claims_of(sent)
		self.assertEqual(claims["method"], "POST")
		self.assertEqual(claims["payload_md5"], hashlib.md5(sent["data"]).hexdigest())
		self.assertEqual(json.loads(sent["data"])["name"], "Cedente Exemplo Ltda")
		self.assertEqual(sent["headers"]["Content-Type"], "application/json")

	def test_private_key_pasted_on_one_line_still_signs(self):
		one_line = PRIVATE_PEM.replace("\n", " ")
		self.assertEqual(normalize_pem(one_line).strip(), PRIVATE_PEM.strip())
		conn, session = qitech({("GET", "/authentication_test"): [PING_OK]}, qitech_private_key=one_line)
		conn.ping()
		self.assertEqual(claims_of(session.sent[0])["method"], "GET")

	def test_bad_key_is_a_credential_error_without_the_key(self):
		conn, _ = qitech({("GET", "/authentication_test"): [PING_OK]}, qitech_private_key="not a key")
		with self.assertRaises(ErpAuthError) as caught:
			conn.ping()
		self.assertNotIn("not a key", str(caught.exception))

	def test_only_qitech_hosts(self):
		for url in ("https://evil.example.com", "http://assignor-api.qidtvm.com.br", "https://qidtvm.com.br.evil.com",
				"https://assignor-api.qidtvm.com.br:8443"):
			with self.subTest(url), self.assertRaises(ErpError):
				qitech({}, qitech_base_url=url)

	def test_works_with_the_shared_token_cache(self):
		stored = []
		cls = connectors.get(QITECH)
		client = cls.make_client({"qitech_base_url": "https://assignor-api.sandbox.qidtvm.com.br",
			"qitech_api_key": API_KEY, "qitech_private_key": PRIVATE_PEM},
			session=FakeSession({("GET", "/authentication_test"): [PING_OK]}), on_token=stored.append)
		cls(client).ping()
		self.assertLess(stored[0].expires_at, 10**11)
		self.assertNotIn(API_KEY, stored[0].value)

	def test_missing_access_is_named(self):
		cls = connectors.get(QITECH)
		self.assertEqual(cls.missing_credentials({}), ["QI Tech API URL", "QI Tech API key", "QI Tech private key (PEM)"])


class TestErrors(unittest.TestCase):
	def test_401_twice_is_a_credential_error(self):
		conn, session = qitech({("GET", "/authentication_test"): [FakeResponse(401, INVALID_ENDPOINT)]})
		with self.assertRaises(ErpAuthError):
			conn.ping()
		self.assertEqual(len(session.sent), 2)  # signed again once (new timestamp), then gave up

	def test_permission_401_then_ok(self):
		conn, _ = qitech({("GET", "/authentication_test"): [FakeResponse(401, NO_PERMISSION), PING_OK]})
		self.assertEqual(conn.ping()["status"], "ok")

	def test_wrong_profile_host(self):
		conn, _ = qitech({("GET", "/assignor_registry/assignor_registries"): [
			FakeResponse(404, {"title": "Not found", "description": "Not found", "translation": "Não encontrado",
				"code": "QIT000404"})]})
		with self.assertRaisesRegex(ErpError, "host of your profile"):
			list(conn.cedentes())

	def test_server_error_and_rate_limit(self):
		conn, _ = qitech({("GET", "/authentication_test"): [FakeResponse(500, {"code": "QIT000500",
			"translation": "Erro interno"})]})
		with self.assertRaises(ErpMaintenance):
			conn.ping()
		conn, _ = qitech({("GET", "/authentication_test"): [FakeResponse(429, {})]})
		with self.assertRaises(ErpRateLimited):
			conn.ping()

	def test_errors_never_carry_the_secrets(self):
		conn, _ = qitech({("GET", "/authentication_test"): [FakeResponse(400, {"code": "QIT000001",
			"translation": "Body inválido"})]})
		with self.assertRaises(ErpError) as caught:
			conn.ping()
		self.assertIn("QIT000001", str(caught.exception))
		self.assertNotIn(API_KEY, str(caught.exception))
		self.assertNotIn("PRIVATE KEY", str(caught.exception))


class TestCedentes(unittest.TestCase):
	def test_registry_pages_until_the_last_and_gives_document_and_name(self):
		second = {**ASSIGNOR, "assignor_registry_key": "k2", "document_number": "647.121.478-22", "name": "Pessoa"}
		conn, session = qitech({("GET", "/assignor_registry/assignor_registries"): [
			listing([ASSIGNOR], 0, last=False), listing([second], 1, last=True)]})
		rows = list(conn.cedentes())
		self.assertEqual([r["CPF/CNPJ"] for r in rows], ["33333333000191", "64712147822"])
		self.assertEqual(rows[0]["address.city"], "São Paulo")
		self.assertEqual([s["params"]["page"] for s in session.sent], ["0", "1"])

	def test_repeated_page_never_loops(self):
		conn, _ = qitech({("GET", "/assignor_registry/assignor_registries"): [listing([ASSIGNOR], last=False)]})
		with self.assertRaisesRegex(ErpError, "same page"):
			list(conn.cedentes())

	def test_existing_cedente_is_not_sent(self):
		conn, session = qitech({("GET", "/assignor_registry/assignor_registries"): [listing([ASSIGNOR])]})
		outcome = conn.send_registration({"name": "x"}, "33333333000191")
		self.assertEqual((outcome.status, outcome.code), ("already", ASSIGNOR["assignor_registry_key"]))
		self.assertEqual([s["method"] for s in session.sent], ["GET"])

	def test_a_different_document_in_the_answer_is_not_taken_as_existing(self):
		conn, _ = qitech({
			("GET", "/assignor_registry/assignor_registries"): [listing([{**ASSIGNOR, "document_number": "1"}])],
			("POST", "/assignor_registry/assignor_registry"): [FakeResponse(201, {**ASSIGNOR, "status": "pending_registry"})],
		})
		self.assertEqual(conn.send_registration({"name": "x"}, "33333333000191").status, "created")

	def test_created_has_the_registry_key_and_tells_about_the_analysis(self):
		conn, _ = qitech({
			("GET", "/assignor_registry/assignor_registries"): [listing([])],
			("POST", "/assignor_registry/assignor_registry"): [FakeResponse(201, {**ASSIGNOR, "status": "pending_registry"})],
		})
		outcome = conn.send_registration({"name": "x"}, "33333333000191")
		self.assertEqual((outcome.status, outcome.code), ("created", ASSIGNOR["assignor_registry_key"]))
		self.assertIn("analysis", outcome.note)
		self.assertEqual(outcome.as_dict()["qitech_status"], "pending_registry")

	def test_default_de_para_builds_the_documented_payload(self):
		records = {
			"organization": {"legal_name": "Cedente Exemplo Ltda", "cnpj": "33333333000191", "registry_email":
				"FIN@CEDENTE.COM.BR", "annual_revenue": 1200000.0, "address_number": "100", "neighborhood": "Centro",
				"registry_phone": "(11) 3636-0268"},
			"address": {"address_line1": "Rua Exemplo, 100", "city": "São Paulo", "state": "sp", "pincode": "01310100"},
			"contact": {"full_name": "Ana Souza", "cpf": "64712147822", "email_id": "ana@cedente.com.br"},
			"deal": {}, "credit": {},
		}
		cls = connectors.get(QITECH)
		result = build([Rule.from_row(r) for r in cls.default_rules()], records)
		self.assertTrue(result.ok, (result.missing, result.errors))
		payload = cls(None).finalize_cedente(result.payload)
		self.assertEqual(payload["document_number"], "33.333.333/0001-91")
		self.assertEqual(payload["person_type"], "legal_person")
		self.assertEqual(payload["annual_revenues"], 1200000)
		self.assertIs(payload["is_in_national_financial_system"], False)
		self.assertEqual(payload["address"], {"street": "Rua Exemplo", "number": "100", "neighborhood": "Centro",
			"city": "São Paulo", "uf": "SP", "postal_code": "01310-100", "country": "BRA"})
		self.assertEqual(payload["phone"], {"area_code": "11", "number": "36360268"})
		self.assertEqual(payload["related_parties"], [{"name": "Ana Souza", "document_number": "647.121.478-22",
			"email": "ana@cedente.com.br", "related_party_type": "partner", "is_representative": True}])

	def test_no_representative_means_no_empty_related_party(self):
		payload = connectors.get(QITECH)(None).finalize_cedente({"document_number": "647.121.478-22",
			"related_parties": [{"related_party_type": "partner", "is_representative": True}]})
		self.assertEqual(payload["person_type"], "natural_person")
		self.assertNotIn("related_parties", payload)

	def test_capabilities_are_honest(self):
		caps = connectors.get(QITECH).capabilities
		self.assertTrue(caps.register_cedente and caps.cedente_registry)
		self.assertFalse(caps.overdue_titles or caps.settled_titles or caps.sacado_registry or caps.lists)


if __name__ == "__main__":
	unittest.main()
