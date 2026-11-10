"""ERP clients (credtech#367) against canned answers shaped like the WBA and Smart Securities docs.
Pure: `python -m pytest cred_hub/tests/test_erp_clients.py` (no Frappe, nothing leaves the machine)."""

import json
import unittest
from datetime import date
from decimal import Decimal

from cred_hub.erp.base import ErpAuthError, ErpMaintenance, ErpRateLimited
from cred_hub.erp.smart import SmartClient, normalize_title, outcome_of
from cred_hub.erp.wba import WbaClient, normalize_receivable


class FakeResponse:
	def __init__(self, status=200, body=None, text=None):
		self.status_code = status
		self._body = body
		self.text = text if text is not None else (json.dumps(body) if body is not None else "")

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class FakeSession:
	"""Answers by (method, path) from queues; records every request."""

	def __init__(self, routes):
		self.routes = {k: list(v) for k, v in routes.items()}
		self.sent = []

	def request(self, method, url, headers=None, timeout=None, **kwargs):
		path = "/" + url.split("://", 1)[1].split("/", 1)[1]
		path, _sep, query = path.partition("?")
		if query:
			kwargs["query"] = query
			kwargs["params"] = dict(pair.split("=", 1) for pair in query.split("&"))
		self.sent.append({"method": method, "path": path, "headers": headers or {}, **kwargs})
		queue = self.routes[(method, path)]
		return queue.pop(0) if len(queue) > 1 else queue[0]


class Clock:
	def __init__(self):
		self.now = 1000.0
		self.slept = []

	def __call__(self):
		return self.now

	def sleep(self, seconds):
		self.slept.append(seconds)
		self.now += seconds


WBA_TOKEN = FakeResponse(200, {"access_token": "wba-1", "expires_in": 600, "token_type": "bearer"})
SMART_TOKEN = FakeResponse(200, {"access_token": "sm-1", "expires_in": 3600, "token_type": "Bearer"})


def wba(routes, clock=None):
	clock = clock or Clock()
	session = FakeSession({("POST", "/oauth/token"): [WBA_TOKEN], **routes})
	return WbaClient("tenant-uuid", "user", "secret", session=session, clock=clock, sleep=clock.sleep), session


def smart(routes, clock=None):
	clock = clock or Clock()
	session = FakeSession({("POST", "/oauth"): [SMART_TOKEN], **routes})
	return SmartClient("cid", "csecret", session=session, clock=clock, sleep=clock.sleep), session, clock


class TestWba(unittest.TestCase):
	def test_login_is_form_password_grant_with_tenant_header(self):
		client, session = wba({("POST", "/v1/external/cadastro/filter"): [FakeResponse(200, {"list": [], "totalPages": 0})]})
		client.find_people(["23.268.852/0001-60"])
		login, query = session.sent
		self.assertEqual(login["data"], {"username": "user", "password": "secret", "grant_type": "password"})
		self.assertEqual(login["headers"]["Tenant-ID"], "tenant-uuid")
		self.assertEqual(query["headers"]["Tenant-ID"], "tenant-uuid")
		self.assertEqual(query["headers"]["Authorization"], "Bearer wba-1")
		self.assertEqual(query["json"]["listCnpj"], ["23268852000160"])
		self.assertEqual(query["json"]["paginacao"]["page"], 0)

	def test_cpf_goes_to_list_cpf(self):
		client, session = wba({("POST", "/v1/external/cadastro/filter"): [FakeResponse(200, {"list": [], "totalPages": 0})]})
		client.find_people(["647.121.478-22"])
		self.assertEqual(session.sent[-1]["json"]["listCpf"], ["64712147822"])
		self.assertNotIn("listCnpj", session.sent[-1]["json"])

	def test_wrong_password_raises_auth_error(self):
		session = FakeSession({("POST", "/oauth/token"): [FakeResponse(401, {"error": "invalid_grant", "error_description": "Bad credentials"})]})
		client = WbaClient("t", "u", "p", session=session)
		with self.assertRaises(ErpAuthError) as ctx:
			client.authenticate()
		self.assertIn("Bad credentials", str(ctx.exception))

	def test_token_is_renewed_before_it_expires(self):
		clock = Clock()
		client, session = wba(
			{("POST", "/v1/external/cadastro/filter"): [FakeResponse(200, {"list": [], "totalPages": 0})]}, clock
		)
		client.find_people(["23268852000160"])
		clock.now += 545  # 600 s token - 60 s margin = valid until +540
		client.find_people(["23268852000160"])
		self.assertEqual([s["path"] for s in session.sent].count("/oauth/token"), 2)

	def test_401_on_data_call_logs_in_again_once(self):
		client, session = wba({
			("POST", "/v1/external/cadastro/filter"): [
				FakeResponse(401, {"error": "invalid_token"}),
				FakeResponse(200, {"list": [], "totalPages": 0}),
			]
		})
		client.find_people(["23268852000160"])
		self.assertEqual([s["path"] for s in session.sent].count("/oauth/token"), 2)

	def test_error_envelope_raises(self):
		client, _ = wba({("POST", "/v1/external/bordero/filter"): [FakeResponse(200, {"error": True, "message": "Filtro inválido"})]})
		with self.assertRaisesRegex(Exception, "Filtro inválido"):
			list(client.list_borderos(date(2026, 1, 1)))

	def test_paginates_borderos_and_receivables(self):
		bordero = {"idBordero": "b-1", "documentoCedente": "43141963000118", "razaoSocialCedente": "CARIZON ENGENHARIA LTDA"}
		client, session = wba({
			("POST", "/v1/external/bordero/filter"): [
				FakeResponse(200, {"listBordero": [bordero], "totalPages": 2}),
				FakeResponse(200, {"listBordero": [{**bordero, "idBordero": "b-2"}], "totalPages": 2}),
			],
			("POST", "/v1/external/recebivel/filter"): [FakeResponse(200, {"listRecebivel": [{
				"idRecebivel": "r-1", "idOperacao": "o-1", "numeroDocumento": "404", "documentoSacado": "34622881000102",
				"nomeSacado": "WINITY S.A.", "valor": 20541.16, "dataVencimento": "18/11/2026", "modoCobranca": "BOLETO",
			}], "totalPages": 1})],
		})
		borderos = list(client.list_borderos(date(2026, 1, 1), date(2026, 11, 10)))
		self.assertEqual([b["idBordero"] for b in borderos], ["b-1", "b-2"])
		self.assertEqual(session.sent[1]["json"]["dataCriacaoDe"], "01/01/2026")
		self.assertEqual(session.sent[2]["json"]["paginacao"]["page"], 1)
		title = normalize_receivable(next(client.list_receivables("b-1")), borderos[0])
		self.assertEqual(title["due_date"], date(2026, 11, 18))
		self.assertEqual(title["amount"], Decimal("20541.16"))
		self.assertEqual(title["cedente_document"], "43141963000118")
		self.assertEqual(title["sacado_document"], "34622881000102")

	def test_send_people_returns_received_text(self):
		client, session = wba({("POST", "/v1/private/cs/cadastro/pessoa"): [FakeResponse(200, "Requisição Recebida.")]})
		self.assertEqual(client.send_people([{"documento": "1"}]), "Requisição Recebida.")
		self.assertEqual(session.sent[-1]["json"], [{"documento": "1"}])


def hal(key, rows, page=1, page_count=1):
	return FakeResponse(200, {"_embedded": {key: rows}, "page": page, "page_count": page_count, "total_items": len(rows)})


OPEN_ROW = {
	"ID_TITULO": "188134", "DOCUMENTO": "404", "TIPO": "DSR", "CPF_CNPJ_SACADO": "34.622.881/0001-02",
	"SACADO": "WINITY S.A.", "CPF_CNPJ_CEDENTE": "43.141.963/0001-18", "CEDENTE": "CARIZON ENGENHARIA LTDA",
	"DATA_EMISSAO": "20/10/2026", "ORIGINAL": "05/11/2026", "VENCIMENTO": "05/11/2026", "SITUACAO": "Inadimplente(Aberto)",
	"OP": "13594", "VALOR": "20.541,16", "LINHA_DIGITAVEL": "03399.12345 67890.123456 78901.234567 8 12345678901234",
}
PAID_ROW = {
	"ID_TITULO": 188134, "NUMERO": "404", "CPF_CNPJ_SACADO": "34.622.881/0001-02", "SACADO": "WINITY S.A.",
	"VENCIMENTO": "05/11/2026", "QUITACAO": "08/11/2026", "VALOR_FACE_": "20.541,16", "VALOR_LIQUIDADO": "20.600,00",
	"OP": 13594, "SITUACAO": "Quitado", "DATA_CUSTODIA": "00/00/0000",
}


class TestSmart(unittest.TestCase):
	def test_login_is_json_client_credentials(self):
		client, session, _ = smart({("GET", "/smartsecurities/setor"): [hal("setor", [{"idSetor": "1"}])]})
		self.assertEqual(client.ping(), {"sectors": 1})
		self.assertEqual(session.sent[0]["json"]["grant_type"], "client_credentials")
		self.assertEqual(session.sent[1]["headers"]["Authorization"], "Bearer sm-1")

	def test_titles_paginate_from_page_1_and_format_dates(self):
		client, session, _ = smart({("GET", "/smartsecurities/titulo"): [
			hal("titulo", [OPEN_ROW], 1, 2), hal("titulo", [OPEN_ROW], 2, 2),
		]})
		rows = list(client.titles("quitados", quitacaoIni=date(2026, 11, 1), quitacaoFim=date(2026, 11, 10), valorIni=10))
		self.assertEqual(len(rows), 2)
		first, second = session.sent[1]["params"], session.sent[2]["params"]
		self.assertEqual((first["page"], second["page"]), ("1", "2"))
		self.assertEqual(first["tipo"], "quitados")
		self.assertEqual(first["quitacaoIni"], "01/11/2026")
		self.assertEqual(first["valorIni"], "10")
		self.assertEqual(first["tipoSaida"], "json")
		# Slashes stay raw and a lone end date gets its start (Smart ignores half ranges)
		self.assertIn("quitacaoIni=01/11/2026", session.sent[1]["query"])

	def test_half_date_range_is_completed(self):
		client, session, _ = smart({("GET", "/smartsecurities/titulo"): [hal("titulo", [])]})
		list(client.titles("emaberto", vencimentoFim=date(2026, 11, 9)))
		params = session.sent[-1]["params"]
		self.assertEqual((params["vencimentoIni"], params["vencimentoFim"]), ("01/01/2000", "09/11/2026"))

	def test_calls_respect_20_per_minute(self):
		client, _, clock = smart({("GET", "/smartsecurities/titulo"): [
			hal("titulo", [], 1, 3), hal("titulo", [], 2, 3), hal("titulo", [], 3, 3),
		]})
		list(client.titles())
		# login + 3 pages = 4 calls -> 3 gaps of 3.1 s
		self.assertEqual(len(clock.slept), 3)
		self.assertTrue(all(abs(s - 3.1) < 1e-9 for s in clock.slept))

	def test_per_minute_429_waits_and_retries_once(self):
		client, _, clock = smart({("GET", "/smartsecurities/setor"): [
			FakeResponse(429, {"detail": "Too Many Requests"}), hal("setor", []),
		]})
		self.assertEqual(client.lookup("setor"), [])
		self.assertIn(61, clock.slept)

	def test_minimum_interval_429_is_not_retried(self):
		client, _, clock = smart({("GET", "/smartsecurities/operacao"): [
			FakeResponse(429, {"detail": "A requisição deve ter um intervalo mínimo de 10 minuto(s)"}),
		]})
		with self.assertRaises(ErpRateLimited) as ctx:
			client.request("GET", "/smartsecurities/operacao")
		self.assertEqual(ctx.exception.wait_seconds, 600)
		self.assertNotIn(600, clock.slept)

	def test_maintenance_stops(self):
		client, _, _ = smart({("GET", "/smartsecurities/titulo"): [FakeResponse(500, {"detail": "Sistema em manutenção"})]})
		with self.assertRaises(ErpMaintenance):
			list(client.titles())

	def test_create_cedente_drops_empty_keys(self):
		client, session, _ = smart({("POST", "/smartsecurities/cedente"): [FakeResponse(201, {"cedente_id": 77})]})
		result = client.create_cedente({"cpfCnpj": "43141963000118", "nomeFantasia": "", "telefones": []})
		self.assertEqual(result, {"created": True, "duplicate": False, "id": 77, "message": ""})
		self.assertEqual(session.sent[-1]["json"], {"cpfCnpj": "43141963000118"})

	def test_existing_cedente_is_a_duplicate_not_an_error(self):
		for answer in (FakeResponse(409, {"detail": "Cedente já cadastrado"}),
				FakeResponse(500, {"detail": "Já existe um cedente com este CPF/CNPJ"})):
			client, _, _ = smart({("POST", "/smartsecurities/cedente"): [answer]})
			result = client.create_cedente({"cpfCnpj": "43141963000118"})
			self.assertTrue(result["duplicate"])
			self.assertFalse(result["created"])

	def test_find_cedente_matches_masked_document(self):
		client, session, _ = smart({("GET", "/smartsecurities/cedente"): [hal("cedente", [
			{"ID_CEDENTE": "9", "CPF_CNPJ": "43.141.963/0001-18", "NOME": "CARIZON"},
		])]})
		self.assertEqual(client.find_cedente("43141963000118")["ID_CEDENTE"], "9")
		self.assertEqual(session.sent[-1]["params"]["cpfCnpjCedente"], "43141963000118")

	def test_normalize_open_and_paid_rows(self):
		opened, paid = normalize_title(OPEN_ROW), normalize_title(PAID_ROW)
		self.assertEqual(opened["erp_id"], paid["erp_id"])  # string vs int in the API
		self.assertEqual(opened["number"], paid["number"])  # DOCUMENTO vs NUMERO
		self.assertEqual(opened["amount"], Decimal("20541.16"))
		self.assertEqual(opened["outcome"], "open")
		self.assertTrue(opened["digitable_line"].isdigit())
		self.assertEqual(paid["outcome"], "paid")
		self.assertEqual(paid["paid_date"], date(2026, 11, 8))
		self.assertEqual(paid["paid_amount"], Decimal("20600.00"))
		self.assertEqual(paid["amount"], Decimal("20541.16"))

	def test_outcomes(self):
		self.assertEqual(outcome_of("Quitado em Cartório"), "paid")
		self.assertEqual(outcome_of("Recomprado c/ Desconto"), "repurchased")
		self.assertEqual(outcome_of("Repassado com Desconto c/ Baixa"), "repurchased")
		self.assertEqual(outcome_of("Aberto c/ Baixa/ Em cartório"), "open")


if __name__ == "__main__":
	unittest.main()
