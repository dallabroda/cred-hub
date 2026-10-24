"""Job em segundo plano: consulta o CNPJ, preenche os campos da Receita, o endereço e o setor, e cadastra os sócios."""

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, today

from cred_hub.cnpj import client
from cred_hub.cnpj.fields import DOCTYPES, RECEITA_FIELDS
from cred_hub.cnpj.mapping import only_digits
from cred_hub.cnpj.sectors import sector_for_cnae

_PARTICULAS = {"da", "de", "do", "das", "dos", "e", "di", "du"}
ADDRESS_SUFFIX = " (Receita)"


def person_name(raw: str) -> str:
	"""ANA MARIA DA SILVA -> Ana Maria da Silva"""
	palavras = raw.strip().lower().split()
	return " ".join(p if i and p in _PARTICULAS else p.capitalize() for i, p in enumerate(palavras))


def years_between(start, end=None) -> int | None:
	"""Anos completos entre o início da atividade e hoje (tempo de mercado)."""
	if not start:
		return None
	start, end = getdate(start), getdate(end or today())
	return max(end.year - start.year - ((end.month, end.day) < (start.month, start.day)), 0)


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

	for field in RECEITA_FIELDS:
		doc.set(field, data.get(field))
	doc.years_in_business = years_between(data.get("activity_start_date"))
	sector = ensure_industry(sector_for_cnae(data.get("main_cnae")))
	if sector:
		doc.industry = sector
	if doctype == "CRM Lead" and not doc.get("organization"):
		doc.organization = data.get("trade_name") or data.get("legal_name")
	if doctype == "CRM Organization":
		doc.address = upsert_address(doc, data.get("address") or {}) or doc.address
	doc.cnpj_checked_at = now_datetime()
	doc.flags.cred_hub_enriching = True
	doc.save(ignore_permissions=True)

	partners = add_partners(doc, data.get("partners") or []) if doctype == "CRM Organization" else 0
	resumo = _("Dados do CNPJ {0} preenchidos automaticamente (fonte: {1}).").format(doc.cnpj, data.get("source"))
	if partners:
		resumo += " " + _("{0} sócio(s) cadastrado(s) como contato.").format(partners)
	doc.add_comment("Comment", resumo)


def _finish_without_data(doc, message, retry=False):
	if not retry:
		# Marca como consultado para não repetir a cada salvamento; mudar o CNPJ consulta de novo
		doc.db_set("cnpj_checked_at", now_datetime(), update_modified=False)
	doc.add_comment("Comment", message)


def ensure_industry(sector: str | None) -> str | None:
	"""Garante o setor (CRM Industry, nome em inglês) e devolve o nome."""
	if not sector:
		return None
	if not frappe.db.exists("CRM Industry", sector):
		frappe.get_doc({"doctype": "CRM Industry", "industry": sector}).insert(ignore_permissions=True)
	return sector


def upsert_address(org, parts: dict) -> str | None:
	"""Endereço da Receita como Address padrão do Frappe, ligado à Organização.

	Reaproveita o endereço criado antes por este job (título "<organização> (Receita)"); um endereço que o
	usuário escolheu à mão não é alterado: nesse caso devolve o atual.
	"""
	if not parts.get("address_line1") or not parts.get("city"):
		return None
	title = f"{org.organization_name}{ADDRESS_SUFFIX}"
	if org.get("address") and frappe.db.get_value("Address", org.address, "address_title") != title:
		return org.address
	name = org.get("address") or frappe.db.get_value("Address", {"address_title": title})
	address = frappe.get_doc("Address", name) if name else frappe.new_doc("Address")
	address.update(
		{
			"address_title": title,
			"address_type": "Office",
			"address_line1": parts["address_line1"],
			"address_line2": parts.get("address_line2") or "",
			"city": parts["city"],
			"state": parts.get("state") or "",
			"pincode": parts.get("pincode") or "",
			"country": "Brazil",
		}
	)
	if not any(link.link_doctype == org.doctype and link.link_name == org.name for link in address.get("links", [])):
		address.append("links", {"link_doctype": org.doctype, "link_name": org.name})
	address.flags.ignore_permissions = True
	address.save()
	return address.name


def add_partners(org, partners) -> int:
	"""Cria um Contato por sócio, ligado à Organização (campo company_name). Não duplica."""
	created = 0
	existentes = {
		c.full_name.strip().lower()
		for c in frappe.get_all("Contact", filters={"company_name": org.name}, fields=["full_name"])
		if c.full_name
	}
	for partner in partners:
		nome = person_name(partner["name"])
		if nome.lower() in existentes:
			continue
		contact = frappe.new_doc("Contact")
		contact.first_name = nome
		contact.company_name = org.name
		contact.designation = partner.get("qualification") or _("Sócio")
		contact.flags.ignore_mandatory = True
		contact.insert(ignore_permissions=True)
		existentes.add(nome.lower())
		created += 1
	return created


def update_years_in_business():
	"""Diário (scheduler): recalcula o tempo de mercado de todas as organizações e leads com início da atividade."""
	for doctype in DOCTYPES:
		table = f"`tab{doctype}`"
		frappe.db.sql(
			f"""update {table} set years_in_business = timestampdiff(year, activity_start_date, curdate())
			where activity_start_date is not null"""
		)
	frappe.db.commit()
