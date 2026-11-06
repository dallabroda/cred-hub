"""Dossiê de crédito, PDF 1: retorno consolidado das consultas em layout executivo compacto (credtech#316).

Apresenta, por provedor e por documento (empresa e sócios), os dados retornados pelos bureaus e fontes públicas
em formato executivo de alta densidade (tabelas horizontais, grids multi-coluna e resumos estatísticos), com
nomes traduzidos para português (`labels.py`). O pacote JSON original integral de todas as consultas é salvo em
anexo para auditoria técnica.
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any

import frappe
from frappe import _
from frappe.utils import date_diff, format_datetime, getdate, now_datetime, today

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check import partners as partners_lib
from cred_hub.credit_check.labels import label_for
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS

FILE_PREFIX = "Dossie-Retorno-"
ORG = "CRM Organization"

# Nome do produto de cada chamada automática (provedor, endpoint)
PRODUCT_LABELS = {
	"ceis_cnep": "Sanções Públicas CEIS/CNEP (CGU)",
	"score": "Score de crédito (QUOD)",
	"protestosbasica": "Protestos Nacional (IEPTB)",
	"processosjudiciaissimplificada": "Processos Judiciais (Simplificada)",
	"processosjudiciaiscompleta": "Processos Judiciais - Completa (com Penhoras)",
	"boavistariscopositivopj": "Boa Vista - Risco Positivo PJ",
	"boavistaacertacompletopositivopf": "Boa Vista - Acerta Completo Positivo PF",
	"dossiecreditocompleto": "Dossiê QUOD - Completo",
	"nivelsocioeconomico": "Nível Socioeconômico e Renda Presumida PF",
	"caixaregularidadeempregadorfgts": "Regularidade do Empregador (FGTS)",
	"sintegraccc-completa": "Sintegra / CCC",
	"sintegraccc": "Sintegra / CCC",
	"receita-federal/pgfn-devedores": "Dívida Ativa da União (PGFN)",
	"receita-federal/pgfn": "Dívida Ativa da União (PGFN)",
	"receita-federal/cnd": "Certidão Negativa de Débitos Federais (CND)",
	"tst/cndt": "Certidão Negativa de Débitos Trabalhistas (CNDT)",
	"protesto/cenprot-sp": "Protestos em Cartório (CENPROT)",
	"envolvido/processos": "Processos Judiciais",
	"scrbacen": "SCR Analítico - Resumo BACEN",
}
# Resumos antigos (relatório sem o retorno inteiro): chave do raw_details_json -> (provedor, produto)
SUMMARY_SLOTS = {
	"cgu": ("Portal da Transparência", "Sanções CEIS/CNEP"),
	"escavador": ("Escavador", "Processos judiciais"),
	"infosimples": ("Infosimples", "Regularidade fiscal e certidões"),
	"directd_score": ("DirectD", "Score de crédito (QUOD)"),
	"directd_protestos": ("DirectD", "Protestos Nacional (IEPTB)"),
	"directd_processos": ("DirectD", "Processos judiciais (simplificada)"),
	"directd_fgts": ("DirectD", "FGTS: regularidade do empregador"),
	"directd_sintegra": ("DirectD", "Sintegra / CCC"),
}

# Chaves puramente técnicas de transporte HTTP omitidas apenas da visualização PDF (mantidas no JSON bruto)
TECHNICAL_META_KEYS = {
	"metaDados",
	"header",
	"site_receipts",
	"site_receipt",
	"signature",
	"api_version",
	"api_version_full",
	"remote_ip",
	"token_name",
	"billable",
	"elapsed_time_in_milliseconds",
	"consultaUid",
	"enviarCallback",
	"gerarComprovante",
	"urlComprovante",
	"assincrono",
	"tempoExecucaoMs",
	"resultadoId",
	"apiVersao",
}

MAX_TABLE_ITEMS = 15


def _parse_num(val: Any) -> float:
	if val is None or isinstance(val, bool):
		return 0.0
	if isinstance(val, (int, float)):
		return float(val)
	s = re.sub(r"[^\d,.\-]", "", str(val))
	if not s or not re.search(r"\d", s):
		return 0.0
	if "," in s:
		s = s.replace(".", "").replace(",", ".")
	elif s.count(".") > 1:
		s = s.replace(".", "")
	try:
		return float(s)
	except ValueError:
		return 0.0


def format_value(value: Any) -> str:
	"""O valor como veio, só legível: Sim/Não para booleano, traço para nulo, (vazio) para texto vazio."""
	frappe.local.lang = "pt-BR"
	if getattr(frappe.local, "job", None) is not None:
		frappe.local.job.lang_resolved = True
	if value is None:
		return "—"
	if isinstance(value, bool):
		return _("Yes") if value else _("No")
	if isinstance(value, str):
		return value if value != "" else _("(empty)")
	return str(value)


def rows_of(node: Any, depth: int = 0) -> list[dict[str, Any]]:
	"""Todos os campos do retorno, em ordem, como linhas: títulos de grupo (objeto ou lista) e valores."""
	rows: list[dict[str, Any]] = []
	if isinstance(node, dict):
		for key, value in node.items():
			label, translated = label_for(key)
			if isinstance(value, (dict, list)) and value:
				rows.append({"depth": depth, "label": label, "key": key, "translated": translated, "group": True})
				rows += rows_of(value, depth + 1)
			else:
				shown = _("(empty)") if isinstance(value, (dict, list)) else format_value(value)
				rows.append(
					{"depth": depth, "label": label, "key": key, "translated": translated, "value": shown}
				)
	elif isinstance(node, list):
		for index, item in enumerate(node, start=1):
			if isinstance(item, (dict, list)) and item:
				rows.append({"depth": depth, "label": f"{_('Item')} {index}", "key": "", "translated": True, "group": True})
				rows += rows_of(item, depth + 1)
			else:
				rows.append(
					{
						"depth": depth,
						"label": f"{_('Item')} {index}",
						"key": "",
						"translated": True,
						"value": _("(empty)") if isinstance(item, (dict, list)) else format_value(item),
					}
				)
	else:
		rows.append({"depth": depth, "label": _("Value"), "key": "", "translated": True, "value": format_value(node)})
	return rows


def _product_label(endpoint: str) -> str:
	return PRODUCT_LABELS.get(str(endpoint).lower()) or str(endpoint)


def _bureau_label(bureau: str) -> str:
	if bureau == "serasa":
		return "Serasa Experian (Relatório Avançado PJ)"
	registry = PREMIUM_BUREAUS.get(bureau)
	return registry.label if registry else bureau


def _is_uncontracted_infosimples_call(call: dict[str, Any]) -> bool:
	"""Ignora chamadas Infosimples com erro 602 (serviço não habilitado/inválido sem dados e sem cobrança)."""
	if (call.get("provider") or "").lower() != "infosimples":
		return False
	payload = call.get("payload")
	if not isinstance(payload, dict):
		return False
	code = payload.get("code")
	if code in (602, 601, 603) and not payload.get("data"):
		return True
	return False


def collect(organization_name: str) -> dict[str, Any]:
	"""Tudo que entra no dossiê: dados da empresa, sócios e uma seção por consulta."""
	org = frappe.get_doc(ORG, organization_name)
	cnpj = org.get("cnpj") or ""
	sections: list[dict[str, Any]] = []
	failures: list[str] = []
	complete = True

	report_name = org.get("credit_report")
	if report_name and frappe.db.exists("Cred Hub Credit Report", report_name):
		report = frappe.get_doc("Cred Hub Credit Report", report_name)
		try:
			raw = json.loads(report.raw_details_json or "{}")
		except ValueError:
			raw = {}
		failures = [str(item) for item in raw.get("failures") or []]
		calls = raw.get("raw_responses") or []
		if calls:
			for call in calls:
				if _is_uncontracted_infosimples_call(call):
					continue
				sections.append(
					{
						"provider": call.get("provider") or "",
						"product": _product_label(call.get("endpoint") or ""),
						"subject": org.organization_name,
						"document": cnpj,
						"when": str(report.checked_on),
						"payload": call.get("payload"),
						"summary_only": False,
					}
				)
		else:
			for slot, (provider, product) in SUMMARY_SLOTS.items():
				if raw.get(slot):
					complete = False
					sections.append(
						{
							"provider": provider,
							"product": product,
							"subject": org.organization_name,
							"document": cnpj,
							"when": str(report.checked_on),
							"payload": raw[slot],
							"summary_only": True,
						}
					)

	latest: dict[tuple[str, str], Any] = {}
	for row in frappe.get_all(
		"Cred Hub Premium Bureau Query",
		filters={"organization": organization_name},
		fields=["name", "bureau", "document", "cnpj", "contact", "subject_type", "requested_on", "raw_response_json"],
		order_by="requested_on desc",
		limit_page_length=500,
	):
		latest.setdefault((row.bureau, row.document or row.cnpj or ""), row)
	contact_names = {}
	for row in latest.values():
		if row.contact and row.contact not in contact_names:
			contact_names[row.contact] = frappe.db.get_value("Contact", row.contact, "full_name") or row.contact
	for row in latest.values():
		try:
			payload = json.loads(row.raw_response_json or "{}")
		except ValueError:
			payload = {"raw_text": row.raw_response_json}
		sections.append(
			{
				"provider": "Serasa Experian" if row.bureau == "serasa" else "DirectD",
				"product": _bureau_label(row.bureau),
				"subject": contact_names.get(row.contact) or org.organization_name,
				"document": row.document or row.cnpj or "",
				"when": format_datetime(row.requested_on),
				"payload": payload,
				"summary_only": False,
			}
		)

	return {
		"organization": org.organization_name,
		"organization_name": org.name,
		"cnpj": cnpj,
		"partners": partners_lib.partners_of(organization_name),
		"sections": sections,
		"failures": failures,
		"complete": complete,
	}


CSS = """
@page { size: A4; margin: 10mm 12mm; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 8.5pt; color: #1e293b; line-height: 1.35; margin: 0; }
.header-banner { background: #1e1b4b; color: #ffffff; padding: 10px 14px; border-radius: 6px; margin-bottom: 10px; }
.header-banner h1 { font-size: 14pt; margin: 0 0 2px; color: #ffffff; letter-spacing: -0.2px; }
.header-banner .sub { color: #cbd5e1; font-size: 9pt; font-weight: bold; margin-bottom: 3px; }
.header-banner .meta { color: #94a3b8; font-size: 7.8pt; margin: 0; }
h2 { font-size: 10pt; margin: 12px 0 4px; padding: 4px 8px; background: #f1f5f9; border-left: 3px solid #4f46e5; color: #0f172a; page-break-after: avoid; }
h3 { font-size: 8.8pt; margin: 6px 0 3px; color: #334155; font-weight: bold; page-break-after: avoid; }
.sec-meta { color: #64748b; font-size: 7.8pt; margin: -2px 0 5px 8px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 6px; page-break-inside: auto; }
tr { page-break-inside: avoid; page-break-after: auto; }
th { background: #f8fafc; color: #334155; font-weight: bold; text-align: left; padding: 3px 6px; border: 1px solid #e2e8f0; font-size: 7.8pt; }
td { padding: 2.5px 6px; vertical-align: top; border: 1px solid #e2e8f0; font-size: 8pt; }
td.label { width: 22%; color: #475569; background: #f8fafc; font-weight: 600; }
td.value { width: 28%; word-break: break-word; color: #0f172a; }
td.label-single { width: 36%; color: #475569; background: #f8fafc; font-weight: 600; }
td.value-single { width: 64%; word-break: break-word; color: #0f172a; }
tr.group td { background: #eef2ff; font-weight: bold; color: #312e81; font-size: 8pt; padding: 3px 6px; }
.orig { color: #94a3b8; font-size: 7pt; font-weight: normal; }
.note { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; padding: 5px 8px; border-radius: 4px; margin: 6px 0; font-size: 7.8pt; }
.alert-box { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 5px 8px; border-radius: 4px; margin: 6px 0; font-size: 8pt; font-weight: 600; }
.ok-box { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; padding: 4px 8px; border-radius: 4px; margin: 4px 0; font-size: 8pt; font-weight: 600; }
.kpi-table td { text-align: center; background: #f8fafc; padding: 5px 4px; }
.kpi-val { font-size: 10pt; font-weight: bold; color: #1e1b4b; display: block; }
.kpi-lbl { font-size: 7pt; color: #64748b; text-transform: uppercase; display: block; }
.muted { color: #64748b; font-size: 7.5pt; font-style: italic; margin: 2px 0 6px; }
.chip-list { font-size: 7.5pt; color: #334155; background: #f8fafc; border: 1px solid #e2e8f0; padding: 4px 6px; border-radius: 4px; margin-bottom: 6px; }
"""

SCOPED_VIEW_CSS = """
.ct-dossier-view { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 12.5px; color: #1E1F23; line-height: 1.45; }
.ct-dossier-view h3 { font-size: 13px; margin: 12px 0 6px; color: #1E1F23; font-weight: 600; }
.ct-dossier-view table { width: 100%; border-collapse: collapse; margin-bottom: 10px; background: #FFFFFF; border-radius: 6px; overflow: hidden; }
.ct-dossier-view th { background: #F5F5F7; color: #5E6068; font-weight: 600; text-align: left; padding: 6px 10px; border: 1px solid #E6E7EB; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.02em; }
.ct-dossier-view td { padding: 6px 10px; vertical-align: top; border: 1px solid #E6E7EB; font-size: 12px; color: #1E1F23; }
.ct-dossier-view td.label { width: 22%; color: #5E6068; background: #F5F5F7; font-weight: 600; }
.ct-dossier-view td.value { width: 28%; word-break: break-word; color: #1E1F23; }
.ct-dossier-view td.label-single { width: 35%; color: #5E6068; background: #F5F5F7; font-weight: 600; }
.ct-dossier-view td.value-single { width: 65%; word-break: break-word; color: #1E1F23; }
.ct-dossier-view .orig { color: #8A8C94; font-size: 10.5px; font-weight: normal; }
.ct-dossier-view .note { background: #FFF4D6; border: 1px solid #FDE68A; color: #8A5A00; padding: 8px 10px; border-radius: 6px; margin: 8px 0; font-size: 12px; }
.ct-dossier-view .alert-box { background: #FEF3F2; border: 1px solid #FECACA; color: #B42318; padding: 8px 10px; border-radius: 6px; margin: 8px 0; font-size: 12px; font-weight: 600; }
.ct-dossier-view .ok-box { background: #E7F6EC; border: 1px solid #BBF7D0; color: #1F7A3D; padding: 8px 10px; border-radius: 6px; margin: 8px 0; font-size: 12px; font-weight: 600; }
.ct-dossier-view .kpi-table td { text-align: center; background: #F5F5F7; padding: 8px 6px; }
.ct-dossier-view .kpi-val { font-size: 14px; font-weight: 700; color: #1E1F23; display: block; }
.ct-dossier-view .kpi-lbl { font-size: 10.5px; color: #5E6068; text-transform: uppercase; display: block; margin-top: 2px; }
.ct-dossier-view .muted { color: #8A8C94; font-size: 11.5px; font-style: italic; margin: 4px 0 8px; }
.ct-dossier-view .chip-list { font-size: 11.5px; color: #2A2B30; background: #F5F5F7; border: 1px solid #E6E7EB; padding: 6px 8px; border-radius: 6px; margin-bottom: 8px; }
"""


def _label_html(key: str) -> str:
	if not key:
		return ""
	label, translated = label_for(key)
	orig = "" if translated else f' <span class="orig">({escape(str(key))})</span>'
	return f"{escape(str(label))}{orig}"


def _fmt_brl(val: Any) -> str:
	if val is None or val == "":
		return "—"
	try:
		num = float(val)
		return f"R$ {num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
	except (ValueError, TypeError):
		return escape(str(val))


def _kv_grid_html(pairs: list[tuple[str, str]], two_columns_per_row: bool = True) -> str:
	"""Renderiza pares (label_html, value_escaped) em grid compacto de 4 colunas (2 pares por linha) ou 2 colunas."""
	if not pairs:
		return ""
	if not two_columns_per_row or len(pairs) <= 2:
		rows = [
			f'<tr><td class="label-single">{lbl}</td><td class="value-single">{val}</td></tr>'
			for lbl, val in pairs
		]
		return f"<table>{''.join(rows)}</table>"

	out = ["<table>"]
	for idx in range(0, len(pairs), 2):
		lbl1, val1 = pairs[idx]
		if idx + 1 < len(pairs):
			lbl2, val2 = pairs[idx + 1]
			out.append(
				f'<tr><td class="label">{lbl1}</td><td class="value">{val1}</td>'
				f'<td class="label">{lbl2}</td><td class="value">{val2}</td></tr>'
			)
		else:
			out.append(
				f'<tr><td class="label">{lbl1}</td><td class="value" colspan="3">{val1}</td></tr>'
			)
	out.append("</table>")
	return "".join(out)


def _unwrap_payload(payload: Any) -> Any:
	"""Remove envelopes técnicos de transporte (metaDados, header, site_receipts) mantendo 100% dos dados de negócio."""
	if not isinstance(payload, dict):
		return payload
	# Envelope DirectD: {"metaDados": ..., "retorno": ...}
	if "retorno" in payload and set(payload.keys()) <= {"metaDados", "retorno"} and isinstance(payload["retorno"], dict):
		return payload["retorno"]
	# Envelope Infosimples: {"code": 200, "header": ..., "data": [...]}
	if "data" in payload and "header" in payload and "code" in payload:
		data_items = payload.get("data")
		if isinstance(data_items, list) and len(data_items) == 1 and isinstance(data_items[0], dict):
			return data_items[0]
		if isinstance(data_items, list) and not data_items:
			return {"situacao": payload.get("code_message") or _("No records found")}
		return {"dados": data_items}
	return {k: v for k, v in payload.items() if k not in TECHNICAL_META_KEYS}


def _render_lawsuits_section(node: dict[str, Any]) -> str:
	"""Renderização executiva compacta para Processos Judiciais (Simplificada, Completa com Penhoras e Escavador)."""
	procs = node.get("processos") or []
	total = node.get("totalProcessos") if node.get("totalProcessos") is not None else len(procs)
	obs = node.get("observacoes") or ""

	if not procs:
		return f'<div class="ok-box">✓ {escape(obs or _("Nenhum processo judicial localizado para este documento."))}</div>'

	passivo_count = 0
	ativo_count = 0
	homonym_count = 0
	penhoras_total = 0
	total_val = 0.0
	max_val = 0.0
	parsed_rows = []
	penhora_alerts: list[str] = []

	for p in procs:
		if not isinstance(p, dict):
			continue
		val_raw = p.get("valorAcao") if p.get("valorAcao") is not None else (p.get("valorProcesso") if p.get("valorProcesso") is not None else p.get("valor"))
		val = _parse_num(val_raw)

		detalhes = p.get("detalhesStatusProcesso") if isinstance(p.get("detalhesStatusProcesso"), dict) else {}
		penhoras = detalhes.get("penhoras") if isinstance(detalhes.get("penhoras"), list) else []
		if penhoras:
			penhoras_total += len(penhoras)
			num_cnj = str(p.get("numeroProcesso") or p.get("numero_cnj") or "—")
			penhora_alerts.append(f"Processo {num_cnj}: {len(penhoras)} penhora(s)/bloqueio(s) registrado(s)")

		partes = p.get("partes") or []
		polos = set()
		has_confirmed_target = False
		has_only_agregado = False
		agregado_seen = False
		for pt in partes:
			if isinstance(pt, dict):
				origem = str(pt.get("origemDocumento") or "").upper()
				if origem == "AGREGADO":
					agregado_seen = True
				elif origem in ("API", "DOCUMENTO", "RECEITA", "TRIBUNAL", ""):
					has_confirmed_target = True
				polo = str(pt.get("polo") or pt.get("tipo") or "").upper()
				if "PASSIVO" in polo or "REU" in polo or "PROMOVIDO" in polo or "EXECUTADO" in polo:
					polos.add("Passivo")
				elif "ATIVO" in polo or "AUTOR" in polo or "PROMOVENTE" in polo or "EXEQUENTE" in polo:
					polos.add("Ativo")

		if agregado_seen and not has_confirmed_target:
			has_only_agregado = True
			homonym_count += 1
		else:
			if val > 0:
				total_val += val
				if val > max_val:
					max_val = val
			if "Passivo" in polos:
				passivo_count += 1
			elif "Ativo" in polos:
				ativo_count += 1

		orgao = p.get("orgaoJulgador") if isinstance(p.get("orgaoJulgador"), dict) else {}
		tribunal_name = p.get("tribunal") or orgao.get("tribunal") or orgao.get("vara") or "—"
		grau = p.get("grau") or (p.get("instancia", {}).get("grauInstancia") if isinstance(p.get("instancia"), dict) else "")
		tribunal_str = f"{tribunal_name}{(' · ' + str(grau) + 'ºG') if grau else ''}"

		classif = p.get("classificacao") if isinstance(p.get("classificacao"), dict) else {}
		classe_str = str(p.get("classe") or classif.get("descricao") or "").strip()
		assunto_str = str(p.get("assuntoPrincipal") or p.get("assunto") or "").strip()
		if not assunto_str and isinstance(p.get("assuntos"), list):
			assuntos_list = [str(a.get("assunto") or "").strip() for a in p["assuntos"] if isinstance(a, dict) and a.get("assunto")]
			assunto_str = ", ".join(assuntos_list[:2])

		ult = p.get("ultimaMovimentacao")
		ult_str = ""
		if isinstance(ult, dict):
			ult_str = f"{str(ult.get('data') or '')[:10]} · {str(ult.get('descricao') or '')[:40]}".strip(" ·")

		data_ini = str(p.get("dataAjuizamento") or p.get("dataInicio") or "")[:10]
		status_proc = str(p.get("status") or p.get("statusProcesso") or "")
		penhora_tag = f" [⚠ {len(penhoras)} Penhora(s)]" if penhoras else ""
		homonym_tag = " [Possível Homônimo]" if has_only_agregado else ""

		parsed_rows.append(
			{
				"val_sort": val if not has_only_agregado else -1.0,
				"numero": str(p.get("numeroProcesso") or p.get("numero_cnj") or "—"),
				"tribunal": tribunal_str,
				"assunto": (" / ".join(filter(None, [classe_str, assunto_str]))[:75] or "—") + penhora_tag + homonym_tag,
				"polo": ("/".join(sorted(polos)) or "—") + (" (Homônimo?)" if has_only_agregado else ""),
				"valor": _fmt_brl(val) if val > 0 else "—",
				"data_status": " · ".join(filter(None, [data_ini, status_proc, ult_str]))[:65] or "—",
			}
		)

	parsed_rows.sort(key=lambda r: r["val_sort"], reverse=True)
	shown = parsed_rows[:MAX_TABLE_ITEMS]

	parts = [
		'<table class="kpi-table"><tr>',
		f'<td><span class="kpi-val">{total}</span><span class="kpi-lbl">Total de Processos</span></td>',
		f'<td><span class="kpi-val">{passivo_count}</span><span class="kpi-lbl">Polo Passivo Confirmado</span></td>',
		f'<td><span class="kpi-val">{_fmt_brl(total_val)}</span><span class="kpi-lbl">Soma dos Valores</span></td>',
		f'<td><span class="kpi-val">{_fmt_brl(max_val)}</span><span class="kpi-lbl">Maior Valor de Causa</span></td>',
		f'<td><span class="kpi-val">{penhoras_total}</span><span class="kpi-lbl">Penhoras / Bloqueios</span></td>',
		"</tr></table>",
	]
	if penhora_alerts:
		parts.append(f'<div class="alert-box">⚠ Alerta de Penhora/Execução: {"; ".join(escape(a) for a in penhora_alerts[:5])}</div>')
	if homonym_count:
		parts.append(
			f'<div class="note">ℹ {homonym_count} processo(s) vieram com documento inferido por nome (origemDocumento=AGREGADO) e foram separados da contagem de réu confirmado para evitar falso positivo de homônimo.</div>'
		)
	parts.extend(
		[
			"<table><thead><tr>",
			"<th>Nº Processo (CNJ)</th><th>Tribunal</th><th>Classe / Assunto Principal</th><th>Polo</th><th>Valor</th><th>Ajuizamento / Status</th>",
			"</tr></thead><tbody>",
		]
	)
	for r in shown:
		parts.append(
			f"<tr><td>{escape(r['numero'])}</td><td>{escape(r['tribunal'])}</td>"
			f"<td>{escape(r['assunto'])}</td><td>{escape(r['polo'])}</td>"
			f"<td>{escape(r['valor'])}</td><td>{escape(r['data_status'])}</td></tr>"
		)
	parts.append("</tbody></table>")
	if len(parsed_rows) > MAX_TABLE_ITEMS:
		extra = len(parsed_rows) - MAX_TABLE_ITEMS
		parts.append(
			f'<div class="muted">+ {extra} processo(s) adicional(is) de menor valor material omitido(s) na versão impressa para compacidade (disponíveis na íntegra no arquivo JSON de auditoria).</div>'
		)
	return "".join(parts)


def _render_scr_section(node: dict[str, Any]) -> str:
	"""Renderização executiva compacta para SCR Analítico - Resumo BACEN."""
	cart = node.get("carteiraCredito") or {}
	ind = node.get("indice") or {}
	p_cat = node.get("percentualCategoria") or {}
	p_venc = node.get("percentualVencido") or {}
	p_prazo = node.get("percentualPrazo") or {}

	parts = [
		'<table class="kpi-table"><tr>',
		f'<td><span class="kpi-val">{escape(format_value(node.get("score")))}</span><span class="kpi-lbl">Score SCR</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(node.get("classeRisco")))}</span><span class="kpi-lbl">Classe de Risco</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(node.get("situacao")))} ({escape(format_value(node.get("volume")))})</span><span class="kpi-lbl">Situação / Volume</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(node.get("quantidadeInstituicoes")))} inst. / {escape(format_value(node.get("quantidadeOperacoes")))} op.</span><span class="kpi-lbl">Relacionamento SFN</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(cart.get("valorVencer")))}</span><span class="kpi-lbl">Carteira a Vencer</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(cart.get("valorVencida")))}</span><span class="kpi-lbl">Carteira Vencida</span></td>',
		"</tr></table>",
	]

	pairs: list[tuple[str, str]] = []
	for k in ("relacionamentos", "perfil"):
		if node.get(k):
			pairs.append((_label_html(k), escape(format_value(node.get(k)))))
	for k, v in ind.items():
		pairs.append((f"Índice · {_label_html(k)}", escape(format_value(v))))
	if p_prazo:
		prazo_str = f"Curto: {p_prazo.get('curto', '0%')} · Médio: {p_prazo.get('medio', '0%')} · Longo: {p_prazo.get('longo', '0%')}"
		pairs.append(("Distribuição por Prazo", escape(prazo_str)))

	active_cats = [f"{label_for(k)[0]}: {v}" for k, v in p_cat.items() if str(v).strip() not in ("0%", "0", "", "None")]
	zero_cats = [label_for(k)[0] for k, v in p_cat.items() if str(v).strip() in ("0%", "0")]
	if active_cats:
		pairs.append(("Modalidades Ativas (>0%)", escape(" · ".join(active_cats))))
	if zero_cats:
		pairs.append(("Modalidades Zeradas (0%)", escape(", ".join(zero_cats))))

	active_venc = [f"{label_for(k)[0]}: {v}" for k, v in p_venc.items() if str(v).strip() not in ("0%", "0", "", "None")]
	pairs.append(
		("Exposição Vencida por Linha", escape(" · ".join(active_venc) if active_venc else "0% em todas as modalidades"))
	)
	parts.append(_kv_grid_html(pairs, two_columns_per_row=True))
	return "".join(parts)


def _render_boavista_header(node: dict[str, Any]) -> str:
	"""Cabeçalho executivo para Boa Vista Risco Positivo PJ ou Acerta Completo Positivo PF."""
	scores = node.get("scores") or {}
	sc_pos = scores.get("scorePositivo") or {}
	ind_neg = node.get("indicadoresNegocio") or {}
	fat_node = ind_neg.get("faturamentoPresumido") if isinstance(ind_neg.get("faturamentoPresumido"), dict) else {}
	lim_node = ind_neg.get("limiteCredito") if isinstance(ind_neg.get("limiteCredito"), dict) else {}
	renda_node = ind_neg.get("rendaPresumida") if isinstance(ind_neg.get("rendaPresumida"), dict) else {}
	cons_ant = node.get("consultasAnteriores") if isinstance(node.get("consultasAnteriores"), dict) else {}
	prot = node.get("protestos") if isinstance(node.get("protestos"), dict) else {}
	pend = node.get("pendenciasRestricoes") if isinstance(node.get("pendenciasRestricoes"), dict) else {}

	score_val = sc_pos.get("pontuacao") or sc_pos.get("score") or node.get("score") or "—"
	classif = sc_pos.get("classificacao_alfabetica") or sc_pos.get("probabilidade") or sc_pos.get("texto") or "—"
	fat_or_renda = fat_node.get("valor") or renda_node.get("valor") or node.get("rendaPresumida") or "—"
	fat_lbl = "Faturamento Presumido" if fat_node.get("valor") else "Renda Presumida"
	limite = lim_node.get("valor") or "—"
	cons_30 = cons_ant.get("ultimos30Dias") if cons_ant.get("ultimos30Dias") is not None else "0"
	cons_90 = cons_ant.get("de31a90Dias") if cons_ant.get("de31a90Dias") is not None else "0"
	prot_qtd = prot.get("quantidadeTotal") or 0
	prot_val = prot.get("valorTotal") or "0,00"
	pend_qtd = pend.get("quantidadeTotal") or 0

	parts = [
		'<table class="kpi-table"><tr>',
		f'<td><span class="kpi-val">{escape(format_value(score_val))}</span><span class="kpi-lbl">Score Positivo ({escape(str(classif)[:24])})</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(fat_or_renda))}</span><span class="kpi-lbl">{escape(fat_lbl)}</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(limite))}</span><span class="kpi-lbl">Limite Sugerido Bureau</span></td>',
		f'<td><span class="kpi-val">{escape(str(prot_qtd))} ({escape(str(prot_val))})</span><span class="kpi-lbl">Protestos</span></td>',
		f'<td><span class="kpi-val">{escape(str(pend_qtd))}</span><span class="kpi-lbl">Pendências / Restrições</span></td>',
		f'<td><span class="kpi-val">{escape(str(cons_30))} (30d) / {escape(str(cons_90))} (90d)</span><span class="kpi-lbl">Buscas no Mercado</span></td>',
		"</tr></table>",
	]
	try:
		if int(_parse_num(cons_30)) >= 8:
			parts.append(
				f'<div class="alert-box">⚠ Alerta de Busca Acelerada de Crédito: {escape(str(cons_30))} consultas no mercado nos últimos 30 dias (possível estresse de caixa ou tentativa de alavancagem simultânea).</div>'
			)
	except Exception:
		pass
	return "".join(parts)


def _render_quod_header(sub: dict[str, Any]) -> str:
	"""Cabeçalho executivo para Dossiê QUOD Completo (PJ ou PF)."""
	sc_block = sub.get("scoreEntidades") or sub.get("scoreEntidade") or {}
	sc = (sc_block.get("scoreCredito") if isinstance(sc_block, dict) else {}) or {}
	pend = sub.get("pendenciaFinanceira") if isinstance(sub.get("pendenciaFinanceira"), dict) else {}
	prots = pend.get("protestos") if isinstance(pend.get("protestos"), list) else []
	negs = pend.get("negativacoes") if isinstance(pend.get("negativacoes"), list) else []
	hist = sub.get("historicoConsulta") if isinstance(sub.get("historicoConsulta"), dict) else {}
	cons_30 = hist.get("ultimos30Dias") if hist.get("ultimos30Dias") is not None else "0"
	freq_pag = sub.get("frequenciaPagamentos") or sc_block.get("capacidadePagamento") or "—"

	parts = [
		'<table class="kpi-table"><tr>',
		f'<td><span class="kpi-val">{escape(format_value(sc.get("score")))}</span><span class="kpi-lbl">Score QUOD ({escape(format_value(sc.get("faixaScore")))})</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(sc.get("probabilidadeInadimplencia")))}</span><span class="kpi-lbl">Prob. Inadimplência</span></td>',
		f'<td><span class="kpi-val">{escape(format_value(freq_pag))}</span><span class="kpi-lbl">Pontualidade / Capacidade</span></td>',
		f'<td><span class="kpi-val">{len(prots)}</span><span class="kpi-lbl">Protestos Detalhados</span></td>',
		f'<td><span class="kpi-val">{len(negs)} ({escape(format_value(pend.get("totalPendencia")))})</span><span class="kpi-lbl">Negativações / Total</span></td>',
		f'<td><span class="kpi-val">{escape(str(cons_30))}</span><span class="kpi-lbl">Consultas 30 Dias</span></td>',
		"</tr></table>",
	]
	try:
		if int(_parse_num(cons_30)) >= 8:
			parts.append(
				f'<div class="alert-box">⚠ Alerta de Busca Acelerada de Crédito: {escape(str(cons_30))} consultas ao documento nos últimos 30 dias.</div>'
			)
	except Exception:
		pass
	return "".join(parts)


def _is_empty_or_zero(val: Any) -> bool:
	if val is None or val is False or val == "" or val == [] or val == {}:
		return True
	if isinstance(val, (int, float)) and val == 0:
		return True
	if isinstance(val, str) and val.strip().lower() in ("0", "0,00", "r$ 0,00", "nao consta", "não consta", "nada consta", "nenhum", "false", "sem apontamentos"):
		return True
	return False


def _render_dict_list_table(items: list[dict[str, Any]], max_items: int = MAX_TABLE_ITEMS) -> str:
	"""Converte uma lista de objetos JSON homogêneos em uma tabela horizontal compacta."""
	dicts = [item for item in items if isinstance(item, dict) and item]
	if not dicts:
		return f'<div class="muted">{escape(_("(empty)"))}</div>'

	# Seleciona até 7 colunas escalares mais relevantes
	cols: list[str] = []
	for d in dicts[:10]:
		for k, v in d.items():
			if k not in cols and not isinstance(v, (dict, list)):
				cols.append(k)
			elif k not in cols and isinstance(v, dict) and len(v) <= 3:
				cols.append(k)
	cols = cols[:7]
	if not cols:
		return ""

	shown = dicts[:max_items]
	out = ["<table><thead><tr>"]
	for c in cols:
		out.append(f"<th>{_label_html(c)}</th>")
	out.append("</tr></thead><tbody>")
	for d in shown:
		out.append("<tr>")
		for c in cols:
			v = d.get(c)
			if isinstance(v, dict):
				cell = ", ".join(f"{format_value(sv)}" for sv in v.values() if sv not in (None, ""))
			elif isinstance(v, list):
				cell = f"{len(v)} reg."
			else:
				cell = format_value(v)
			out.append(f"<td>{escape(str(cell)[:90])}</td>")
		out.append("</tr>")
	out.append("</tbody></table>")
	if len(dicts) > max_items:
		out.append(
			f'<div class="muted">+ {len(dicts) - max_items} registro(s) adicional(is) omitido(s) para compacidade (disponíveis no JSON anexo).</div>'
		)
	return "".join(out)


def _format_address_inline(addr: dict[str, Any]) -> str:
	parts = [
		addr.get("logradouro") or addr.get("street") or "",
		addr.get("numero") or addr.get("number") or "",
		addr.get("complemento") or "",
		addr.get("bairro") or addr.get("district") or "",
		f"{addr.get('cidade') or addr.get('city') or ''}/{addr.get('uf') or addr.get('state') or ''}".strip("/"),
		f"CEP {addr.get('cep') or addr.get('zipCode') or ''}".strip(),
	]
	return ", ".join(str(p).strip() for p in parts if str(p).strip() and str(p).strip() != "CEP")


def _render_compact_node(node: Any, depth: int = 0) -> str:
	"""Renderizador recursivo compacto: agrupa escalares em grid de 4 colunas e listas de objetos em tabelas horizontais."""
	if not isinstance(node, (dict, list)):
		return f"<p>{escape(format_value(node))}</p>"

	if isinstance(node, list):
		if not node:
			return f'<div class="muted">{escape(_("(empty)"))}</div>'
		if all(isinstance(x, dict) for x in node):
			return _render_dict_list_table(node)
		joined = " · ".join(escape(format_value(x)) for x in node[:30] if not isinstance(x, (dict, list)))
		return f'<div class="chip-list">{joined}</div>'

	# Detecta estruturas especializadas
	if "processos" in node and isinstance(node.get("processos"), list):
		return _render_lawsuits_section(node)
	if "carteiraCredito" in node and "percentualCategoria" in node:
		return _render_scr_section(node)
	if "entidadeJuridica" in node or "entidadeFisica" in node:
		sub = node.get("entidadeJuridica") or node.get("entidadeFisica") or {}
		if isinstance(sub, dict):
			header = _render_quod_header(sub) if depth == 0 else ""
			return header + _render_compact_node(sub, depth + 1)

	header_prefix = ""
	if depth == 0 and ("scores" in node and ("indicadoresNegocio" in node or "pendenciasRestricoes" in node)):
		header_prefix = _render_boavista_header(node)

	scalar_pairs: list[tuple[str, str]] = []
	sub_blocks: list[str] = []

	for key, value in node.items():
		if key in TECHNICAL_META_KEYS:
			continue
		# Omite listas vazias ou objetos nulos secundários em profundidades > 0 para não poluir
		if depth > 0 and value in (None, [], {}):
			continue

		# Endereço inline em 1 campo só
		if key.lower() in ("endereco", "address") and isinstance(value, dict):
			scalar_pairs.append((_label_html(key), escape(_format_address_inline(value) or "—")))
			continue

		# Filiais (outrasEmpresas) em chips compactos
		if key == "outrasEmpresas" and isinstance(value, list):
			if value:
				chips = [
					f"{item.get('documento', '')} ({str(item.get('dataFundacao', ''))[:10]})".strip(" ()")
					for item in value[:12]
					if isinstance(item, dict)
				]
				more = f" (+{len(value) - 12} filiais)" if len(value) > 12 else ""
				sub_blocks.append(
					f'<h3>{_label_html(key)} ({len(value)})</h3><div class="chip-list">{escape(" · ".join(chips) + more)}</div>'
				)
			continue

		if isinstance(value, dict) and value:
			# Se o sub-dicionário inteiro for de pendências zeradas, resume em 1 linha
			if key in ("pendenciaFinanceira", "protestos", "chequesSemFundo", "acoesJudiciais") and all(
				_is_empty_or_zero(v) for v in value.values()
			):
				scalar_pairs.append((_label_html(key), escape("Nada consta (0 ocorrências)")))
				continue
			sub_html = _render_compact_node(value, depth + 1)
			if sub_html:
				sub_blocks.append(f"<h3>{_label_html(key)}</h3>{sub_html}")
		elif isinstance(value, list) and value:
			if all(isinstance(x, dict) for x in value):
				sub_blocks.append(f"<h3>{_label_html(key)} ({len(value)})</h3>{_render_dict_list_table(value)}")
			else:
				joined = "; ".join(format_value(x) for x in value[:20])
				scalar_pairs.append((_label_html(key), escape(joined)))
		else:
			shown = _("(empty)") if isinstance(value, (dict, list)) else format_value(value)
			scalar_pairs.append((_label_html(key), escape(shown)))

	out: list[str] = [header_prefix] if header_prefix else []
	if scalar_pairs:
		out.append(_kv_grid_html(scalar_pairs, two_columns_per_row=(len(scalar_pairs) >= 4)))
	out.extend(sub_blocks)
	return "".join(out)


def render_query_html(bureau_or_product: str, payload: Any) -> str:
	"""Renderiza o retorno de uma consulta individual em HTML executivo escopado para visualização na tela (modal)."""
	frappe.local.lang = "pt-BR"
	if getattr(frappe.local, "job", None) is not None:
		frappe.local.job.lang_resolved = True
	cleaned = _unwrap_payload(payload)
	body = _render_compact_node(cleaned)
	return f'<div class="ct-dossier-view"><style>{SCOPED_VIEW_CSS}</style>{body}</div>'


def summarize_highlights(organization_name: str) -> dict[str, Any]:
	"""Consolida os indicadores-chave e alertas de fraude de todas as consultas já realizadas para o painel executivo."""
	ctx = collect(organization_name)
	org = frappe.get_doc(ORG, organization_name)
	highlights: dict[str, Any] = {
		"scores": [],
		"revenue_presumed": None,
		"credit_limit_suggested": None,
		"scr_due": None,
		"scr_overdue": None,
		"scr_risk_class": None,
		"scr_institutions": None,
		"protests_count": 0,
		"protests_amount": 0.0,
		"negativations_count": 0,
		"lawsuits_defendant": 0,
		"penhoras_count": 0,
		"inquiries_30d": 0,
		"fraud_alerts": [],
	}

	company_age_days = 0
	if org.get("activity_start_date"):
		try:
			company_age_days = date_diff(today(), getdate(org.activity_start_date))
		except Exception:
			company_age_days = 0

	for sec in ctx["sections"]:
		payload = _unwrap_payload(sec["payload"])
		if not isinstance(payload, dict):
			continue
		subj = sec["subject"]
		prod = sec["product"]

		# Dossiê QUOD
		sub_quod = payload.get("entidadeJuridica") or payload.get("entidadeFisica")
		if isinstance(sub_quod, dict):
			sc_b = sub_quod.get("scoreEntidades") or sub_quod.get("scoreEntidade") or {}
			sc_c = (sc_b.get("scoreCredito") if isinstance(sc_b, dict) else {}) or {}
			if sc_c.get("score") is not None:
				highlights["scores"].append(
					{
						"source": "QUOD",
						"subject": subj,
						"score": sc_c.get("score"),
						"band": sc_c.get("faixaScore") or sc_c.get("probabilidadeInadimplencia") or "",
					}
				)
			pend = sub_quod.get("pendenciaFinanceira") if isinstance(sub_quod.get("pendenciaFinanceira"), dict) else {}
			prots = pend.get("protestos") if isinstance(pend.get("protestos"), list) else []
			negs = pend.get("negativacoes") if isinstance(pend.get("negativacoes"), list) else []
			highlights["protests_count"] = max(highlights["protests_count"], len(prots))
			highlights["negativations_count"] = max(highlights["negativations_count"], len(negs))
			hist = sub_quod.get("historicoConsulta") if isinstance(sub_quod.get("historicoConsulta"), dict) else {}
			c30 = int(_parse_num(hist.get("ultimos30Dias")))
			if c30 > highlights["inquiries_30d"]:
				highlights["inquiries_30d"] = c30

		# Boa Vista PJ / PF
		if "scores" in payload and isinstance(payload.get("scores"), dict):
			sc_pos = payload["scores"].get("scorePositivo") or {}
			pt = sc_pos.get("pontuacao") or sc_pos.get("score")
			if pt is not None:
				highlights["scores"].append(
					{
						"source": "Boa Vista",
						"subject": subj,
						"score": pt,
						"band": sc_pos.get("classificacao_alfabetica") or sc_pos.get("probabilidade") or "",
					}
				)
			ind_neg = payload.get("indicadoresNegocio") if isinstance(payload.get("indicadoresNegocio"), dict) else {}
			fat = ind_neg.get("faturamentoPresumido") if isinstance(ind_neg.get("faturamentoPresumido"), dict) else {}
			lim = ind_neg.get("limiteCredito") if isinstance(ind_neg.get("limiteCredito"), dict) else {}
			if fat.get("valor") and not highlights["revenue_presumed"]:
				highlights["revenue_presumed"] = fat.get("valor")
			if lim.get("valor") and not highlights["credit_limit_suggested"]:
				highlights["credit_limit_suggested"] = lim.get("valor")
			cons_ant = payload.get("consultasAnteriores") if isinstance(payload.get("consultasAnteriores"), dict) else {}
			c30 = int(_parse_num(cons_ant.get("ultimos30Dias")))
			if c30 > highlights["inquiries_30d"]:
				highlights["inquiries_30d"] = c30

			# Checa entrada recente de sócio em empresa antiga
			qsa_bv = ((payload.get("quadroSocietario") or {}).get("socios") if isinstance(payload.get("quadroSocietario"), dict) else None) or []
			if company_age_days > 730 and isinstance(qsa_bv, list):
				for socio in qsa_bv:
					if isinstance(socio, dict) and socio.get("dataEntrada"):
						try:
							days_in = date_diff(today(), getdate(str(socio["dataEntrada"])[:10]))
							if 0 <= days_in <= 180:
								msg = f"Alteração societária recente: sócio {socio.get('nome', '')} ingressou há {days_in} dias em CNPJ com {company_age_days // 365} anos."
								if msg not in highlights["fraud_alerts"]:
									highlights["fraud_alerts"].append(msg)
						except Exception:
							pass

		# SCR Bacen
		if "carteiraCredito" in payload and isinstance(payload.get("carteiraCredito"), dict):
			cart = payload["carteiraCredito"]
			highlights["scr_due"] = cart.get("valorVencer")
			highlights["scr_overdue"] = cart.get("valorVencida")
			highlights["scr_risk_class"] = payload.get("classeRisco")
			highlights["scr_institutions"] = payload.get("quantidadeInstituicoes")

		# Processos Judiciais
		if "processos" in payload and isinstance(payload.get("processos"), list):
			for pr in payload["processos"]:
				if isinstance(pr, dict):
					det = pr.get("detalhesStatusProcesso") if isinstance(pr.get("detalhesStatusProcesso"), dict) else {}
					pen = det.get("penhoras") if isinstance(det.get("penhoras"), list) else []
					highlights["penhoras_count"] += len(pen)

		# Serasa
		if "reports" in payload and isinstance(payload.get("reports"), list) and payload["reports"]:
			rep0 = payload["reports"][0] if isinstance(payload["reports"][0], dict) else {}
			sc_s = rep0.get("score") if isinstance(rep0.get("score"), dict) else {}
			if sc_s.get("score") is not None:
				highlights["scores"].append(
					{
						"source": "Serasa",
						"subject": subj,
						"score": sc_s.get("score"),
						"band": sc_s.get("message") or "",
					}
				)
		elif "OptionalFeatures" in payload and isinstance(payload.get("OptionalFeatures"), dict):
			sc_s = payload["OptionalFeatures"].get("Score") or {}
			if sc_s.get("Score") is not None:
				highlights["scores"].append(
					{
						"source": "Serasa",
						"subject": subj,
						"score": sc_s.get("Score"),
						"band": sc_s.get("Message") or "",
					}
				)

		# Produtinhos avulsos
		if prod and "Score" in prod and payload.get("pessoaJuridica", {}).get("score"):
			highlights["scores"].append(
				{
					"source": "QUOD Score",
					"subject": subj,
					"score": payload["pessoaJuridica"]["score"],
					"band": payload["pessoaJuridica"].get("faixaScore") or "",
				}
			)

	if highlights["inquiries_30d"] >= 8:
		msg = f"Volume atípico de buscas de crédito: {highlights['inquiries_30d']} consultas ao documento nos últimos 30 dias."
		if msg not in highlights["fraud_alerts"]:
			highlights["fraud_alerts"].append(msg)
	if highlights["penhoras_count"] > 0:
		msg = f"Foram identificadas {highlights['penhoras_count']} penhora(s)/bloqueio(s) em processos judiciais."
		if msg not in highlights["fraud_alerts"]:
			highlights["fraud_alerts"].append(msg)

	return highlights


def build_html(ctx: dict[str, Any], generated_by: str, generated_on: str) -> str:
	frappe.local.lang = "pt-BR"
	if getattr(frappe.local, "job", None) is not None:
		frappe.local.job.lang_resolved = True
	parts = [f"<style>{CSS}</style>"]
	parts.append('<div class="header-banner">')
	parts.append(f"<h1>{escape(_('Credit dossier: query returns'))}</h1>")
	parts.append(f'<div class="sub">{escape(ctx["organization"])} · CNPJ {escape(ctx["cnpj"])}</div>')
	parts.append(
		f'<div class="meta">{escape(_("Generated on"))} {escape(generated_on)} · {escape(_("by"))} '
		f"{escape(generated_by)} · {len(ctx['sections'])} {escape(_('queries'))}</div>"
	)
	parts.append("</div>")

	parts.append(
		f'<div class="note">{escape(_("This document reproduces what each provider returned, field by field, without changes. It contains no credit recommendation."))}</div>'
	)
	if not ctx["complete"]:
		parts.append(
			f'<div class="note">{escape(_("Some automatic queries were saved before the complete return was kept: for them this dossier shows only the summary the system stored. Run the analysis again to get the complete return."))}</div>'
		)
	if ctx["failures"]:
		items = "".join(f"<li>{escape(item)}</li>" for item in ctx["failures"])
		parts.append(f'<div class="note"><b>{escape(_("Queries that failed"))}:</b><ul>{items}</ul></div>')

	if ctx["partners"]:
		parts.append(f"<h2>{escape(_('Partners'))}</h2>")
		parts.append("<table><thead><tr><th>Nome / Sócio</th><th>CPF / Documento</th><th>Qualificação / Cargo</th><th>Participação</th></tr></thead><tbody>")
		for partner in ctx["partners"]:
			cpf = partner["cpf"] or _("CPF not filled")
			share = f"{partner['share']}%" if partner.get("share") is not None else "—"
			role = partner.get("designation") or "—"
			parts.append(
				f"<tr><td>{escape(partner['name'])}</td><td>{escape(cpf)}</td><td>{escape(role)}</td><td>{escape(share)}</td></tr>"
			)
		parts.append("</tbody></table>")

	if ctx["sections"]:
		parts.append(f"<h2>{escape(_('Contents'))}</h2>")
		parts.append("<table><thead><tr><th>#</th><th>Provedor</th><th>Produto / Consulta</th><th>Consultado</th><th>Documento</th><th>Data/Hora</th></tr></thead><tbody>")
		for number, section in enumerate(ctx["sections"], start=1):
			parts.append(
				f"<tr><td>{number}</td><td>{escape(section['provider'])}</td><td>{escape(section['product'])}</td>"
				f"<td>{escape(section['subject'])}</td><td>{escape(section['document'])}</td><td>{escape(section['when'])}</td></tr>"
			)
		parts.append("</tbody></table>")

	for number, section in enumerate(ctx["sections"], start=1):
		parts.append(f"<h2>{number}. {escape(section['provider'])}: {escape(section['product'])}</h2>")
		parts.append(
			f'<div class="sec-meta">{escape(_("Queried"))}: <b>{escape(section["subject"])}</b> · {escape(section["document"])} · '
			f"{escape(section['when'])}</div>"
		)
		if section["summary_only"]:
			parts.append(
				f'<div class="note">{escape(_("Summary only: the complete return of this query was not kept."))}</div>'
			)
		cleaned_payload = _unwrap_payload(section["payload"])
		parts.append(_render_compact_node(cleaned_payload))

	if not ctx["sections"]:
		parts.append(f'<div class="note">{escape(_("No query has been made for this organization yet."))}</div>')
	return "".join(parts)


def json_bundle(ctx: dict[str, Any], generated_by: str, generated_on: str) -> str:
	"""Os retornos exatamente como guardados, em um JSON só: a prova do que o PDF mostra."""
	return json.dumps(
		{
			"organization": ctx["organization"],
			"cnpj": ctx["cnpj"],
			"generated_on": generated_on,
			"generated_by": generated_by,
			"queries": [
				{
					"provider": section["provider"],
					"product": section["product"],
					"queried": section["subject"],
					"document": section["document"],
					"when": section["when"],
					"summary_only": section["summary_only"],
					"payload": section["payload"],
				}
				for section in ctx["sections"]
			],
		},
		ensure_ascii=False,
		indent=2,
	)


def _safe_attach_and_comment(
	organization_name: str,
	deal_name: str | None,
	files: tuple[Any, ...],
	comment_text: str,
) -> None:
	"""Anexa os arquivos ao Card (se houver) e registra na timeline de forma isolada contra lock concorrente (1020)."""
	try:
		if deal_name and frappe.db.exists("CRM Deal", deal_name):
			for file_doc in files:
				frappe.get_doc(
					{
						"doctype": "File",
						"file_name": file_doc.file_name,
						"file_url": file_doc.file_url,
						"attached_to_doctype": "CRM Deal",
						"attached_to_name": deal_name,
						"is_private": 1,
					}
				).insert(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
	except Exception:
		frappe.db.rollback()

	for _attempt in range(2):
		try:
			target = (
				frappe.get_doc("CRM Deal", deal_name)
				if (deal_name and frappe.db.exists("CRM Deal", deal_name))
				else frappe.get_doc(ORG, organization_name)
			)
			target.add_comment("Comment", comment_text)
			break
		except Exception:
			frappe.db.rollback()


def generate(organization_name: str, deal_name: str | None = None) -> dict[str, Any]:
	"""Gera o PDF e o JSON, anexa na Organização (e no card, se vier de um) e registra na linha do tempo."""
	from frappe.utils.file_manager import save_file
	from frappe.utils.pdf import get_pdf

	frappe.local.lang = "pt-BR"
	if getattr(frappe.local, "job", None) is not None:
		frappe.local.job.lang_resolved = True
	ctx = collect(organization_name)
	if not ctx["sections"]:
		frappe.throw(_("No query has been made for this organization yet. Run the analysis or a bureau query first."))

	now = now_datetime()
	generated_on = format_datetime(now)
	generated_by = frappe.utils.get_fullname(frappe.session.user) or frappe.session.user
	stamp = now.strftime("%Y%m%d-%H%M%S")
	base = f"{FILE_PREFIX}{only_digits(ctx['cnpj']) or organization_name}-{stamp}"

	pdf = get_pdf(
		build_html(ctx, generated_by, generated_on),
		{"page-size": "A4", "margin-top": "10mm", "margin-bottom": "10mm", "margin-left": "12mm", "margin-right": "12mm"},
	)
	pdf_file = save_file(f"{base}.pdf", pdf, ORG, organization_name, is_private=1)
	json_file = save_file(
		f"{base}.json", json_bundle(ctx, generated_by, generated_on).encode("utf-8"), ORG, organization_name, is_private=1
	)

	comment_text = _("Credit dossier (query returns) generated by {0}: {1} queries. File: {2}").format(
		generated_by, len(ctx["sections"]), pdf_file.file_name
	)
	_safe_attach_and_comment(organization_name, deal_name, (pdf_file, json_file), comment_text)

	return {
		"pdf_url": pdf_file.file_url,
		"pdf_name": pdf_file.file_name,
		"json_url": json_file.file_url,
		"json_name": json_file.file_name,
		"queries": len(ctx["sections"]),
		"complete": ctx["complete"],
	}


def list_files(organization_name: str) -> list[dict[str, Any]]:
	"""Os dossiês já gerados (PDF e JSON), do mais novo para o mais antigo."""
	return frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": ORG,
			"attached_to_name": organization_name,
			"file_name": ("like", f"{FILE_PREFIX}%"),
		},
		fields=["name", "file_name", "file_url", "creation", "owner"],
		order_by="creation desc",
		limit_page_length=50,
	)
