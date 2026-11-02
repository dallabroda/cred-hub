"""Captura do retorno inteiro dos provedores e nomes em português dos campos (credtech#316). Sem Frappe."""

from unittest.mock import MagicMock, patch

from cred_hub.credit_check.clients.directd import fetch_score
from cred_hub.credit_check.labels import KEY_LABELS, humanize, label_for
from cred_hub.credit_check.raw_capture import capture, record, scrub


def _response(payload):
	response = MagicMock()
	response.status_code = 200
	response.ok = True
	response.json.return_value = payload
	return response


def test_record_outside_capture_does_nothing():
	record("DirectD", "Score", {"a": 1})  # não deve falhar nem guardar em lugar nenhum
	with capture() as calls:
		pass
	assert calls == []


def test_capture_keeps_the_whole_return_not_only_the_parsed_summary():
	full = {
		"metaDados": {"resultado": "Sucesso"},
		"retorno": {
			"pessoaJuridica": {"score": 740, "faixaScore": "Baixo risco", "motivos": ["a"], "indicadoresNegocio": []},
			"campoQueOResumoDescarta": {"x": [1, 2, 3]},
		},
	}
	with patch("cred_hub.credit_check.clients.directd.requests.get", return_value=_response(full)), capture() as calls:
		summary = fetch_score("11.222.333/0001-81", "token-do-cliente")

	assert summary["score"] == 740  # o resumo que as regras usam continua igual
	assert len(calls) == 1
	assert calls[0]["provider"] == "DirectD" and calls[0]["endpoint"] == "Score"
	assert calls[0]["payload"] == full  # e o retorno inteiro foi guardado


def test_credentials_echoed_by_a_provider_are_never_stored():
	payload = {"header": {"token": "segredo", "Authorization": "Bearer x"}, "items": [{"password": "p", "ok": 1}]}
	with capture() as calls:
		record("Infosimples", "pgfn", payload)
	stored = calls[0]["payload"]
	assert stored["header"] == {"token": "***", "Authorization": "***"}
	assert stored["items"] == [{"password": "***", "ok": 1}]
	assert payload["header"]["token"] == "segredo"  # o original não é alterado
	assert scrub({"token": ""}) == {"token": ""}  # vazio continua vazio


def test_known_serasa_and_directd_fields_have_portuguese_names():
	assert label_for("documentNumber") == ("CNPJ", True)
	assert label_for("inquiryCompanyResponse") == ("Consultas de outras empresas", True)
	assert label_for("quantidadeInstituicoes")[0] == "Quantidade instituições"
	assert label_for("carteiraCredito") == ("Carteira de crédito", True)


def test_unknown_fields_are_humanized_and_flagged_as_untranslated():
	label, translated = label_for("quantidadeContratosAtivos")
	assert (label, translated) == ("Quantidade contratos ativos", False)
	assert humanize("valor_total_consolidado") == "Valor total consolidado"
	assert humanize("situacaoCadastral") == "Situação cadastral"


def test_label_table_keys_are_lowercase():
	assert all(key == key.lower() for key in KEY_LABELS)
