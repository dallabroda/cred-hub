"""Job em segundo plano: descobre site, LinkedIn e funcionários e preenche o Lead/Organização.
Nunca sobrescreve o que o usuário já preencheu manualmente."""

import frappe
from frappe import _
from frappe.utils import today

from cred_hub.company import discover as discovery
from cred_hub.company.events import company_name

# Porte da v0.2 (cred_hub.cnpj.mapping.COMPANY_SIZES) -> texto da Receita que o piso SEBRAE entende
RECEITA_SIZE = {"ME": "ME", "EPP": "EPP", "Other size": "DEMAIS", "Demais": "DEMAIS"}
NO_OF_EMPLOYEES_DEFAULT = "1-10"


def enrich_company(doctype, name):
	if not frappe.db.exists(doctype, name):
		# Se a Organização foi renomeada para a razão social (use_legal_name) antes do job rodar:
		if doctype == "CRM Organization":
			renamed = frappe.db.get_value("CRM Organization", {"trade_name": name}, "name")
			if renamed:
				name = renamed
			else:
				return
		else:
			return
	doc = frappe.get_doc(doctype, name)
	if doc.get("company_checked_on") or not company_name(doc):
		return

	try:
		found = discovery.discover(
			company_name(doc),
			porte=RECEITA_SIZE.get(doc.get("company_size") or "", ""),
			cnae=(doc.get("main_cnae") or "").split(" ")[0],
			opcao_mei=doc.get("is_mei") in ("Yes", "Sim"),
			capital_social=float(doc.get("share_capital") or 0.0),
			trade_name=doc.get("trade_name") or "",
			natureza_juridica=doc.get("legal_nature") or "",
		)
	except Exception:
		# Não marca como consultado: a próxima gravação tenta de novo
		frappe.log_error(title=f"Cred Hub: descoberta de empresa falhou ({company_name(doc)})")
		return

	if not getattr(frappe.flags, "in_test", False):
		frappe.db.commit()  # nosemgrep — renova o snapshot após chamadas HTTP longas para evitar erro 1020
	doc.reload()
	apply(doc, found)
	doc.company_checked_on = today()
	doc.flags.cred_hub_enriching = True  # não reconsulta o CNPJ por causa deste salvamento
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", summary(found))


def apply(doc, found):
	if found["site"] and not doc.get("website"):
		doc.website = found["site"]
	if found["linkedin"] and not doc.get("linkedin"):
		doc.linkedin = found["linkedin"]
	# O Frappe CRM preenche "1-10" por padrão: esse valor ou estimativa preliminar SEBRAE pode ser atualizado
	if found["crm_bucket"] and (
		doc.get("no_of_employees") in (None, "", NO_OF_EMPLOYEES_DEFAULT)
		or doc.get("employee_source") in (None, "", "SEBRAE/CNPJ")
	):
		doc.no_of_employees = found["crm_bucket"]
	doc.employee_count = found["funcionarios"] or None
	doc.employee_range = found["faixa"]
	doc.employee_source = found["fonte"]


def summary(found) -> str:
	if not (found["site"] or found["linkedin"] or found["faixa"]):
		return _("Nenhum site, LinkedIn ou número de funcionários encontrado automaticamente.")
	parts = []
	if found["site"]:
		parts.append(_("site {0}").format(found["site"]))
	if found["linkedin"]:
		parts.append(_("LinkedIn {0}").format(found["linkedin"]))
	if found["faixa"]:
		parts.append(_("funcionários {0} (fonte: {1})").format(found["faixa"], found["fonte"]))
	return _("Presença digital preenchida automaticamente: {0}.").format("; ".join(parts))
