"""Sócios da empresa analisada (credtech#316): quem são, se já têm CPF completo e o que pode ser consultado
sobre cada um.

O sócio é um Contato ligado à Organização (`company_name`), o mesmo vínculo que a Receita (nome) e o
Localize da Assertiva (nome + CPF) já usam. A Receita só mostra o CPF mascarado, então o CPF completo
vem de uma destas fontes: o Contato que já tem (Localize), ou o operador digitando. Nenhum sócio vem
marcado para consulta: quem escolhe é o operador, e cada consulta é paga na conta do cliente.
"""

from __future__ import annotations

import frappe
from frappe import _

from cred_hub.cnpj.mapping import only_digits

# Bureaus finais que aceitam CPF (DirectD: Dossiê QUOD, SCR Bacen, Boa Vista Acerta PF, Processos
# Completa e Nível Socioeconômico PF). Boa Vista Risco Positivo PJ e Serasa Relato PJ são só de empresa.
PARTNER_BUREAUS = (
	"dossie_quod",
	"scr_bacen",
	"boavista_acerta_pf",
	"processos_completa",
	"nivel_socioeconomico",
)


def _is_managing_role(designation: str) -> bool:
	role = (designation or "").strip().lower()
	return any(k in role for k in ("admin", "diretor", "presidente", "representante", "titular", "gerente"))


def is_valid_cpf(value) -> bool:
	"""CPF com 11 dígitos e dígitos verificadores corretos (aceita com ou sem máscara)."""
	digits = only_digits(value)
	if len(digits) != 11 or digits == digits[0] * 11:
		return False
	for size in (9, 10):
		total = sum(int(d) * (size + 1 - i) for i, d in enumerate(digits[:size]))
		if digits[size] != str((total * 10 % 11) % 10):
			return False
	return True


def format_cpf(value) -> str:
	digits = only_digits(value)
	return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}" if len(digits) == 11 else (value or "")


def partners_of(organization_name: str) -> list[dict]:
	"""Contatos da Organização como sócios, com o CPF quando já é completo e válido."""
	rows = frappe.get_all(
		"Contact",
		filters={"company_name": organization_name},
		fields=["name", "full_name", "designation", "share_percentage", "cpf"],
		order_by="share_percentage desc, full_name asc",
	)
	has_explicit_admin = any(_is_managing_role(row.designation or "") for row in rows)
	out = []
	for idx, row in enumerate(rows):
		is_admin = _is_managing_role(row.designation or "") if has_explicit_admin else (idx == 0)
		out.append(
			{
				"contact": row.name,
				"name": row.full_name or row.name,
				"designation": row.designation or "",
				"share": row.share_percentage,
				"cpf": format_cpf(row.cpf) if is_valid_cpf(row.cpf) else "",
				"has_cpf": is_valid_cpf(row.cpf),
				"is_admin": is_admin,
			}
		)
	return out


def partner_contact(organization_name: str, contact_name: str):
	"""O Contato, só se for mesmo da Organização e o usuário puder vê-lo."""
	if not contact_name or not frappe.db.exists("Contact", contact_name):
		frappe.throw(_("Partner not found."))
	contact = frappe.get_doc("Contact", contact_name)
	if contact.get("company_name") != organization_name:
		frappe.throw(_("This contact is not a partner of this organization."))
	if not frappe.has_permission("Contact", "read", doc=contact):
		frappe.throw(_("You do not have permission to access this contact."), frappe.PermissionError)
	return contact


def save_cpf(organization_name: str, contact_name: str, cpf: str) -> dict:
	"""Grava o CPF digitado pelo operador no Contato do sócio. CPF inválido não é gravado."""
	contact = partner_contact(organization_name, contact_name)
	if not is_valid_cpf(cpf):
		frappe.throw(_("Enter a valid CPF."))
	if not frappe.has_permission("Contact", "write", doc=contact):
		frappe.throw(_("You do not have permission to edit this contact."), frappe.PermissionError)
	contact.cpf = format_cpf(cpf)
	contact.save()
	return {"contact": contact.name, "cpf": contact.cpf}


def cpf_digits(organization_name: str, contact_name: str) -> tuple[str, str]:
	"""(CPF só com dígitos, nome) de um sócio para consultar; falha se ainda não tem CPF completo."""
	contact = partner_contact(organization_name, contact_name)
	if not is_valid_cpf(contact.get("cpf")):
		frappe.throw(_("{0} has no valid CPF yet. Fill the CPF first.").format(contact.full_name or contact.name))
	return only_digits(contact.cpf), contact.full_name or contact.name
