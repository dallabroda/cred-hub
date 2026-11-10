"""Job em segundo plano: consulta o CNPJ, preenche os campos da Receita, o endereço e o setor, e cadastra os sócios."""

import frappe
from frappe import _
from frappe.utils import getdate, today

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

	# Encerra a transação anterior à chamada HTTP externa e grava auxiliares (setor/endereço) antes
	# de recarregar o documento, evitando erro 1020 ("Record has changed since last read").
	sector = ensure_industry(sector_for_cnae(data.get("main_cnae")))
	new_addr = upsert_address(doc, data.get("address") or {}) if doctype == "CRM Organization" else None
	frappe.db.commit()
	if not frappe.db.exists(doctype, name):
		return
	doc.reload()

	for field in RECEITA_FIELDS:
		doc.set(field, data.get(field))
	doc.years_in_business = years_between(data.get("activity_start_date"))
	if sector:
		doc.industry = sector
	# Estima imediatamente a faixa de funcionários pelo Porte + CNAE + Capital Social da Receita
	# para nunca deixar empresa de porte Demais/EPP com o default "1-10"
	if doc.get("no_of_employees") in (None, "", "1-10"):
		from cred_hub.company.discover import sebrae_crm_bucket, sebrae_range
		from cred_hub.company.enrich import RECEITA_SIZE
		porte_receita = RECEITA_SIZE.get(doc.get("company_size") or "", "")
		cnae_code = (doc.get("main_cnae") or "").split(" ")[0]
		faixa_sebrae = sebrae_range(porte_receita, cnae_code, doc.get("is_mei") in ("Yes", "Sim"))
		bucket = sebrae_crm_bucket(
			faixa_sebrae,
			capital_social=float(doc.get("share_capital") or 0.0),
			natureza_juridica=doc.get("legal_nature") or "",
			name=doc.get("legal_name") or doc.name,
		)
		if bucket:
			doc.no_of_employees = bucket
			doc.employee_range = faixa_sebrae
			doc.employee_source = "SEBRAE/CNPJ"
	if doctype == "CRM Lead" and not doc.get("organization"):
		doc.organization = data.get("trade_name") or data.get("legal_name")
	if doctype == "CRM Organization" and new_addr:
		doc.address = new_addr
	doc.cnpj_checked_on = today()
	doc.flags.cred_hub_enriching = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	partners = add_partners(doc, data.get("partners") or []) if doctype == "CRM Organization" else 0
	resumo = _("Dados do CNPJ {0} preenchidos automaticamente (fonte: {1}).").format(doc.cnpj, data.get("source"))
	if partners:
		resumo += " " + _("{0} sócio(s) cadastrado(s) como contato.").format(partners)
	doc.add_comment("Comment", resumo)
	frappe.db.commit()
	if doctype == "CRM Organization":
		use_legal_name(doc, data.get("legal_name"))
		final_name = (data.get("legal_name") or "").strip()
		if final_name and frappe.db.exists("CRM Organization", final_name):
			from cred_hub.company.events import enqueue as enqueue_company
			enqueue_company("CRM Organization", final_name)


def use_legal_name(doc, legal_name: str | None):
	"""A organização com CNPJ passa a se chamar pela razão social da Receita (decisão do dono, 28/10/2026): razão
	social e nome fantasia saíram das telas e o nome da organização é o único nome. Os cards e contatos ligados
	acompanham a troca. Se outra organização já tem esse nome, fica o nome que o usuário cadastrou."""
	legal_name = (legal_name or "").strip()
	if not legal_name or legal_name == doc.name:
		return
	if frappe.db.exists("CRM Organization", legal_name):
		doc.add_comment(
			"Comment",
			_("O nome não foi trocado para a razão social {0}: já existe outra organização com esse nome.").format(legal_name),
		)
		return
	old = doc.name
	frappe.rename_doc("CRM Organization", old, legal_name, force=True, show_alert=False)
	frappe.db.set_value("CRM Organization", legal_name, "organization_name", legal_name, update_modified=False)
	# a organização do contato é texto (company_name), não vínculo: o rename não troca sozinho
	frappe.db.set_value("Contact", {"company_name": old}, "company_name", legal_name, update_modified=False)
	frappe.get_doc("CRM Organization", legal_name).add_comment(
		"Comment", _("Nome trocado de {0} para a razão social da Receita.").format(old)
	)


def _finish_without_data(doc, message, retry=False):
	if not retry:
		# Marca como consultado para não repetir a cada salvamento; mudar o CNPJ consulta de novo
		doc.db_set("cnpj_checked_on", today(), update_modified=False)
		from cred_hub.company.events import company_name, enqueue

		if company_name(doc) and not doc.get("company_checked_on"):
			enqueue(doc.doctype, doc.name)  # sem dados da Receita, segue só com a busca na web
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
	existing_title = frappe.db.get_value("Address", org.address, "address_title") if org.get("address") else None
	if existing_title and existing_title != title:
		return org.address
	name = (org.get("address") if existing_title else None) or frappe.db.get_value("Address", {"address_title": title})
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


@frappe.whitelist()
def resolve_organization(org_name: str) -> dict:
	"""Localiza o nome atual de uma organização caso tenha sido renomeada pelo CNPJ (razão social da Receita).
	Permite que a interface redirecione para a rota atual sem exibir 404."""
	if not org_name:
		return {"exists": False}
	org_name = org_name.strip()
	if frappe.db.exists("CRM Organization", org_name):
		return {"exists": True, "name": org_name}

	found = frappe.db.get_value("CRM Organization", {"organization_name": org_name}, "name")
	if found:
		return {"exists": True, "name": found, "redirect": True}

	# Busca em comentários de alteração de nome da Receita
	comment = frappe.db.get_value(
		"Comment",
		{
			"reference_doctype": "CRM Organization",
			"comment_type": "Comment",
			"content": ["like", f"%Nome trocado de {org_name} para a razão social da Receita%"],
		},
		"reference_name",
	)
	if comment and frappe.db.exists("CRM Organization", comment):
		return {"exists": True, "name": comment, "redirect": True}

	return {"exists": False}


@frappe.whitelist()
def fetch_cnpj_preview(cnpj: str) -> dict:
	"""Consulta imediata de CNPJ na Receita Federal para autopreenchimento no modal de criação."""
	frappe.only_for(["System Manager", "Sales Manager", "Sales User"])
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		return {"success": False, "error": "CNPJ inválido"}
	try:
		data = client.fetch(digits)
		return {
			"success": True,
			"legal_name": data.get("legal_name") or "",
			"trade_name": data.get("trade_name") or "",
			"main_cnae": data.get("main_cnae") or "",
			"address": data.get("address") or {},
			"state": data.get("state") or "",
			"city": data.get("city") or "",
			"activity_start_date": data.get("activity_start_date") or "",
			"share_capital": data.get("share_capital") or 0.0,
			"partners_count": len(data.get("partners") or []),
			"source": data.get("source") or "",
		}
	except client.CnpjNotFound:
		return {"success": False, "error": "CNPJ não encontrado na Receita Federal"}
	except Exception as e:
		return {"success": False, "error": str(e)}

