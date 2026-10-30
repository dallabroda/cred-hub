"""Testes unitários do motor de regras e parsers de análise de crédito (sem Frappe)."""

import pytest

from cred_hub.credit_check.clients.escavador import parse_escavador_payload
from cred_hub.credit_check.clients.serasa import (
	BureauGateBlocked,
	SerasaNotConfigured,
	assert_pre_approved_for_bureau,
	trigger_serasa_relato,
)
from cred_hub.credit_check.rules import (
	VERDICT_ERROR,
	VERDICT_PRE_APPROVED,
	VERDICT_REJECTED,
	VERDICT_WARNING,
	evaluate_credit_risk,
	format_timeline_summary,
)


def test_escavador_payload_parsing_clean():
	payload = {
		"envolvido_encontrado": {"quantidade_processos": 0},
		"items": [],
	}
	res = parse_escavador_payload(payload, "12345678000199")
	assert res["total_count"] == 0
	assert res["defendant_count"] == 0
	assert res["has_bankruptcy_recovery"] is False
	assert res["highest_value"] == 0.0


def test_escavador_payload_parsing_with_lawsuits():
	payload = {
		"envolvido_encontrado": {"quantidade_processos": 2},
		"items": [
			{
				"numero_cnj": "1001-2025",
				"titulo_polo": "BANCO XYZ X EMPRESA EXEMPLO",
				"polo": "PASSIVO",
				"assuntos": ["Execução de Título Extrajudicial"],
				"valor_causa": 45000.0,
			},
			{
				"numero_cnj": "1002-2024",
				"titulo_polo": "EMPRESA EXEMPLO X CLIENTE DEVEDOR",
				"polo": "ATIVO",
				"assuntos": ["Cobrança"],
				"valor_causa": 12000.0,
			},
		],
	}
	res = parse_escavador_payload(payload, "12345678000199")
	assert res["total_count"] == 2
	assert res["defendant_count"] == 1
	assert res["plaintiff_count"] == 1
	assert res["highest_value"] == 45000.0
	assert res["has_bankruptcy_recovery"] is False
	assert len(res["critical_lawsuits"]) == 1


def test_escavador_payload_bankruptcy_detection():
	payload = {
		"items": [
			{
				"numero_cnj": "9999-2026",
				"titulo_polo": "EMPRESA EXEMPLO",
				"polo": "PASSIVO",
				"assuntos": ["Pedido de Recuperação Judicial"],
			}
		]
	}
	res = parse_escavador_payload(payload, "12345678000199")
	assert res["has_bankruptcy_recovery"] is True


def test_evaluate_credit_risk_pre_approved():
	escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	infosimples = {
		"pgfn": {"total_debt": 0.0, "inscriptions_count": 0},
		"cnd_federal": {"is_regular": True, "status": "Negativa"},
		"cndt": {"is_regular": True, "status": "Regular"},
		"cenprot": {"has_protests": False, "protests_count": 0},
	}
	eval_res = evaluate_credit_risk(escavador, infosimples)
	assert eval_res["verdict"] == VERDICT_PRE_APPROVED
	assert len(eval_res["reasons"]) == 0


def test_evaluate_credit_risk_rejected_by_bankruptcy():
	escavador = {"total_count": 1, "defendant_count": 1, "highest_value": 50000.0, "has_bankruptcy_recovery": True}
	infosimples = {
		"pgfn": {"total_debt": 0.0},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": False},
	}
	eval_res = evaluate_credit_risk(escavador, infosimples)
	assert eval_res["verdict"] == VERDICT_REJECTED
	assert any("falência ou recuperação" in r for r in eval_res["reasons"])


def test_evaluate_credit_risk_rejected_by_pgfn_debt():
	escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	infosimples = {
		"pgfn": {"total_debt": 35000.0, "inscriptions_count": 2},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": False},
	}
	# Com tolerância zero (padrão)
	eval_res = evaluate_credit_risk(escavador, infosimples)
	assert eval_res["verdict"] == VERDICT_REJECTED
	assert any("PGFN" in r for r in eval_res["reasons"])

	# Com tolerância acima do débito
	eval_res_tolerant = evaluate_credit_risk(escavador, infosimples, {"max_pgfn_debt": 50000.0})
	assert eval_res_tolerant["verdict"] == VERDICT_WARNING


def test_evaluate_credit_risk_rejected_by_protests():
	escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	infosimples = {
		"pgfn": {"total_debt": 0.0},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": True, "protests_count": 3},
	}
	eval_res = evaluate_credit_risk(escavador, infosimples)
	assert eval_res["verdict"] == VERDICT_REJECTED
	assert any("protestado" in r for r in eval_res["reasons"])


def test_evaluate_credit_risk_rejected_by_fgts_irregular():
	clean_escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	infosimples_with_fgts = {
		"pgfn": {"total_debt": 0.0},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": False},
		"fgts": {"is_regular": False, "status": "Irregular"},
	}
	eval_res = evaluate_credit_risk(clean_escavador, infosimples_with_fgts)
	assert eval_res["verdict"] == VERDICT_REJECTED
	assert any("FGTS" in r for r in eval_res["reasons"])

	# Com block_on_fgts_irregular desligado, vira só aviso
	eval_res_warning = evaluate_credit_risk(clean_escavador, infosimples_with_fgts, {"block_on_fgts_irregular": False})
	assert eval_res_warning["verdict"] == VERDICT_WARNING


def test_evaluate_credit_risk_directd_score_rejects_below_floor():
	clean_escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	clean_infosimples = {
		"pgfn": {"total_debt": 0.0},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": False},
	}
	directd_score_data = {"score": 350, "faixa_score": "Alto risco"}

	# Sem piso configurado (0): não reprova por score sozinho
	eval_res_no_floor = evaluate_credit_risk(clean_escavador, clean_infosimples, directd_score_data=directd_score_data)
	assert eval_res_no_floor["verdict"] == VERDICT_PRE_APPROVED

	# Com piso 600: reprova
	eval_res_floor = evaluate_credit_risk(
		clean_escavador, clean_infosimples, {"min_directd_score": 600}, directd_score_data=directd_score_data
	)
	assert eval_res_floor["verdict"] == VERDICT_REJECTED
	assert any("Score de crédito" in r for r in eval_res_floor["reasons"])


def test_evaluate_credit_risk_never_fails_open_on_provider_failure():
	"""Mesmo com dados 'limpos', uma falha de provedor nunca pode virar Pre-Approved (credtech#302)."""
	clean_escavador = {"total_count": 0, "defendant_count": 0, "highest_value": 0.0, "has_bankruptcy_recovery": False}
	clean_infosimples = {
		"pgfn": {"total_debt": 0.0, "inscriptions_count": 0},
		"cndt": {"is_regular": True},
		"cenprot": {"has_protests": False},
	}
	eval_res = evaluate_credit_risk(
		clean_escavador,
		clean_infosimples,
		provider_failures=["Escavador: query failed (timeout)"],
	)
	assert eval_res["verdict"] == VERDICT_ERROR
	assert eval_res["verdict"] != VERDICT_PRE_APPROVED


def test_format_timeline_summary_for_error_verdict():
	evaluation = {
		"verdict": VERDICT_ERROR,
		"reasons": ["Falha na consulta: Escavador: query failed (timeout)"],
		"warnings": [],
		"metrics": {},
	}
	text = format_timeline_summary(evaluation, "12.345.678/0001-99")
	assert "Não verificado" in text
	assert "Aprovado" not in text


def test_trigger_serasa_relato_requires_real_credentials():
	"""Sem endpoint/token do contrato do cliente, nunca finge sucesso (credtech#302)."""
	with pytest.raises(SerasaNotConfigured):
		trigger_serasa_relato("12345678000199", "user@example.com", "Pre-Approved")


def test_bureau_gate_blocking():
	with pytest.raises(BureauGateBlocked):
		assert_pre_approved_for_bureau("Rejected", "12345678000199")

	with pytest.raises(BureauGateBlocked):
		assert_pre_approved_for_bureau("Warning", "12345678000199")

	# Pre-Approved passa sem erro
	assert_pre_approved_for_bureau("Pre-Approved", "12345678000199")


def test_format_timeline_summary():
	evaluation = {
		"verdict": VERDICT_PRE_APPROVED,
		"reasons": [],
		"warnings": [],
		"metrics": {
			"lawsuits_total": 0,
			"lawsuits_defendant": 0,
			"has_bankruptcy": False,
			"pgfn_debt": 0.0,
			"cndt_status": "Regular",
			"cenprot_has_protest": False,
		},
	}
	text = format_timeline_summary(evaluation, "12.345.678/0001-99")
	assert "Aprovado no Pré-Filtro" in text
	assert "CNPJ 12.345.678/0001-99" in text
