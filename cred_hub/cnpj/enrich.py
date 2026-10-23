"""Job em segundo plano: consulta o CNPJ, preenche os campos da Receita e cadastra os sócios."""

import frappe
from frappe import _
from frappe.utils import now_datetime

from cred_hub.cnpj import client
from cred_hub.cnpj.fields import RECEITA_FIELDS
from cred_hub.cnpj.mapping import only_digits

_PARTICULAS = {"da", "de", "do", "das", "dos", "e", "di", "du"}


def person_name(raw: str) -> str:
	"""ANA MARIA DA SILVA -> Ana Maria da Silva"""
	palavras = raw.strip().lower().split()
	return " ".join(p if i and p in _PARTICULAS else p.capitalize() for i, p in enumerate(palavras))


def enrich_doc(doctype, name, cnpj):
	if not frappe.db.exists(doctype, name):
		return
	doc = frappe.get_doc(doctype, name)
	if only_digits(doc.get("cnpj")) != cnpj:
		# O CNPJ mudou de novo antes do job rodar; o novo job cuida dele
		return

	try:
		data = client.fetch(cnpj)
	except client.CnpjNotFound:
		_finish_without_data(doc, _("CNPJ {0} não encontrado na Receita (OpenCNPJ/BrasilAPI).").format(doc.cnpj))
		return
	except client.CnpjUnavailable as e:
		frappe.log_error(title=f"Cred Hub: consulta de CNPJ falhou ({doc.cnpj})", message=str(e))
		_finish_without_data(doc, _("Não foi possível consultar o CNPJ {0} agora. Salve de novo mais tarde para tentar outra vez.").format(doc.cnpj), retry=True)
		return

	for field, key in RECEITA_FIELDS.items():
		doc.set(field, data.get(key))
	if doctype == "CRM Lead" and not doc.get("organization"):
		doc.organization = data.get("nome_fantasia") or data.get("razao_social")
	doc.cnpj_consultado_em = now_datetime()
	doc.flags.cred_hub_enriching = True
	doc.save(ignore_permissions=True)

	socios = add_partners(doc, data.get("socios") or []) if doctype == "CRM Organization" else 0
	resumo = _("Dados do CNPJ {0} preenchidos automaticamente (fonte: {1}).").format(doc.cnpj, data.get("fonte"))
	if socios:
		resumo += " " + _("{0} sócio(s) cadastrado(s) como contato.").format(socios)
	doc.add_comment("Comment", resumo)


def _finish_without_data(doc, message, retry=False):
	if not retry:
		# Marca como consultado para não repetir a cada salvamento; mudar o CNPJ consulta de novo
		doc.db_set("cnpj_consultado_em", now_datetime(), update_modified=False)
	doc.add_comment("Comment", message)


def add_partners(org, socios) -> int:
	"""Cria um Contato por sócio, ligado à Organização (campo company_name). Não duplica."""
	created = 0
	existentes = {
		c.full_name.strip().lower()
		for c in frappe.get_all("Contact", filters={"company_name": org.name}, fields=["full_name"])
		if c.full_name
	}
	for socio in socios:
		nome = person_name(socio["nome"])
		if nome.lower() in existentes:
			continue
		contact = frappe.new_doc("Contact")
		contact.first_name = nome
		contact.company_name = org.name
		contact.designation = socio.get("qualificacao") or _("Sócio")
		contact.flags.ignore_mandatory = True
		contact.insert(ignore_permissions=True)
		existentes.add(nome.lower())
		created += 1
	return created
