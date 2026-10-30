"""Testes unitários dos clientes HTTP do Escavador e Infosimples com mocks."""

from unittest.mock import MagicMock, patch
import pytest

from cred_hub.credit_check.clients.directd import (
	DirectDAuthError,
	fetch_boa_vista_risco_pj,
	fetch_dossie_credito_completo,
	fetch_fgts_regularidade,
	fetch_protests_nacional,
	fetch_scr_bacen,
	fetch_score,
	fetch_sintegra_ccc,
	merge_lawsuit_sources,
	merge_protest_sources,
	parse_directd_processos_payload,
)
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS
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


def test_directd_score_missing_token():
	with pytest.raises(DirectDAuthError):
		fetch_score("12345678000199", "")


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_score_success(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"retorno": {"pessoaJuridica": {"score": 820, "faixaScore": "Baixo risco", "motivos": []}}
	}
	mock_get.return_value = mock_resp

	res = fetch_score("12345678000199", "token")
	assert res["score"] == 820
	assert res["faixa_score"] == "Baixo risco"


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_protestos_nacional_success(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"retorno": {
			"constamProtestos": True,
			"numeroTotalProtestos": 2,
			"valorTotalProtestos": "1.500,00",
		}
	}
	mock_get.return_value = mock_resp

	res = fetch_protests_nacional("12345678000199", "token")
	assert res["has_protests"] is True
	assert res["protests_count"] == 2
	assert res["total_value"] == 1500.0


def test_directd_processos_payload_parsing():
	payload = {
		"retorno": {
			"totalProcessos": 1,
			"processos": [
				{
					"numeroProcesso": "0012345-67.2024.8.26.0100",
					"classe": "Execução Fiscal",
					"assuntoPrincipal": "Cobrança",
					"status": "Em andamento",
					"valorAcao": 80000.0,
					"partes": [{"polo": "PASSIVO", "documento": "12345678000199"}],
				}
			],
		}
	}
	res = parse_directd_processos_payload(payload, "12345678000199")
	assert res["total_count"] == 1
	assert res["defendant_count"] == 1
	assert res["highest_value"] == 80000.0
	assert len(res["critical_lawsuits"]) == 1


def test_merge_lawsuit_sources_is_conservative():
	a = {"total_count": 1, "defendant_count": 1, "highest_value": 5000.0, "has_bankruptcy_recovery": False, "critical_lawsuits": []}
	b = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": True, "critical_lawsuits": []}
	merged = merge_lawsuit_sources(a, b)
	assert merged["defendant_count"] == 1
	assert merged["has_bankruptcy_recovery"] is True


def test_merge_protest_sources_is_conservative():
	a = {"has_protests": False, "protests_count": 0}
	b = {"has_protests": True, "protests_count": 3}
	merged = merge_protest_sources(a, b)
	assert merged["has_protests"] is True
	assert merged["protests_count"] == 3


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_fgts_irregular(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {"retorno": {"flagIrregularidade": True, "status": "Irregular"}}
	mock_get.return_value = mock_resp

	res = fetch_fgts_regularidade("12345678000199", "token")
	assert res["is_regular"] is False
	assert res["status"] == "Irregular"


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_sintegra_ccc(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {"retorno": {"situacaoCadastral": "Ativa", "listaInscricoesEstaduaisSintegra": ["123456"]}}
	mock_get.return_value = mock_resp

	res = fetch_sintegra_ccc("12345678000199", "token")
	assert res["situacao_cadastral"] == "Ativa"
	assert res["inscricoes_estaduais"] == ["123456"]


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_boa_vista_risco_pj_success(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"retorno": {
			"score": {"score": "320", "classificacaoABC": "C", "risco": "Alto"},
			"restricoes": {
				"protestos": {"quantidade": "2", "valorTotal": "1500"},
				"falenciaRecuperacaoJudicial": {"quantidade": "0"},
			},
		}
	}
	mock_get.return_value = mock_resp

	res = fetch_boa_vista_risco_pj("12345678000199", "token")
	assert res["score"] == 320.0
	assert res["has_protests"] is True
	assert res["protests_count"] == 2
	assert res["has_bankruptcy_recovery"] is False


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_dossie_credito_completo_accepts_cpf_or_cnpj(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"retorno": {
			"entidadeJuridica": {
				"scoreEntidades": {"entidadeJuridica": {"score": 700, "motivos": []}},
				"pendenciaFinanceira": {"status": "Regular", "protestos": [], "acoesJudiciais": []},
			}
		}
	}
	mock_get.return_value = mock_resp

	res = fetch_dossie_credito_completo("12345678000199", "token")
	assert res["score"] == 700.0
	assert res["has_protests"] is False

	with pytest.raises(ValueError):
		fetch_dossie_credito_completo("123", "token")


@patch("cred_hub.credit_check.clients.directd.requests.get")
def test_directd_scr_bacen_success(mock_get):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = {
		"retorno": {
			"quantidadeInstituicoes": "3",
			"quantidadeOperacoes": "5",
			"situacao": "Normal",
			"carteiraCredito": {"valorVencer": "10000,00", "valorVencida": "0,00"},
		}
	}
	mock_get.return_value = mock_resp

	res = fetch_scr_bacen("12345678000199", "token")
	assert res["quantidade_instituicoes"] == 3
	assert res["valor_a_vencer"] == 10000.0


def test_premium_bureaus_registry_has_cost_and_client_price():
	for key in ("boavista_risco", "dossie_quod", "scr_bacen"):
		bureau = PREMIUM_BUREAUS[key]
		assert bureau.cost > 0
		assert bureau.client_price > bureau.cost  # sempre vendido com margem
