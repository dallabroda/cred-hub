"""Job em segundo plano: descobre site, LinkedIn e funcionários e preenche o Lead/Organização.
Nunca sobrescreve o que o usuário já preencheu (website, nº de funcionários, LinkedIn)."""

import frappe
from frappe import _
from frappe.utils import now_datetime

from cred_hub.company import discover as discovery
from cred_hub.company.events import company_name

# Porte da v0.2 (cred_hub.cnpj.mapping.COMPANY_SIZES) -> texto da Receita que o piso SEBRAE entende
RECEITA_SIZE = {"ME": "ME", "EPP": "EPP", "Other size": "DEMAIS"}
NO_OF_EMPLOYEES_DEFAULT = "1-10"


def enrich_company(doctype, name):
	if not frappe.db.exists(doctype, name):
		return
	doc = frappe.get_doc(doctype, name)
	if doc.get("company_checked_at") or not company_name(doc):
		return

	try:
		found = discovery.discover(
			company_name(doc),
			porte=RECEITA_SIZE.get(doc.get("company_size") or "", ""),
			cnae=(doc.get("main_cnae") or "").split(" ")[0],
			opcao_mei=doc.get("is_mei") == "Yes",
		)
	except Exception:
		# Não marca como consultado: a próxima gravação tenta de novo
		frappe.log_error(title=f"Cred Hub: descoberta de empresa falhou ({company_name(doc)})")
		return

	apply(doc, found)
	doc.company_checked_at = now_datetime()
	doc.flags.cred_hub_enriching = True  # não reconsulta o CNPJ por causa deste salvamento
	doc.save(ignore_permissions=True)
	doc.add_comment("Comment", summary(found))


def apply(doc, found):
	if found["site"] and not doc.get("website"):
		doc.website = found["site"]
	if found["linkedin"] and not doc.get("linkedin"):
		doc.linkedin = found["linkedin"]
	# O Frappe CRM preenche "1-10" por padrão: esse valor conta como não preenchido
	if found["crm_bucket"] and doc.get("no_of_employees") in (None, "", NO_OF_EMPLOYEES_DEFAULT):
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
