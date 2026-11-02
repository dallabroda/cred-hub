"""Dossiê de crédito, PDF 1: o retorno das consultas sem tirar nem pôr (credtech#316).

Mostra, por provedor e por documento (empresa e cada sócio), todos os campos do retorno em tabela, com nomes em
português (`labels.py`) e os valores exatamente como vieram. O JSON original de cada consulta vai junto, em um
arquivo, como prova. Não tem recomendação nenhuma: o parecer é outro documento (PDF 2).

Fontes: o relatório de crédito da Organização (consultas automáticas, com o retorno inteiro guardado por
`raw_capture.py`; relatório antigo sem retorno inteiro mostra só o resumo, e diz isso) e o histórico de consultas a
bureau final (Serasa, Boa Vista, Dossiê QUOD, SCR Bacen), com a consulta mais recente de cada produto e documento.
Cada geração cria arquivos novos (data e hora no nome); nada é sobrescrito.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

import frappe
from frappe import _
from frappe.utils import format_datetime, now_datetime

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check import partners as partners_lib
from cred_hub.credit_check.labels import label_for
from cred_hub.credit_check.premium_bureaus import PREMIUM_BUREAUS

FILE_PREFIX = "Dossie-Retorno-"
ORG = "CRM Organization"

# Nome do produto de cada chamada automática (provedor, endpoint)
PRODUCT_LABELS = {
	"score": "Score de crédito (QUOD)",
	"protestosbasica": "Protestos Nacional (IEPTB)",
	"processosjudiciaissimplificada": "Processos judiciais (simplificada)",
	"caixaregularidadeempregadorfgts": "FGTS: regularidade do empregador",
	"sintegraccc-completa": "Sintegra / CCC",
	"sintegraccc": "Sintegra / CCC",
	"receita-federal/pgfn-devedores": "Dívida Ativa da União (PGFN)",
	"receita-federal/pgfn": "Dívida Ativa da União (PGFN)",
	"envolvido/processos": "Processos judiciais",
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


def format_value(value: Any) -> str:
	"""O valor como veio, só legível: Sim/Não para booleano, traço para nulo, (vazio) para texto vazio."""
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
body { font-family: Arial, Helvetica, sans-serif; font-size: 9.5pt; color: #1a1a1a; }
h1 { font-size: 17pt; margin: 0 0 2px; }
h2 { font-size: 11.5pt; margin: 22px 0 4px; padding-bottom: 3px; border-bottom: 2px solid #6938ef; }
.sub { color: #666; font-size: 9pt; margin-bottom: 12px; }
.meta { color: #555; font-size: 8.5pt; margin-bottom: 6px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 6px; }
td { padding: 2px 6px; vertical-align: top; border-bottom: 1px solid #eee; font-size: 8.5pt; }
td.label { width: 38%; color: #444; }
td.value { word-break: break-word; }
tr.group td { background: #f4f1fe; font-weight: bold; color: #3d2a9a; }
.orig { color: #999; font-size: 7.5pt; }
.note { background: #fff7e0; border: 1px solid #f0d78c; padding: 6px 8px; margin: 8px 0; font-size: 8.5pt; }
.partner { margin: 0; padding: 0; font-size: 9pt; }
.break { page-break-before: always; }
"""


def _table(rows: list[dict[str, Any]]) -> str:
	out = ["<table>"]
	for row in rows:
		indent = f"padding-left:{6 + row['depth'] * 14}px"
		original = (
			""
			if row["translated"] or not row.get("key")
			else f' <span class="orig">({escape(str(row["key"]))})</span>'
		)
		label = f"{escape(str(row['label']))}{original}"
		if row.get("group"):
			out.append(f'<tr class="group"><td colspan="2" style="{indent}">{label}</td></tr>')
		else:
			out.append(
				f'<tr><td class="label" style="{indent}">{label}</td><td class="value">{escape(row["value"])}</td></tr>'
			)
	out.append("</table>")
	return "".join(out)


def build_html(ctx: dict[str, Any], generated_by: str, generated_on: str) -> str:
	parts = [f"<style>{CSS}</style>"]
	parts.append(f"<h1>{escape(_('Credit dossier: query returns'))}</h1>")
	parts.append(f'<div class="sub">{escape(ctx["organization"])} · {escape(ctx["cnpj"])}</div>')
	parts.append(
		f'<div class="meta">{escape(_("Generated on"))} {escape(generated_on)} · {escape(_("by"))} '
		f"{escape(generated_by)} · {len(ctx['sections'])} {escape(_('queries'))}</div>"
	)
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
		for partner in ctx["partners"]:
			cpf = partner["cpf"] or _("CPF not filled")
			share = f" · {partner['share']}%" if partner.get("share") is not None else ""
			parts.append(
				f'<p class="partner">{escape(partner["name"])} · {escape(cpf)}'
				f"{escape(partner['designation'] and ' · ' + partner['designation'] or '')}{escape(share)}</p>"
			)

	parts.append(f"<h2>{escape(_('Contents'))}</h2>")
	for number, section in enumerate(ctx["sections"], start=1):
		parts.append(
			f'<p class="partner">{number}. {escape(section["provider"])}: {escape(section["product"])} '
			f"({escape(section['subject'])})</p>"
		)

	for number, section in enumerate(ctx["sections"], start=1):
		parts.append(f'<div class="break"></div><h2>{number}. {escape(section["provider"])}: {escape(section["product"])}</h2>')
		parts.append(
			f'<div class="meta">{escape(_("Queried"))}: {escape(section["subject"])} · {escape(section["document"])} · '
			f"{escape(section['when'])}</div>"
		)
		if section["summary_only"]:
			parts.append(
				f'<div class="note">{escape(_("Summary only: the complete return of this query was not kept."))}</div>'
			)
		parts.append(_table(rows_of(section["payload"])))
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


def generate(organization_name: str, deal_name: str | None = None) -> dict[str, Any]:
	"""Gera o PDF e o JSON, anexa na Organização (e no card, se vier de um) e registra na linha do tempo."""
	from frappe.utils.file_manager import save_file
	from frappe.utils.pdf import get_pdf

	ctx = collect(organization_name)
	if not ctx["sections"]:
		frappe.throw(_("No query has been made for this organization yet. Run the analysis or a bureau query first."))

	now = now_datetime()
	generated_on = format_datetime(now)
	generated_by = frappe.utils.get_fullname(frappe.session.user) or frappe.session.user
	stamp = now.strftime("%Y%m%d-%H%M%S")
	base = f"{FILE_PREFIX}{only_digits(ctx['cnpj']) or organization_name}-{stamp}"

	pdf = get_pdf(build_html(ctx, generated_by, generated_on), {"page-size": "A4", "margin-top": "12mm", "margin-bottom": "12mm"})
	pdf_file = save_file(f"{base}.pdf", pdf, ORG, organization_name, is_private=1)
	json_file = save_file(
		f"{base}.json", json_bundle(ctx, generated_by, generated_on).encode("utf-8"), ORG, organization_name, is_private=1
	)

	if deal_name:
		for file_doc in (pdf_file, json_file):
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

	target = frappe.get_doc("CRM Deal", deal_name) if deal_name else frappe.get_doc(ORG, organization_name)
	target.add_comment(
		"Comment",
		_("Credit dossier (query returns) generated by {0}: {1} queries. File: {2}").format(
			generated_by, len(ctx["sections"]), pdf_file.file_name
		),
	)
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
		limit_page_length=60,
	)
