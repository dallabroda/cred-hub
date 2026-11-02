"""Gatilho: ao salvar Lead ou Organização de pessoa jurídica, agenda a descoberta de site/LinkedIn/funcionários."""

import frappe

from cred_hub.cnpj.mapping import is_valid_cnpj


def company_name(doc) -> str:
	"""Nome para pesquisar: Organização usa organization_name; Lead usa o campo organization."""
	field = "organization_name" if doc.doctype == "CRM Organization" else "organization"
	return (doc.get(field) or "").strip()


def on_update(doc, method=None):
	"""Bônus: qualquer falha aqui só vai para o log e nunca impede salvar o Lead/Organização."""
	try:
		_on_update(doc)
	except Exception:
		frappe.log_error(title="Cred Hub: agendamento da presença digital falhou")


def _on_update(doc):
	if getattr(frappe.flags, "in_import", False):
		return
	name = company_name(doc)
	if doc.get("company_checked_on") or not name:
		return
	# Não buscar presença digital para Sacados/Cedentes de cobrança
	if name.endswith("-Sacado") or name.endswith("-Cedente") or str(doc.name).endswith("-Sacado") or str(doc.name).endswith("-Cedente"):
		return
	# Com CNPJ válido, espera a Receita responder (traz porte/CNAE para o piso SEBRAE); o job do
	# CNPJ salva o documento de novo e este gatilho dispara outra vez.
	if is_valid_cnpj(doc.get("cnpj") or "") and not doc.get("cnpj_checked_on"):
		return
	enqueue(doc.doctype, doc.name)


def enqueue(doctype, name):
	try:
		_enqueue(doctype, name)
	except Exception:
		frappe.log_error(title="Cred Hub: agendamento da presença digital falhou")


def _enqueue(doctype, name):
	frappe.enqueue(
		"cred_hub.company.enrich.enrich_company",
		# Várias buscas na web (segundos a minutos): fila longa, para não atrasar a consulta de CNPJ (fila curta)
		queue="long",
		job_id=f"cred_hub_company::{doctype}::{name}",
		deduplicate=True,
		enqueue_after_commit=True,
		doctype=doctype,
		name=name,
	)
