"""Cliente do Serasa Experian (Relatório Avançado PJ), com sessão simulada. As respostas seguem o que a homologação
devolveu em 02/11/2026 (credtech#316): login 201 com `expiresIn` como horário, erro como `[{"code", "message"}]`."""

import base64

import pytest

from cred_hub.credit_check.clients.serasa import (
	ENVIRONMENTS,
	SerasaClient,
	SerasaDocumentNotFound,
	SerasaError,
	SerasaNotConfigured,
	token_expiry,
)

CNPJ = "11.222.333/0001-81"
FAR_FUTURE = 4102444800  # 2100-01-01


class FakeResponse:
	def __init__(self, status, body=None, text=""):
		self.status_code = status
		self._body = body
		self.text = text or ""
		self.ok = 200 <= status < 300

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class FakeSession:
	def __init__(self, login=None, reports=None):
		self.login_responses = list(login or [FakeResponse(201, {"accessToken": "tok-1", "expiresIn": FAR_FUTURE})])
		self.report_responses = list(reports or [FakeResponse(200, {"reports": [{"reportName": "X"}]})])
		self.calls = []

	def post(self, url, headers=None, timeout=None):
		self.calls.append(("POST", url, headers, None))
		return self.login_responses.pop(0)

	def get(self, url, params=None, headers=None, timeout=None):
		self.calls.append(("GET", url, headers, params))
		return self.report_responses.pop(0)


def _client(session, **kwargs):
	return SerasaClient("my-id", "my-secret", "Homologation", session=session, **kwargs)


def test_login_uses_basic_auth_and_reads_absolute_expiry():
	session = FakeSession()
	client = _client(session)
	assert client.login() == "tok-1"
	method, url, headers, _ = session.calls[0]
	assert method == "POST" and url == f"{ENVIRONMENTS['Homologation']}/security/iam/v1/client-identities/login"
	assert headers["Authorization"] == "Basic " + base64.b64encode(b"my-id:my-secret").decode()
	assert client.token_expires == FAR_FUTURE


def test_token_expiry_accepts_absolute_time_or_duration():
	assert token_expiry(FAR_FUTURE, now=1000) == FAR_FUTURE
	assert token_expiry(3600, now=1000) == 4600
	assert token_expiry(None, now=1000) == 1600


def test_fetch_report_sends_document_header_report_name_and_bearer():
	session = FakeSession()
	data = _client(session).fetch_report(CNPJ, "RELATORIO_AVANCADO_PJ_PME")
	assert data == {"reports": [{"reportName": "X"}]}
	_method, url, headers, params = session.calls[1]
	assert url == f"{ENVIRONMENTS['Homologation']}/credit-services/business-information-report/v1/reports"
	assert params == {"reportName": "RELATORIO_AVANCADO_PJ_PME"}
	assert headers["X-Document-Id"] == "11222333000181"
	assert headers["Authorization"] == "Bearer tok-1"


def test_valid_cached_token_is_reused_without_logging_in():
	session = FakeSession()
	client = _client(session, token="cached", token_expires=FAR_FUTURE)
	client.fetch_report(CNPJ)
	assert [call[0] for call in session.calls] == ["GET"]
	assert session.calls[0][2]["Authorization"] == "Bearer cached"


def test_expired_cached_token_logs_in_again():
	session = FakeSession()
	client = _client(session, token="old", token_expires=1)
	client.fetch_report(CNPJ)
	assert [call[0] for call in session.calls] == ["POST", "GET"]


def test_rejected_token_logs_in_once_and_retries():
	session = FakeSession(
		login=[FakeResponse(201, {"accessToken": "tok-2", "expiresIn": FAR_FUTURE})],
		reports=[FakeResponse(401, [{"code": "401", "message": "token"}]), FakeResponse(200, {"reports": []})],
	)
	client = _client(session, token="stale", token_expires=FAR_FUTURE)
	assert client.fetch_report(CNPJ) == {"reports": []}
	assert session.calls[-1][2]["Authorization"] == "Bearer tok-2"


def test_unknown_document_is_a_specific_error_with_the_serasa_message():
	session = FakeSession(
		reports=[FakeResponse(404, [{"code": "404", "message": "[ERROR][DOCUMENT_NOT_FOUND] Documento não encontrado."}])]
	)
	with pytest.raises(SerasaDocumentNotFound) as error:
		_client(session).fetch_report(CNPJ)
	assert "DOCUMENT_NOT_FOUND" in str(error.value)


def test_other_http_errors_carry_the_serasa_message():
	session = FakeSession(reports=[FakeResponse(412, [{"code": "412", "message": "informe o documento"}])])
	with pytest.raises(SerasaError) as error:
		_client(session).fetch_report(CNPJ)
	assert "412" in str(error.value) and "informe o documento" in str(error.value)


def test_login_refused_and_missing_credentials_fail_explicitly():
	with pytest.raises(SerasaError):
		_client(FakeSession(login=[FakeResponse(401, [{"message": "no"}])])).login()
	with pytest.raises(SerasaNotConfigured):
		SerasaClient("", "", "Production", session=FakeSession()).fetch_report(CNPJ)


def test_invalid_cnpj_is_refused_before_any_call():
	session = FakeSession()
	with pytest.raises(ValueError):
		_client(session).fetch_report("123")
	assert session.calls == []
