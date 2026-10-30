"""Assertiva Localize (credtech#290, módulo 1). A API é simulada com as respostas de exemplo da documentação
(`api documentation/localize/README.md`); nada sai do servidor. Roda no bench: `bench --site <site> run-tests
--module cred_hub.tests.test_assertiva`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.integrations import SETTINGS, assertiva, bureau_log, localize
from cred_hub.integrations.assertiva import AssertivaClient, AssertivaError

CNPJ = "11.222.333/0001-81"
CNPJ_RESPONSE = {
	"protocolo": "LOC-20261029-77192",
	"empresa": {"cnpj": "11222333000181", "razaoSocial": "EMPRESA DEVEDORA EXEMPLO LTDA"},
	"telefones": [
		{"ddd": "19", "numero": "32104455", "tipo": "FIXO", "scoreAssertividade": 92, "operadora": "CLARO", "temWhatsApp": False},
		{"ddd": "19", "numero": "998765432", "tipo": "MOVEL", "scoreAssertividade": 95, "operadora": "VIVO", "temWhatsApp": True},
	],
	"emails": [{"email": "financeiro@exemplodistribuidora.com.br", "score": 90, "tipo": "CORPORATIVO"}],
	"quadroSocietario": [
		{"cpf": "12345678909", "nome": "CARLOS EDUARDO SILVA", "qualificacao": "SÓCIO-ADMINISTRADOR", "percentualCapital": 80.0},
		{"cpf": "98765432100", "nome": "MARIANA SOUZA SILVA", "qualificacao": "SÓCIO", "percentualCapital": 20.0},
		{"cpf": "12345678000199", "nome": "HOLDING SOCIA LTDA", "qualificacao": "SÓCIO"},
	],
}
CPF_RESPONSES = {
	"12345678909": {
		"protocolo": "LOC-20261029-88321",
		"dadosCadastrais": {"cpf": "12345678909", "nome": "CARLOS EDUARDO SILVA"},
		"telefones": [
			{"ddd": "11", "numero": "981234567", "tipo": "MOVEL", "operadora": "TIM", "scoreAssertividade": 78, "temWhatsApp": True},
			{"ddd": "19", "numero": "998765432", "tipo": "MOVEL", "operadora": "VIVO", "scoreAssertividade": 96, "temWhatsApp": True},
			{"ddd": "19", "numero": "32510099", "tipo": "FIXO", "operadora": "CLARO", "scoreAssertividade": 60, "temWhatsApp": False},
		],
		"emails": [{"email": "carlos.esilva@gmail.com", "score": 92}],
	},
	"98765432100": None,  # não localizado (404)
}


class FakeResponse:
	def __init__(self, status, body=None):
		self.status_code = status
		self._body = body

	def json(self):
		return self._body


class FakeSession:
	def __init__(self, token_status=200, gets=None):
		self.token_status = token_status
		self.gets = list(gets or [])
		self.posts = []
		self.calls = []

	def post(self, url, **kwargs):
		self.posts.append(kwargs)
		if self.token_status != 200:
			return FakeResponse(self.token_status)
		return FakeResponse(200, {"access_token": f"tok{len(self.posts)}", "expires_in": 3600})

	def get(self, url, **kwargs):
		self.calls.append((url, kwargs["params"], kwargs["headers"]["Authorization"]))
		return self.gets.pop(0)


class TestAssertivaParsing(IntegrationTestCase):
	def test_phones_whatsapp_first_then_score(self):
		phones = assertiva.phones(CPF_RESPONSES["12345678909"])
		self.assertEqual([p["phone"] for p in phones], ["+5519998765432", "+5511981234567", "+551932510099"])
		self.assertEqual((phones[0]["whatsapp"], phones[0]["score"], phones[0]["carrier"]), (True, 96, "VIVO"))
		self.assertFalse(phones[2]["mobile"])
		self.assertFalse(phones[2]["whatsapp"])

	def test_envelope_with_mobiles_and_fixed(self):
		payload = {
			"cabecalho": {"protocolo": "P-1"},
			"resposta": {
				"telefones": {
					"moveis": [{"numero": "(48) 99988-7766", "aplicativos": {"whatsApp": True}}],
					"fixos": [{"numero": "(48) 3222-1100"}],
				},
				"socios": [{"documento": "123.456.789-09", "nomeOuRazaoSocial": "ANA DA SILVA"}],
			},
		}
		self.assertEqual(assertiva.protocol_of(payload), "P-1")
		phones = assertiva.phones(payload)
		self.assertEqual(phones[0], {"phone": "+5548999887766", "mobile": True, "whatsapp": True, "score": 0, "carrier": ""})
		self.assertEqual(phones[1]["phone"], "+554832221100")
		self.assertEqual(assertiva.partners(payload)[0]["cpf"], "12345678909")

	def test_partners_are_people_only(self):
		partners = assertiva.partners(CNPJ_RESPONSE)
		self.assertEqual([p["cpf"] for p in partners], ["12345678909", "98765432100"])
		self.assertEqual(partners[0]["share"], 80.0)

	def test_token_is_cached_and_renewed_on_401(self):
		assertiva.forget_token()
		session = FakeSession(gets=[FakeResponse(401), FakeResponse(200, CNPJ_RESPONSE)])
		client = AssertivaClient("id", "secret", session=session, sleep=lambda s: None)
		status, body = client.localize_cnpj(CNPJ, 2)
		self.assertEqual((status, body["protocolo"]), (200, "LOC-20261029-77192"))
		self.assertEqual([c[2] for c in session.calls], ["Bearer tok1", "Bearer tok2"])
		self.assertEqual(session.calls[0][1], {"cnpj": "11222333000181", "idFinalidade": "2"})
		# o token novo ficou no cache: outra consulta não pede token de novo
		session.gets.append(FakeResponse(404))
		self.assertEqual(client.localize_cnpj(CNPJ, 2), (404, None))
		self.assertEqual(len(session.posts), 2)
		assertiva.forget_token()

	def test_errors_are_messages_and_retry_only_429_and_5xx(self):
		assertiva.forget_token()
		session = FakeSession(gets=[FakeResponse(429), FakeResponse(503), FakeResponse(403)])
		client = AssertivaClient("id", "secret", session=session, sleep=lambda s: None)
		with self.assertRaises(AssertivaError) as error:
			client.localize_cpf("12345678909")
		self.assertEqual(error.exception.http_status, 403)
		self.assertEqual(len(session.calls), 3)
		with self.assertRaises(AssertivaError):
			AssertivaClient("id2", "secret", session=FakeSession(token_status=401)).token()
		assertiva.forget_token()


class TestLocalizeOrganization(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"assertiva_enabled": 1,
				"assertiva_client_id": "client",
				"assertiva_client_secret": "secret",
				"assertiva_partner_lookup": 1,
				"assertiva_cache_days": 30,
				"cenprot_enabled": 0,
			}
		)
		settings.save()
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Exemplo Localize Teste", "cnpj": CNPJ}
		).insert()
		# sócio que a consulta da Receita já tinha cadastrado, só com o nome
		self.carlos = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Carlos Eduardo Silva", "company_name": self.org.name}
		).insert()

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _calls(self):
		calls = []

		def cnpj(_self, document, purpose):
			calls.append(("cnpj", document, purpose))
			return 200, CNPJ_RESPONSE

		def cpf(_self, document, purpose):
			calls.append(("cpf", document, purpose))
			body = CPF_RESPONSES[document]
			return (200, body) if body else (404, None)

		return calls, patch.multiple(AssertivaClient, localize_cnpj=cnpj, localize_cpf=cpf)

	def test_enrich_creates_and_updates_partner_contacts(self):
		calls, mocked = self._calls()
		with mocked:
			result = localize.enrich_organization(self.org.name)
		self.assertTrue(result["ok"], result)
		self.assertEqual((result["created"], result["updated"], result["paid_queries"]), (1, 1, 3))
		self.assertEqual(calls[0], ("cnpj", "11222333000181", 2))

		carlos = frappe.get_doc("Contact", self.carlos.name)
		self.assertEqual(carlos.cpf, "123.456.789-09")
		self.assertEqual(carlos.mobile_no, "+5519998765432")
		self.assertEqual(carlos.phone, "+551932510099")
		self.assertEqual((carlos.has_whatsapp, carlos.phone_assertiveness, carlos.phone_carrier), (1, 96, "VIVO"))
		self.assertEqual(carlos.share_percentage, 80.0)
		self.assertEqual(carlos.designation, "Sócio-administrador")
		self.assertEqual(carlos.email_id, "carlos.esilva@gmail.com")
		self.assertEqual(carlos.assertiva_protocol, "LOC-20261029-88321")
		self.assertEqual(len(carlos.phone_nos), 3)

		mariana = frappe.get_doc("Contact", {"company_name": self.org.name, "cpf": "987.654.321-00"})
		self.assertEqual(mariana.full_name, "Mariana Souza Silva")
		self.assertFalse(mariana.phone_nos)
		# telefone e e-mail da empresa preenchem os da Receita que estavam vazios
		org = frappe.get_doc("CRM Organization", self.org.name)
		self.assertEqual((org.registry_phone, org.registry_email), ("+5519998765432", "financeiro@exemplodistribuidora.com.br"))
		logs = frappe.get_all(bureau_log.QUERY, {"reference_name": self.org.name}, ["operation", "status", "purpose"])
		self.assertEqual(sorted((log.operation, log.status) for log in logs), [
			("localize_cnpj", "Success"), ("localize_cpf", "Not Found"), ("localize_cpf", "Success"),
		])
		self.assertEqual({log.purpose for log in logs}, {"Credit protection and collection"})

	def test_second_click_uses_cache_and_does_not_duplicate(self):
		calls, mocked = self._calls()
		with mocked:
			localize.enrich_organization(self.org.name)
			result = localize.enrich_organization(self.org.name)
			self.assertEqual(len(calls), 3)
			self.assertTrue(result["cached"])
			self.assertEqual((result["created"], result["updated"], result["phones"]), (0, 2, 0))
			# nova consulta paga só quando pedida
			forced = localize.enrich_organization(self.org.name, force=1)
		self.assertEqual(len(calls), 6)
		self.assertFalse(forced["cached"])
		self.assertEqual(frappe.db.count("Contact", {"company_name": self.org.name}), 2)
		status = localize.get_status(self.org.name)
		self.assertTrue(status["enabled"] and status["has_cnpj"] and status["last_query_on"])

	def test_existing_mobile_stays_primary_when_assertiva_has_it(self):
		self.carlos.append("phone_nos", {"phone": "+5511981234567", "is_primary_mobile_no": 1})
		self.carlos.save()
		_calls, mocked = self._calls()
		with mocked:
			localize.enrich_organization(self.org.name)
		carlos = frappe.get_doc("Contact", self.carlos.name)
		self.assertEqual((carlos.mobile_no, carlos.phone_assertiveness, carlos.phone_carrier), ("+5511981234567", 78, "TIM"))

	def test_provider_error_is_returned_and_recorded(self):
		def fail(_self, document, purpose):
			raise AssertivaError("sem saldo", 403)

		with patch.object(AssertivaClient, "localize_cnpj", fail):
			result = localize.enrich_organization(self.org.name)
		self.assertEqual(result, {"ok": False, "message": "sem saldo"})
		log = frappe.get_all(bureau_log.QUERY, {"reference_name": self.org.name}, ["status", "http_status", "error"])
		self.assertEqual((log[0].status, log[0].http_status, log[0].error), ("Error", 403, "sem saldo"))

	def test_needs_valid_cnpj_and_enabled(self):
		frappe.db.set_value("CRM Organization", self.org.name, "cnpj", "")
		self.assertRaises(frappe.ValidationError, localize.enrich_organization, self.org.name)
		frappe.db.set_single_value(SETTINGS, "assertiva_enabled", 0)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.assertEqual(localize.get_status(self.org.name), {"enabled": False})
		self.assertRaises(frappe.ValidationError, localize.enrich_organization, self.org.name)
