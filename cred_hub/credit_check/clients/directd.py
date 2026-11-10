"""Cliente HTTP para a Direct Data (DirectD) API v3 — produtos de Crédito e Processos usados
nesta esteira (https://directd.com.br/central-de-ajuda/apis/introducao).

Escopo desta integração: só produtos das categorias "Crédito" e "Processos" do catálogo DirectD
(confirmados na doc pública ou colados pelo dono, não as ~140 APIs restantes do catálogo):

Automáticos (entram no pré-filtro, "ProviderCheck" em enrich.py):
1. Score de Crédito - QUOD — GET /api/Score — score Quod PF/PJ, R$ 1,98/consulta.
2. Protestos Nacional - IEPTB Básica — GET /api/ProtestosBasica — protestos em cartório em
   TODO o Brasil (a Infosimples/CENPROT já integrada só cobre SP), R$ 2,95/consulta.
3. Processos Judiciais - Simplificada — GET /api/ProcessosJudiciaisSimplificada — processos em
   tribunais estaduais, federais e trabalhistas, R$ 1,50/consulta. Resultado convertido pro mesmo
   formato do cliente do Escavador (clients/escavador.py) pra reaproveitar as mesmas regras de
   falência/execução/réu em rules.py sem duplicar lógica.

Sob demanda / Premium (bureaus pagos, gate de pré-aprovação — ver credit_check/premium_bureaus.py):
4. Boa Vista - Risco Positivo PJ — GET /api/BoaVistaRiscoPositivoPJ — R$ 14,03/consulta.
5. Dossiê QUOD - Completo — GET /api/DossieCreditoCompleto — PF ou PJ, R$ 6,99/consulta.
6. SCR Analítico - Resumo BACEN — GET /api/SCRBacen — PF ou PJ, R$ 3,90/consulta.
"""

from __future__ import annotations

from typing import Any

import requests

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.clients.escavador import RE_BANKRUPTCY, RE_EXECUTION
from cred_hub.credit_check.raw_capture import record

DEFAULT_BASE_URL = "https://apiv3.directd.com.br"
DEFAULT_TIMEOUT = 20


class DirectDError(Exception):
	"""Erro base na comunicação com a DirectD."""


class DirectDAuthError(DirectDError):
	"""Token de autenticação inválido (401)."""


def _request_directd(endpoint: str, params: dict[str, str], token: str, base_url: str, timeout: int) -> dict[str, Any]:
	"""Chamada HTTP genérica a um endpoint DirectD, com os parâmetros de consulta já montados
	(CNPJ, CPF, ou ambos, conforme o produto)."""
	if not token or not token.strip():
		raise DirectDAuthError("Token da DirectD não informado.")

	url = f"{base_url.rstrip('/')}/api/{endpoint.lstrip('/')}"
	headers = {
		"Authorization": f"Bearer {token.strip()}",
		"Accept": "application/json",
	}

	try:
		response = requests.get(url, headers=headers, params=params, timeout=timeout)
	except requests.RequestException as e:
		raise DirectDError(f"Falha de rede ao consultar {endpoint} na DirectD: {e}") from e

	if response.status_code in (401, 403):
		raise DirectDAuthError(f"Token da DirectD não autorizado para {endpoint}.")
	if not response.ok:
		raise DirectDError(f"Erro {response.status_code} na DirectD ({endpoint}): {response.text[:300]}")

	try:
		data = response.json()
	except ValueError as e:
		raise DirectDError(f"Resposta não-JSON da DirectD ({endpoint}): {response.text[:200]}") from e

	record("DirectD", endpoint, data)
	return data


def _get_directd(endpoint: str, digits: str, token: str, base_url: str, timeout: int) -> dict[str, Any]:
	"""Atalho pros produtos que só aceitam CNPJ (uso interno dos 3 provedores automáticos)."""
	return _request_directd(endpoint, {"CNPJ": digits}, token, base_url, timeout)


def _cpf_ou_cnpj_params(documento: str) -> dict[str, str]:
	"""Monta os parâmetros de consulta pros produtos que aceitam CPF ou CNPJ (bureaus Premium)."""
	digits = only_digits(documento)
	if len(digits) == 14:
		return {"CNPJ": digits}
	if len(digits) == 11:
		return {"CPF": digits}
	raise ValueError(f"Documento inválido (precisa ser CPF ou CNPJ): {documento}")


def fetch_score(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta o Score de Crédito (Quod, via DirectD) da pessoa jurídica."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de score: {cnpj}")

	data = _get_directd("Score", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	pj = retorno.get("pessoaJuridica") or {}

	return {
		"score": pj.get("score"),
		"faixa_score": pj.get("faixaScore") or "",
		"motivos": pj.get("motivos") or [],
		"indicadores_negocio": pj.get("indicadoresNegocio") or [],
	}


def _to_float(value: Any) -> float:
	"""Converte valores monetários que a DirectD às vezes devolve como string formatada."""
	if isinstance(value, (int, float)):
		return float(value)
	if isinstance(value, str):
		cleaned = value.replace("R$", "").strip()
		if "," in cleaned and "." in cleaned:
			cleaned = cleaned.replace(".", "").replace(",", ".")
		elif "," in cleaned:
			cleaned = cleaned.replace(",", ".")
		try:
			return float(cleaned)
		except ValueError:
			return 0.0
	return 0.0


def fetch_protests_nacional(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta Protestos Nacional - IEPTB Básica (cobertura nacional, ao contrário do CENPROT-SP
	já integrado via Infosimples)."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de protestos: {cnpj}")

	data = _get_directd("ProtestosBasica", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}

	return {
		"has_protests": bool(retorno.get("constamProtestos")),
		"protests_count": int(retorno.get("numeroTotalProtestos") or 0),
		"total_value": _to_float(retorno.get("valorTotalProtestos")),
		"cartorios_distintos": int(retorno.get("quantidadeCartoriosDistintos") or 0),
		"cidades_distintas": int(retorno.get("quantidadeCidadesDistintas") or 0),
	}


def merge_protest_sources(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
	"""Combina dois sinais de protesto (ex.: CENPROT-SP via Infosimples + Protestos Nacional via
	DirectD) da forma mais conservadora: OU lógico e maior contagem, para nunca perder um protesto
	que só uma das fontes tenha encontrado."""
	return {
		"has_protests": bool(a.get("has_protests")) or bool(b.get("has_protests")),
		"protests_count": max(int(a.get("protests_count") or 0), int(b.get("protests_count") or 0)),
	}


def parse_directd_processos_payload(payload: dict[str, Any], digits: str) -> dict[str, Any]:
	"""Converte o retorno de ProcessosJudiciaisSimplificada ou ProcessosJudiciaisCompleta para o
	mesmo formato usado pelo cliente do Escavador (total_count, defendant_count, highest_value,
	has_bankruptcy_recovery, critical_lawsuits), separando Polo Passivo confirmado de Polo Ativo
	e de vínculos fracos por homônimo (`origemDocumento == 'AGREGADO'`)."""
	retorno = payload.get("retorno") or {}
	processos = retorno.get("processos") or []

	defendant_count = 0
	plaintiff_count = 0
	other_count = 0
	homonym_suspect_count = 0
	penhoras_count = 0
	has_bankruptcy = False
	has_penhoras = False
	highest_value = 0.0
	defendant_total_value = 0.0
	critical_lawsuits: list[dict[str, Any]] = []

	for item in processos:
		numero = item.get("numeroProcesso") or ""
		classificacao = item.get("classificacao")
		classe = item.get("classe") or (classificacao.get("descricao") if isinstance(classificacao, dict) else "") or ""
		assuntos_list = item.get("assuntos") or []
		assuntos_str = ", ".join(
			(a.get("assunto") or "") for a in assuntos_list if isinstance(a, dict) and a.get("assunto")
		)
		assunto = item.get("assuntoPrincipal") or assuntos_str or ""
		area_direito = item.get("areaDireito") or ""
		full_text = f"{area_direito} {classe} {assunto}".strip()

		detalhes_status = item.get("detalhesStatusProcesso") or {}
		status = item.get("status") or detalhes_status.get("statusDetalhes") or ""
		orgao = item.get("orgaoJulgador") or {}
		tribunal = item.get("tribunal") or (orgao.get("tribunal") if isinstance(orgao, dict) else "") or ""
		valor_raw = item.get("valorProcesso") if item.get("valorProcesso") is not None else item.get("valorAcao")
		valor = _to_float(valor_raw)

		penhoras = detalhes_status.get("penhoras") or []
		if penhoras:
			has_penhoras = True
			penhoras_count += len(penhoras)

		partes = item.get("partes") or []
		matched_parties = [
			p for p in partes if isinstance(p, dict) and only_digits(str(p.get("documento") or "")) == digits
		]
		is_homonym_suspect = bool(
			matched_parties
			and all((p.get("origemDocumento") or "").upper() == "AGREGADO" for p in matched_parties)
		)

		is_defendant = any(
			(p.get("polo") or p.get("posicaoProcessual") or "").upper()
			in ("PASSIVO", "REU", "RÉU", "EXECUTADO", "RECLAMADO", "EXECUTADA", "RECLAMADA")
			for p in matched_parties
		)
		is_plaintiff = any(
			(p.get("polo") or p.get("posicaoProcessual") or "").upper()
			in ("ATIVO", "AUTOR", "AUTORA", "EXEQUENTE", "RECLAMANTE")
			for p in matched_parties
		)
		if not partes:
			is_defendant = False
			is_plaintiff = False

		if is_homonym_suspect:
			homonym_suspect_count += 1
			other_count += 1
		elif is_defendant:
			defendant_count += 1
			defendant_total_value += valor
			if valor > highest_value:
				highest_value = valor
		elif is_plaintiff:
			plaintiff_count += 1
		else:
			other_count += 1
			if not partes and valor > highest_value:
				highest_value = valor

		is_bankruptcy = bool(RE_BANKRUPTCY.search(full_text)) and not is_homonym_suspect and not is_plaintiff
		if is_bankruptcy:
			has_bankruptcy = True
		is_execution = bool(RE_EXECUTION.search(full_text))

		if (
			is_bankruptcy
			or (is_defendant and not is_homonym_suspect and (is_execution or valor >= 10000.0 or bool(penhoras)))
		):
			critical_lawsuits.append({
				"numero_cnj": numero,
				"titulo": assunto or classe,
				"polo": "PASSIVO" if is_defendant else "OUTRO",
				"tribunal": tribunal,
				"area_direito": area_direito,
				"assunto": assunto,
				"is_bankruptcy": is_bankruptcy,
				"is_execution": is_execution,
				"has_penhora": bool(penhoras),
				"penhoras_count": len(penhoras),
				"valor": valor,
				"status": status,
			})

	return {
		"cnpj": digits,
		"total_count": retorno.get("totalProcessos") or len(processos),
		"defendant_count": defendant_count,
		"defendant_total_value": defendant_total_value,
		"plaintiff_count": plaintiff_count,
		"other_count": other_count,
		"homonym_suspect_count": homonym_suspect_count,
		"highest_value": highest_value,
		"has_bankruptcy_recovery": has_bankruptcy,
		"has_penhoras": has_penhoras,
		"penhoras_count": penhoras_count,
		"critical_lawsuits": critical_lawsuits,
		"items_analyzed": len(processos),
		"raw_envolvido": {},
	}


def fetch_lawsuits_directd(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta processos judiciais (Processos Judiciais Simplificada) via DirectD."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de processos: {cnpj}")

	data = _get_directd("ProcessosJudiciaisSimplificada", digits, token, base_url, timeout)
	return parse_directd_processos_payload(data, digits)


def fetch_processos_completa(documento: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Processos Judiciais - Completa (R$ 3,30/consulta, PF ou PJ). Inclui órgão julgador,
	classificação, assuntos, origemDocumento (TRIBUNAL vs AGREGADO), julgamentos e penhoras."""
	params = _cpf_ou_cnpj_params(documento)
	digits = only_digits(documento)

	data = _request_directd("ProcessosJudiciaisCompleta", params, token, base_url, timeout)
	parsed = parse_directd_processos_payload(data, digits)
	parsed["raw"] = data.get("retorno") or {}
	return parsed


def fetch_fgts_regularidade(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""FGTS - Regularidade do Empregador (automático, R$ 0,36/consulta)."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta de regularidade FGTS: {cnpj}")

	data = _get_directd("CaixaRegularidadeEmpregadorFGTS", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	status_str = str(retorno.get("status") or "")
	irregular = bool(
		retorno.get("possuiIrregularidade")
		or retorno.get("flagIrregularidade")
		or retorno.get("irregular")
		or ("IRREGULAR" in status_str.upper())
	)

	return {
		"is_regular": not irregular,
		"status": status_str or ("Irregular" if irregular else "Regular"),
		"certificado_numero": retorno.get("numeroCertificado") or retorno.get("numeroCRF") or "",
		"periodo_validade": retorno.get("periodoValidade") or {},
		"raw": retorno,
	}


def fetch_sintegra_ccc(
	cnpj: str,
	token: str,
	uf: str = "",
	*args,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	**kwargs,
) -> dict[str, Any]:
	"""Sintegra/CCC - Cadastro Centralizado de Contribuinte (R$ 0,36/consulta quando UF informada via
	/api/SintegraCCC; fallback para SintegraCCC-Completa caso UF não seja passada)."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta Sintegra/CCC: {cnpj}")

	clean_uf = (uf or "").strip().upper()
	if len(clean_uf) == 2:
		data = _request_directd("SintegraCCC", {"CNPJ": digits, "Uf": clean_uf}, token, base_url, timeout)
	else:
		data = _get_directd("SintegraCCC-Completa", digits, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	inscricoes = retorno.get("inscricoesEstaduais") or retorno.get("listaInscricoesEstaduaisSintegra") or []

	return {
		"situacao_cadastral": (
			retorno.get("situacaoCadastral")
			or retorno.get("situacaoContribuinteNaData")
			or retorno.get("situacaoCNPJ")
			or retorno.get("situacaoCnpj")
			or ""
		),
		"situacao_cnpj": (
			retorno.get("situacaoCNPJ")
			or retorno.get("situacaoCnpj")
			or retorno.get("situacaoDoCnpj")
			or ""
		),
		"regime_apuracao": retorno.get("regimeApuracao") or "",
		"inscricoes_estaduais": inscricoes,
		"raw": retorno,
	}


def fetch_boa_vista_risco_pj(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Boa Vista - Risco Positivo PJ (bureau pago sob demanda, R$ 14,03/consulta).

	Segundo bureau de score independente do Quod — usado no "Premium" (ver premium_bureaus.py),
	nunca automático. `raw` guarda o payload inteiro (empresa, decisões, indicadores, governanças,
	alertas, histórico) pro dossiê mostrar tudo, já que a doc pública não documenta 1:1 todo campo.
	"""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta Boa Vista Risco Positivo PJ: {cnpj}")

	data = _request_directd("BoaVistaRiscoPositivoPJ", {"CNPJ": digits}, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	score = retorno.get("score") or {}
	restricoes = retorno.get("restricoes") or {}
	pendencias = restricoes.get("pendenciasFinanceiras") or {}
	protestos = restricoes.get("protestos") or {}
	acoes = restricoes.get("acoesCiveis") or {}
	falencia = restricoes.get("falenciaRecuperacaoJudicial") or {}
	cheques_bacen = restricoes.get("chequesSemFundoBacen") or {}
	cheques_varejo = restricoes.get("chequesSemFundoVarejo") or {}
	limite = restricoes.get("limiteCredito") or {}
	faturamento = restricoes.get("faturamentoPresumido") or {}

	return {
		"score": _to_float(score.get("score")) or None,
		"classificacao_abc": score.get("classificacaoABC") or "",
		"classificacao_numero": score.get("classificacaoNumero") or "",
		"probabilidade_inadimplencia": score.get("probabilidadeInadimplencia") or "",
		"risco": score.get("risco") or "",
		"has_financial_pendencies": _to_float(pendencias.get("quantidade")) > 0,
		"financial_pendencies_count": int(_to_float(pendencias.get("quantidade"))),
		"financial_pendencies_value": _to_float(pendencias.get("valorTotal")),
		"has_protests": _to_float(protestos.get("quantidade")) > 0,
		"protests_count": int(_to_float(protestos.get("quantidade"))),
		"protests_value": _to_float(protestos.get("valorTotal")),
		"has_bad_checks": _to_float(cheques_bacen.get("quantidade")) > 0 or _to_float(cheques_varejo.get("quantidade")) > 0,
		"has_civil_lawsuits": _to_float(acoes.get("quantidade")) > 0,
		"civil_lawsuits_count": int(_to_float(acoes.get("quantidade"))),
		"has_bankruptcy_recovery": _to_float(falencia.get("quantidade")) > 0,
		"credit_limit_suggested": limite.get("valor") or "",
		"estimated_revenue_range": faturamento.get("faixa") or "",
		"alertas": retorno.get("alertas") or [],
		"raw": retorno,
	}


def fetch_boa_vista_acerta_pf(
	cpf: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
	"""Boa Vista - Acerta Completo Positivo PF (bureau pago sob demanda para sócios/PF, R$ 14,03/consulta)."""
	digits = only_digits(cpf)
	if not digits or len(digits) != 11:
		raise ValueError(f"CPF inválido para consulta Boa Vista Acerta Completo Positivo PF: {cpf}")

	data = _request_directd("BoaVistaAcertaCompletoPositivoPF", {"CPF": digits}, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	ident = retorno.get("identificacaoBasica") or {}

	scores_list = (retorno.get("scores") or {}).get("ocorrencias") or []
	score_block = (scores_list[0] if scores_list and isinstance(scores_list[0], dict) else None) or retorno.get("score") or {}

	debitos_resumo = (retorno.get("debitos") or {}).get("resumo") or {}
	pendencias = retorno.get("pendenciasFinanceiras") or debitos_resumo or {}
	protestos = retorno.get("protestos") or {}
	cheque_bacen = retorno.get("chequeSemFundoBacen") or retorno.get("chequesSemFundoBacen") or {}
	cheque_varejo = retorno.get("chequeSemFundoVarejo") or retorno.get("chequesSemFundoVarejo") or {}
	acoes = retorno.get("acoesCiveis") or {}
	falencias = retorno.get("falenciasAcoesRecuperacoes") or retorno.get("falenciasRecuperacoesJudiciais") or {}
	renda_presumida = retorno.get("rendaPresumida") or {}
	participacoes = (
		(retorno.get("participacaoEmEmpresas") or {}).get("ocorrencias")
		or retorno.get("participacoesSocietarias")
		or []
	)

	pend_qty = int(_to_float(pendencias.get("quantidadeOcorrencia") or pendencias.get("quantidade")))
	prot_qty = int(_to_float(protestos.get("quantidadeOcorrencia") or protestos.get("quantidade")))
	acoes_qty = int(_to_float(acoes.get("quantidadeOcorrencia") or acoes.get("quantidade")))
	fal_qty = int(_to_float(falencias.get("quantidadeOcorrencia") or falencias.get("quantidade")))
	chq_b_qty = int(_to_float(cheque_bacen.get("quantidadeOcorrencia") or cheque_bacen.get("quantidade")))
	chq_v_qty = int(_to_float(cheque_varejo.get("quantidadeOcorrencia") or cheque_varejo.get("quantidade")))

	return {
		"cpf": retorno.get("cpf") or ident.get("cpf") or digits,
		"nome": retorno.get("nome") or ident.get("nome") or "",
		"cpf_situacao": retorno.get("cpfSituacao") or ident.get("situacaoCPF") or "",
		"indicacao_obito": retorno.get("indicacaoObito") or ident.get("possuiObito") or "",
		"score": _to_float(score_block.get("score")) or None,
		"classificacao_abc": score_block.get("classificacaoABC") or "",
		"probabilidade_inadimplencia": score_block.get("probabilidadeInadimplencia") or "",
		"risco": score_block.get("risco") or "",
		"faixa_renda": retorno.get("faixaRenda") or renda_presumida.get("faixa") or "",
		"renda_presumida": renda_presumida.get("valorPresumido") or renda_presumida.get("rendaAnual") or "",
		"classe_social": retorno.get("classeSocial") or "",
		"poder_aquisitivo": retorno.get("poderAquisitivo") or "",
		"has_financial_pendencies": pend_qty > 0,
		"financial_pendencies_count": pend_qty,
		"financial_pendencies_value": _to_float(pendencias.get("valorTotal")),
		"has_protests": prot_qty > 0,
		"protests_count": prot_qty,
		"protests_value": _to_float(protestos.get("valorTotal")),
		"has_bad_checks": (chq_b_qty + chq_v_qty) > 0,
		"has_civil_lawsuits": acoes_qty > 0,
		"civil_lawsuits_count": acoes_qty,
		"has_bankruptcy_recovery": fal_qty > 0,
		"participacoes_empresas": participacoes,
		"raw": retorno,
	}


def fetch_nivel_socioeconomico(
	cpf: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
	"""Nível Socioeconômico e Renda Estimada PF (R$ 0,36/consulta)."""
	digits = only_digits(cpf)
	if not digits or len(digits) != 11:
		raise ValueError(f"CPF inválido para consulta de Nível Socioeconômico: {cpf}")

	data = _request_directd("NivelSocioeconomico", {"CPF": digits}, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	return {
		"cpf": retorno.get("cpf") or digits,
		"renda_estimada": _to_float(retorno.get("rendaEstimada")),
		"renda_estimada_texto": retorno.get("rendaEstimada") or "",
		"renda_faixa_salarial": retorno.get("rendaFaixaSalarial") or "",
		"classe_social": retorno.get("classeSocial") or "",
		"escolaridade": retorno.get("escolaridade") or "",
		"cbo": retorno.get("cbo") or "",
		"codigo_cbo": retorno.get("codigoCBO") or "",
		"perfil_domiciliar": retorno.get("perfilDomiciliar") or {},
		"raw": retorno,
	}


def fetch_dossie_credito_completo(documento: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Dossiê QUOD - Completo (bureau pago sob demanda, R$ 6,99/consulta). Aceita CPF ou CNPJ —
	usa `entidadeJuridica` para CNPJ e `entidadeFisica`/`pessoaFisica` para CPF."""
	params = _cpf_ou_cnpj_params(documento)

	data = _request_directd("DossieCreditoCompleto", params, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	entidade = (
		retorno.get("entidadeJuridica")
		or retorno.get("entidadeFisica")
		or retorno.get("pessoaJuridica")
		or retorno.get("pessoaFisica")
		or {}
	)
	score_entidades = entidade.get("scoreEntidades") or entidade.get("scoreEntidade") or {}
	score_block = (
		score_entidades.get("entidadeJuridica")
		or score_entidades.get("entidadeFisica")
		or score_entidades.get("pessoaJuridica")
		or score_entidades.get("pessoaFisica")
		or {}
	)
	pendencia = entidade.get("pendenciaFinanceira") or {}
	protestos = pendencia.get("protestos") or []
	acoes = pendencia.get("acoesJudiciais") or []
	negativacoes = pendencia.get("negativacoes") or []
	falencias = pendencia.get("recuperacoesJudiciaisFalencia") or []
	cheques = pendencia.get("chequesSemFundo") or []

	return {
		"score": _to_float(score_block.get("score")) or None,
		"motivos": score_block.get("motivos") or [],
		"status_pendencia": pendencia.get("status") or "",
		"total_pendencia": _to_float(pendencia.get("totalPendencia")),
		"has_protests": len(protestos) > 0,
		"protests_count": len(protestos),
		"has_civil_lawsuits": len(acoes) > 0,
		"civil_lawsuits_count": len(acoes),
		"has_negativacoes": len(negativacoes) > 0,
		"negativacoes_count": len(negativacoes),
		"has_bankruptcy_recovery": len(falencias) > 0,
		"has_bad_checks": len(cheques) > 0,
		"socios": (entidade.get("quadroSocietario") or {}).get("informacoes") or [],
		"raw": retorno,
	}


def fetch_scr_bacen(documento: str, token: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""SCR Analítico - Resumo BACEN (bureau pago sob demanda, R$ 3,90/consulta). Dívida bancária
	real (Sistema de Informações de Crédito do Banco Central), aceita CPF ou CNPJ."""
	params = _cpf_ou_cnpj_params(documento)

	data = _request_directd("SCRBacen", params, token, base_url, timeout)
	retorno = data.get("retorno") or {}
	carteira = retorno.get("carteiraCredito") or {}

	return {
		"quantidade_instituicoes": int(_to_float(retorno.get("quantidadeInstituicoes"))),
		"quantidade_operacoes": int(_to_float(retorno.get("quantidadeOperacoes"))),
		"situacao": retorno.get("situacao") or "",
		"classe_risco": retorno.get("classeRisco") or "",
		"score": retorno.get("score") or "",
		"valor_a_vencer": _to_float(carteira.get("valorVencer")),
		"valor_vencido": _to_float(carteira.get("valorVencida")),
		"raw": retorno,
	}


def merge_lawsuit_sources(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
	"""Combina dois resultados 'formato Escavador' (ex.: Escavador + DirectD Processos) da forma
	mais conservadora: cada contagem/alerta usa o maior valor ou o OU lógico entre as fontes, para
	nunca perder um sinal de risco que uma das duas tenha encontrado."""
	if a == b:
		return a
	return {
		"cnpj": a.get("cnpj") or b.get("cnpj"),
		"total_count": max(a.get("total_count") or 0, b.get("total_count") or 0),
		"defendant_count": max(a.get("defendant_count") or 0, b.get("defendant_count") or 0),
		"plaintiff_count": max(a.get("plaintiff_count") or 0, b.get("plaintiff_count") or 0),
		"other_count": max(a.get("other_count") or 0, b.get("other_count") or 0),
		"highest_value": max(a.get("highest_value") or 0.0, b.get("highest_value") or 0.0),
		"has_bankruptcy_recovery": bool(a.get("has_bankruptcy_recovery")) or bool(b.get("has_bankruptcy_recovery")),
		"critical_lawsuits": (a.get("critical_lawsuits") or []) + (b.get("critical_lawsuits") or []),
		"items_analyzed": (a.get("items_analyzed") or 0) + (b.get("items_analyzed") or 0),
		"raw_envolvido": a.get("raw_envolvido") or b.get("raw_envolvido") or {},
	}
