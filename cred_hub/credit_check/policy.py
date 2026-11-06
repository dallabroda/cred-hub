"""Política de crédito da empresa (credtech#316): metodologia em texto, que a IA segue, e parâmetros, que viram
alertas automáticos. Vem um padrão pronto (os 5 Cs do crédito) e o cliente edita. Cada vez que salva nasce uma
versão nova; o parecer guarda a versão que usou.

Os parâmetros não reprovam nada: viram alertas no parecer ("o sistema avisa, o operador decide"). 0 = sem limite.
"""

from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, now_datetime, today

VERSION_DOCTYPE = "Cred Hub Credit Policy Version"
MAX_METHODOLOGY_CHARS = 20000

DEFAULT_PARAMETERS = {
	# empresa com menos meses de atividade que isso gera alerta (0 = sem limite)
	"min_company_age_months": 12,
	# percentual vencido da dívida bancária (SCR) acima disso gera alerta (0 = sem limite)
	"max_scr_overdue_percent": 10,
	# sócio com restrição (pendência financeira, protesto ou dívida bancária vencida) gera alerta
	"alert_partner_restrictions": 1,
}

DEFAULT_METHODOLOGY = """Metodologia padrão de análise de crédito de empresas (os 5 Cs do crédito)

Objetivo: recomendar se a operação pode seguir, usando só os dados consultados e os dados do card. A decisão é sempre do analista.

1. Caráter (histórico de pagamento e conduta): protestos, negativações (PEFIN, REFIN), cheques sem fundo, ações de cobrança, sanções (CEIS/CNEP), score e probabilidade de inadimplência. Pese a recência e o valor: um protesto antigo e pequeno pesa menos que vários recentes. Restrição de sócio também pesa no caráter da empresa.

2. Capacidade (gerar caixa para pagar): faturamento informado e presumido, porte, tempo de atividade, dívida bancária no SCR (valor a vencer, valor vencido, número de instituições) e consultas recentes de outras empresas (muitas consultas em pouco tempo podem indicar busca de crédito). Compare o valor pedido com o faturamento e com a dívida bancária.

3. Capital (patrimônio e estrutura): capital social, natureza jurídica, quadro societário, dívida ativa da União (PGFN) e situação cadastral.

4. Colateral (garantias): as garantias oferecidas no card. Se não houver garantia informada, diga.

5. Condições (contexto da operação): finalidade, prazo, setor de atividade (CNAE) e o que falta saber.

Como concluir:
- Aprovar: sem restrições relevantes e capacidade compatível com o valor pedido.
- Aprovar com ressalvas: há pontos de atenção que o analista deve tratar antes de liberar (por exemplo, pedir garantia, confirmar faturamento, reduzir a exposição).
- Recusar: restrições graves e recentes; restrições ativas (protestos, negativações ou cheques sem fundo) junto com score na faixa de risco mais alta do bureau (por exemplo, até 300 numa escala de 0 a 1000) ou probabilidade de inadimplência alta; falência ou recuperação judicial; ou incapacidade evidente de pagar.

Dado que falta não vira aprovação: se uma consulta do pacote mínimo falhou ou não foi feita, ou se faltou uma informação importante do card (como o valor pedido), diga qual e recomende no máximo "aprovar com ressalvas". Consulta fora do pacote mínimo que não foi feita não muda o resultado."""


def _clean_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
	params = dict(DEFAULT_PARAMETERS)
	for key, value in (parameters or {}).items():
		if key not in DEFAULT_PARAMETERS:
			continue
		try:
			number = float(value or 0)
		except (TypeError, ValueError):
			frappe.throw(_("Invalid value for {0}.").format(key))
		if number < 0:
			frappe.throw(_("{0} cannot be negative.").format(key))
		params[key] = int(number) if key != "max_scr_overdue_percent" else round(number, 2)
	params["alert_partner_restrictions"] = 1 if params["alert_partner_restrictions"] else 0
	return params


def current() -> dict[str, Any]:
	"""A política em vigor: a versão mais recente salva pelo cliente, ou o padrão (versão 0)."""
	rows = frappe.get_all(
		VERSION_DOCTYPE,
		fields=["version", "methodology", "parameters_json", "saved_by", "saved_on"],
		order_by="version desc",
		limit_page_length=1,
	)
	if not rows:
		return {
			"version": 0,
			"methodology": DEFAULT_METHODOLOGY,
			"parameters": dict(DEFAULT_PARAMETERS),
			"saved_by": "",
			"saved_on": None,
			"is_default": True,
		}
	row = rows[0]
	try:
		parameters = json.loads(row.parameters_json or "{}")
	except ValueError:
		parameters = {}
	return {
		"version": row.version,
		"methodology": row.methodology or DEFAULT_METHODOLOGY,
		"parameters": _clean_parameters(parameters),
		"saved_by": row.saved_by,
		"saved_on": row.saved_on,
		"is_default": False,
	}


def save(methodology: str, parameters: dict[str, Any] | str | None) -> dict[str, Any]:
	"""Salva uma versão nova da política (a anterior fica guardada, os pareceres antigos apontam para ela)."""
	methodology = (methodology or "").strip()
	if not methodology:
		frappe.throw(_("The methodology cannot be empty."))
	if len(methodology) > MAX_METHODOLOGY_CHARS:
		frappe.throw(_("The methodology is too long (maximum {0} characters).").format(MAX_METHODOLOGY_CHARS))
	if isinstance(parameters, str):
		parameters = json.loads(parameters or "{}")
	params = _clean_parameters(parameters)
	latest = current()
	if not latest["is_default"] and latest["methodology"] == methodology and latest["parameters"] == params:
		return latest
	frappe.get_doc(
		{
			"doctype": VERSION_DOCTYPE,
			"version": latest["version"] + 1,
			"saved_by": frappe.session.user,
			"saved_on": now_datetime(),
			"methodology": methodology,
			"parameters_json": json.dumps(params),
		}
	).insert(ignore_permissions=True)
	return current()


def br_number(value: Any) -> float | None:
	"""'12.500,00' / 'R$ 1.234,5' / '1234.56' / 1234 -> float; texto sem número -> None."""
	if isinstance(value, bool) or value is None:
		return None
	if isinstance(value, (int, float)):
		return float(value)
	text = re.sub(r"[^\d,.\-]", "", str(value))
	if not text or not re.search(r"\d", text):
		return None
	if "," in text:
		text = text.replace(".", "").replace(",", ".")
	elif text.count(".") > 1:
		text = text.replace(".", "")
	try:
		return float(text)
	except ValueError:
		return None


def _company_age_months(org) -> int | None:
	start = org.get("activity_start_date")
	if start:
		return max(0, date_diff(today(), getdate(start)) // 30)
	years = org.get("years_in_business")
	return int(years) * 12 if years else None


def _scr_overdue(payload: Any) -> tuple[float, float] | None:
	"""(vencido, a vencer) do SCR Bacen da DirectD, quando o retorno tem a carteira."""
	if not isinstance(payload, dict):
		return None
	retorno = payload.get("retorno") if isinstance(payload.get("retorno"), dict) else payload
	carteira = retorno.get("carteiraCredito")
	if not isinstance(carteira, dict):
		return None
	overdue = br_number(carteira.get("valorVencida"))
	due = br_number(carteira.get("valorVencer"))
	if overdue is None and due is None:
		return None
	return overdue or 0.0, due or 0.0


def _partner_restrictions(payload: Any) -> list[str]:
	"""Restrições de um sócio no Dossiê QUOD (pendência financeira) ou no SCR (dívida vencida)."""
	found = []
	if not isinstance(payload, dict):
		return found
	retorno = payload.get("retorno") if isinstance(payload.get("retorno"), dict) else payload
	pf = retorno.get("entidadeFisica") if isinstance(retorno.get("entidadeFisica"), dict) else None
	if pf:
		pendencia = pf.get("pendenciaFinanceira") or {}
		total = br_number(pendencia.get("totalPendencia"))
		if total and total > 0:
			found.append(_("financial pendencies of {0}").format(pendencia.get("totalPendencia")))
		for key in ("protestos", "negativacoes", "chequesSemFundo", "recuperacoesJudiciaisFalencia"):
			items = pendencia.get(key)
			if isinstance(items, list) and items:
				found.append(f"{key}: {len(items)}")
	scr = _scr_overdue(payload)
	if scr and scr[0] > 0:
		found.append(_("overdue bank debt (SCR) of R$ {0:,.2f}").format(scr[0]))
	return found


def alerts(org, sections: list[dict[str, Any]], params: dict[str, Any]) -> list[str]:
	"""Alertas da política calculados direto dos dados (sem IA): o mesmo dado dá sempre o mesmo alerta."""
	if not getattr(frappe.flags, "in_test", False):
		frappe.local.lang = "pt-BR"
		if getattr(frappe.local, "job", None) is not None:
			frappe.local.job.lang_resolved = True
	out: list[str] = []
	minimum = int(params.get("min_company_age_months") or 0)
	age = _company_age_months(org)
	if minimum and age is not None and age < minimum:
		out.append(
			_("Company with {0} months of activity, below the policy minimum of {1} months.").format(age, minimum)
		)

	max_overdue = float(params.get("max_scr_overdue_percent") or 0)
	for section in sections:
		is_partner = section["subject"] != org.organization_name
		if "SCR" in section["product"]:
			scr = _scr_overdue(section["payload"])
			if scr and max_overdue:
				overdue, due = scr
				total = overdue + due
				percent = round(overdue / total * 100, 1) if total else 0.0
				if percent > max_overdue:
					out.append(
						_("{0}: {1}% of the bank debt (SCR) is overdue, above the policy limit of {2}%.").format(
							section["subject"], percent, max_overdue
						)
					)
		if is_partner and params.get("alert_partner_restrictions"):
			found = _partner_restrictions(section["payload"])
			if found:
				out.append(
					_("Partner {0} has restrictions ({1}: {2}).").format(
						section["subject"], section["product"], "; ".join(found)
					)
				)
	return out
