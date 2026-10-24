"""Eventos de Organização e Lead: formata o CNPJ ao salvar e agenda a consulta quando ele muda."""

import frappe
from frappe import _

from cred_hub.cnpj.mapping import format_cnpj, is_valid_cnpj, only_digits


def validate(doc, method=None):
	if not doc.get("cnpj"):
		return
	doc.cnpj = format_cnpj(doc.cnpj)
	if not is_valid_cnpj(doc.cnpj) and doc.has_value_changed("cnpj"):
		# Decisão do dono (24/10/2026): CNPJ inválido só gera aviso, não bloqueia o salvamento
		frappe.msgprint(
			_("O CNPJ {0} parece inválido (dígitos verificadores não conferem). Os dados da Receita não serão buscados.").format(doc.cnpj),
			indicator="orange",
			alert=True,
		)


def on_update(doc, method=None):
	if doc.flags.cred_hub_enriching or not doc.get("cnpj") or not is_valid_cnpj(doc.cnpj):
		return
	if not doc.has_value_changed("cnpj") and doc.get("cnpj_checked_at"):
		return
	digits = only_digits(doc.cnpj)
	frappe.enqueue(
		"cred_hub.cnpj.enrich.enrich_doc",
		queue="short",
		job_id=f"cred_hub_cnpj::{doc.doctype}::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		doctype=doc.doctype,
		name=doc.name,
		cnpj=digits,
	)
