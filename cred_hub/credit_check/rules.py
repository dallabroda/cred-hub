"""Motor de Regras e Parecer de Risco Pré-Crédito (Python puro, sem dependência do Frappe).

Calcula o veredito da esteira de baixo custo com base nos retornos do Escavador e Infosimples,
aplicando os limites de tolerância configurados pela empresa.
"""

from __future__ import annotations

from typing import Any

# Vereditos possíveis
VERDICT_PRE_APPROVED = "Pre-Approved"  # Aprovado para Birô / Sem restrições graves
VERDICT_WARNING = "Warning"            # Atenção: Pequenas pendências dentro da tolerância
VERDICT_REJECTED = "Rejected"          # Reprovado no Pré-Filtro (falência, execuções, protestos)
VERDICT_ERROR = "Error"                # Não verificado: um provedor habilitado falhou ou está sem credencial


def evaluate_credit_risk(
	escavador_data: dict[str, Any],
	infosimples_data: dict[str, Any],
	settings: dict[str, Any] | None = None,
	provider_failures: list[str] | None = None,
	directd_score_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Avalia os dados judiciais e fiscais retornando o parecer e as justificativas.

	Se algum provedor habilitado falhou (`provider_failures`), o parecer é sempre 'Error':
	dado incompleto nunca pode virar aprovação por omissão (fail-open).
	"""
	if provider_failures:
		reasons_fail = [f"Falha na consulta: {item}" for item in provider_failures]
		return {
			"verdict": VERDICT_ERROR,
			"summary": f"Não verificado — {'; '.join(reasons_fail)}. Nenhum parecer de crédito foi emitido; tente novamente.",
			"reasons": reasons_fail,
			"warnings": [],
			"metrics": {
				"lawsuits_total": None,
				"lawsuits_defendant": None,
				"lawsuits_highest_value": None,
				"has_bankruptcy": None,
				"pgfn_debt": None,
				"cnd_federal_status": "Não verificado",
				"cnd_federal_pdf": "",
				"cndt_status": "Não verificado",
				"cndt_pdf": "",
				"cenprot_has_protest": None,
				"cenprot_protests_count": None,
				"fgts_status": "Não verificado",
				"directd_score": None,
			},
		}

	cfg = settings or {}
	max_lawsuits_defendant = int(cfg.get("max_lawsuits_defendant", 0))
	max_lawsuit_value = float(cfg.get("max_lawsuit_value", 0.0))
	block_on_bankruptcy = bool(cfg.get("block_on_bankruptcy", True))
	max_pgfn_debt = float(cfg.get("max_pgfn_debt", 0.0))
	block_on_cndt = bool(cfg.get("block_on_cndt", True))
	block_on_protest = bool(cfg.get("block_on_protest", True))
	block_on_fgts_irregular = bool(cfg.get("block_on_fgts_irregular", True))
	min_directd_score = int(cfg.get("min_directd_score", 0))

	reasons: list[str] = []
	warnings: list[str] = []
	is_rejected = False

	# 1. Falência e Recuperação Judicial (Escavador)
	has_bankruptcy = bool(escavador_data.get("has_bankruptcy_recovery"))
	if has_bankruptcy:
		if block_on_bankruptcy:
			is_rejected = True
			reasons.append("Processo de falência ou recuperação judicial em andamento.")
		else:
			warnings.append("Alerta de falência ou recuperação judicial identificado.")

	# 2. Processos como Réu e Valor de Causa (Escavador)
	defendant_count = int(escavador_data.get("defendant_count") or 0)
	highest_lawsuit_val = float(escavador_data.get("highest_value") or 0.0)

	if defendant_count > max_lawsuits_defendant:
		if max_lawsuits_defendant == 0:
			is_rejected = True
			reasons.append(f"Figura como ré em {defendant_count} processo(s) judicial(is).")
		else:
			is_rejected = True
			reasons.append(f"Número de processos como réu ({defendant_count}) excede o limite tolerado ({max_lawsuits_defendant}).")
	elif defendant_count > 0:
		warnings.append(f"Figura como ré em {defendant_count} processo(s) judicial(is).")

	if max_lawsuit_value > 0.0 and highest_lawsuit_val > max_lawsuit_value:
		is_rejected = True
		reasons.append(f"Maior valor de ação executiva (R$ {highest_lawsuit_val:,.2f}) excede o limite tolerado (R$ {max_lawsuit_value:,.2f}).")

	# 3. Dívida Ativa da União e FGTS (Infosimples - PGFN)
	pgfn = infosimples_data.get("pgfn") or {}
	pgfn_debt = float(pgfn.get("total_debt") or 0.0)
	pgfn_inscriptions = int(pgfn.get("inscriptions_count") or 0)

	if pgfn_debt > max_pgfn_debt:
		if max_pgfn_debt == 0.0:
			is_rejected = True
			reasons.append(f"Inscrito na Dívida Ativa da União (PGFN) com débitos de R$ {pgfn_debt:,.2f}.")
		else:
			is_rejected = True
			reasons.append(f"Dívida Ativa da União (R$ {pgfn_debt:,.2f}) excede a tolerância (R$ {max_pgfn_debt:,.2f}).")
	elif pgfn_debt > 0.0 or pgfn_inscriptions > 0:
		warnings.append(f"Possui débito inscrito na PGFN de R$ {pgfn_debt:,.2f} ({pgfn_inscriptions} inscrições).")

	# 4. Certidão Negativa de Débitos Trabalhistas (Infosimples - TST/CNDT)
	cndt = infosimples_data.get("cndt") or {}
	cndt_regular = bool(cndt.get("is_regular", True))
	if not cndt_regular:
		if block_on_cndt:
			is_rejected = True
			reasons.append("Consta no Banco Nacional de Devedores Trabalhistas (CNDT irregular).")
		else:
			warnings.append("Débitos trabalhistas pendentes no TST.")

	# 5. Protestos em Cartório (Infosimples - CENPROT)
	cenprot = infosimples_data.get("cenprot") or {}
	has_protest = bool(cenprot.get("has_protests"))
	protest_count = int(cenprot.get("protests_count") or 0)

	if has_protest:
		if block_on_protest:
			is_rejected = True
			reasons.append(f"Possui {protest_count or 'apontamentos de'} título(s) protestado(s) em cartório (CENPROT).")
		else:
			warnings.append(f"Protestos em cartório identificados ({protest_count} títulos).")

	# 6. CND Federal (Receita / PGFN)
	cnd_fed = infosimples_data.get("cnd_federal") or {}
	cnd_fed_regular = bool(cnd_fed.get("is_regular", True))
	if not cnd_fed_regular:
		warnings.append(f"CND Federal com pendências: {cnd_fed.get('status') or 'Positiva'}.")

	# 7. Regularidade do FGTS (DirectD)
	fgts = infosimples_data.get("fgts") or {}
	fgts_regular = bool(fgts.get("is_regular", True))
	if not fgts_regular:
		if block_on_fgts_irregular:
			is_rejected = True
			reasons.append("FGTS irregular perante a Caixa Econômica.")
		else:
			warnings.append("FGTS irregular perante a Caixa Econômica.")

	# 8. Score de Crédito (DirectD / Quod) — só reprova se o cliente configurou um piso (> 0)
	directd_score = None
	if directd_score_data and directd_score_data.get("score") is not None:
		directd_score = int(directd_score_data["score"])
		if min_directd_score > 0 and directd_score < min_directd_score:
			is_rejected = True
			reasons.append(f"Score de crédito DirectD ({directd_score}) abaixo do piso tolerado ({min_directd_score}).")
		elif min_directd_score > 0:
			warnings.append(f"Score de crédito DirectD: {directd_score} (faixa: {directd_score_data.get('faixa_score') or '—'}).")

	# Determinação do veredito final
	if is_rejected:
		verdict = VERDICT_REJECTED
		summary_text = f"Reprovado no Pré-Filtro: {'; '.join(reasons)}"
	elif warnings:
		verdict = VERDICT_WARNING
		summary_text = f"Atenção / Pré-Aprovado com ressalvas: {'; '.join(warnings)}"
	else:
		verdict = VERDICT_PRE_APPROVED
		summary_text = "Aprovado no Pré-Filtro de Crédito e Compliance (Sem execuções, falência, PGFN ou protestos)."

	return {
		"verdict": verdict,
		"summary": summary_text,
		"reasons": reasons,
		"warnings": warnings,
		"metrics": {
			"lawsuits_total": escavador_data.get("total_count", 0),
			"lawsuits_defendant": defendant_count,
			"lawsuits_highest_value": highest_lawsuit_val,
			"has_bankruptcy": has_bankruptcy,
			"pgfn_debt": pgfn_debt,
			"cnd_federal_status": cnd_fed.get("status", "Regular"),
			"cnd_federal_pdf": cnd_fed.get("pdf_url", ""),
			"cndt_status": cndt.get("status", "Regular"),
			"cndt_pdf": cndt.get("pdf_url", ""),
			"cenprot_has_protest": has_protest,
			"cenprot_protests_count": protest_count,
			"fgts_status": fgts.get("status") or ("Regular" if fgts_regular else "Irregular"),
			"directd_score": directd_score,
		},
	}


def format_timeline_summary(evaluation: dict[str, Any], cnpj_formatted: str) -> str:
	"""Formata o comentário amigável para a linha do tempo do CRM."""
	verdict = evaluation.get("verdict")
	metrics = evaluation.get("metrics") or {}
	reasons = evaluation.get("reasons") or []
	warnings = evaluation.get("warnings") or []

	if verdict == VERDICT_ERROR:
		lines = [
			f"❌ **Não verificado — consulta de crédito falhou** (CNPJ {cnpj_formatted})",
			f"**Motivo:** {'; '.join(reasons) if reasons else 'erro desconhecido na consulta.'}",
			"Nenhum parecer de aprovação foi emitido. Corrija a configuração do provedor e tente novamente.",
		]
		return "\n".join(lines)

	if verdict == VERDICT_REJECTED:
		icon = "🚫"
		badge = "Reprovado no Pré-Filtro de Crédito"
	elif verdict == VERDICT_WARNING:
		icon = "⚠️"
		badge = "Atenção: Pré-Aprovado com Apontamentos"
	else:
		icon = "✅"
		badge = "Aprovado no Pré-Filtro de Crédito e Compliance"

	lines = [
		f"{icon} **{badge}** (CNPJ {cnpj_formatted})",
		f"• **Processos Judiciais:** {metrics.get('lawsuits_defendant', 0)} como réu (total: {metrics.get('lawsuits_total', 0)}). Falência/Recuperação: {'Sim ⚠️' if metrics.get('has_bankruptcy') else 'Não'}.",
		f"• **Dívida Ativa da União (PGFN):** R$ {metrics.get('pgfn_debt', 0.0):,.2f}.",
		f"• **Certidão Trabalhista (CNDT):** {metrics.get('cndt_status')}.",
		f"• **Protestos em Cartório (CENPROT):** {'Sim (' + str(metrics.get('cenprot_protests_count', 0)) + ')' if metrics.get('cenprot_has_protest') else 'Nenhum'}.",
		f"• **FGTS:** {metrics.get('fgts_status')}.",
	]
	if metrics.get("directd_score") is not None:
		lines.append(f"• **Score de Crédito (DirectD/Quod):** {metrics['directd_score']}.")

	if reasons:
		lines.append(f"**Motivos de restrição:** {'; '.join(reasons)}")
	elif warnings:
		lines.append(f"**Observações:** {'; '.join(warnings)}")

	return "\n".join(lines)
