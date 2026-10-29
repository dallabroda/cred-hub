"""Testes unitários dos clientes HTTP do Escavador e Infosimples com mocks."""

from unittest.mock import MagicMock, patch
import pytest

from cred_hub.credit_check.clients.escavador import (
	EscavadorAuthError,
	EscavadorRateLimitError,
	fetch_lawsuits,
)
from cred_hub.credit_check.clients.infosimples import (
	InfosimplesAuthError,
	fetch_fiscal_and_protests,
	query_cnd_federal,
	query_cndt,
	query_pgfn,
)


def test_escavador_invalid_cnpj():
	with pytest.raises(ValueError):
		fetch_lawsuits("123", "token")


def test_escavador_missing_token():
	with pytest.raises(EscavadorAuthError):
		fetch_lawsuits("12345678000199", "")


@patch("cred_hub.credit_check.clients.escavador.requests.get")
def test_escavador_auth_error(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 401
	mock_get.return_value = mock_resp

	with pytest.raises(EscavadorAuthError):
		fetch_lawsuits("12345678000199", "bad-token")


@patch("cred_hub.credit_check.clients.escavador.requests.get")
def test_escavador_rate_limit(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 429
	mock_get.return_value = mock_resp

	with pytest.raises(EscavadorRateLimitError):
		fetch_lawsuits("12345678000199", "token")


@patch("cred_hub.credit_check.clients.escavador.requests.get")
def test_escavador_404_empty(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 404
	mock_get.return_value = mock_resp

	res = fetch_lawsuits("12345678000199", "token")
	assert res["total_count"] == 0
	assert res["defendant_count"] == 0


@patch("cred_hub.credit_check.clients.escavador.requests.get")
def test_escavador_success(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"envolvido_encontrado": {"quantidade_processos": 1},
		"items": [
			{
				"numero_cnj": "5001234-88.2024.4.04.7200",
				"titulo_polo": "FAZENDA NACIONAL X EMPRESA EXEMPLO",
				"polo": "PASSIVO",
				"assuntos": ["Execução Fiscal"],
				"valor_causa": 80000.0,
			}
		],
	}
	mock_get.return_value = mock_resp

	res = fetch_lawsuits("12345678000199", "valid-token")
	assert res["total_count"] == 1
	assert res["defendant_count"] == 1
	assert res["highest_value"] == 80000.0
	assert len(res["critical_lawsuits"]) == 1


@patch("cred_hub.credit_check.clients.infosimples.requests.post")
def test_infosimples_pgfn_query(mock_post):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"code": 200,
		"data": [
			{
				"valor_total_consolidado": 15400.50,
				"inscricoes": [{"numero_inscricao": "123", "valor_consolidado": 15400.50, "situacao": "ATIVA"}],
			}
		],
	}
	mock_post.return_value = mock_resp

	res = query_pgfn("12345678000199", "token")
	assert res["status"] == "Inscrito na Dívida Ativa"
	assert res["total_debt"] == 15400.50
	assert res["inscriptions_count"] == 1


@patch("cred_hub.credit_check.clients.infosimples.requests.post")
def test_infosimples_cnd_federal_query(mock_post):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"code": 200,
		"data": [{"tipo_certidao": "NEGATIVA", "pdf_url": "https://gov.br/cnd.pdf"}],
	}
	mock_post.return_value = mock_resp

	res = query_cnd_federal("12345678000199", "token")
	assert res["is_regular"] is True
	assert res["pdf_url"] == "https://gov.br/cnd.pdf"


@patch("cred_hub.credit_check.clients.infosimples.requests.post")
def test_infosimples_cndt_query(mock_post):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"code": 200,
		"data": [{"consta_bndt": False, "pdf_url": "https://tst.jus.br/cndt.pdf"}],
	}
	mock_post.return_value = mock_resp

	res = query_cndt("12345678000199", "token")
	assert res["is_regular"] is True
	assert res["status"] == "Regular"


@patch("cred_hub.credit_check.clients.infosimples.query_pgfn")
@patch("cred_hub.credit_check.clients.infosimples.query_cnd_federal")
@patch("cred_hub.credit_check.clients.infosimples.query_cndt")
@patch("cred_hub.credit_check.clients.infosimples.query_cenprot")
def test_fetch_fiscal_and_protests_aggregate(mock_cenprot, mock_cndt, mock_cnd, mock_pgfn):
	mock_pgfn.return_value = {"total_debt": 0.0, "status": "Regular", "inscriptions_count": 0}
	mock_cnd.return_value = {"is_regular": True, "status": "Negativa"}
	mock_cndt.return_value = {"is_regular": True, "status": "Regular"}
	mock_cenprot.return_value = {"has_protests": False, "protests_count": 0}

	res = fetch_fiscal_and_protests("12345678000199", "token")
	assert res["cnpj"] == "12345678000199"
	assert res["pgfn"]["total_debt"] == 0.0
	assert len(res["errors"]) == 0
