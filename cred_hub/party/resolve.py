"""Acha ou cria quem tem o documento (credtech#315), para importações e conversões: CPF vira Contato e
CNPJ vira Organização, sempre reaproveitando o registro que já tem o mesmo documento (sem duplicar)."""

import frappe
from frappe import _

from cred_hub.cnpj.enrich import person_name
from cred_hub.cnpj.mapping import format_cnpj, only_digits
from cred_hub.party.document import CNPJ, CPF, document_kind, format_cpf
from cred_hub.sandbox import same_space


def contact_for_cpf(cpf, name: str = "") -> str:
	"""Contato com esse CPF; senão um novo com o nome e o CPF."""
	formatted = format_cpf(cpf)
	found = frappe.get_all(
		"Contact", {"cpf": ("in", (formatted, only_digits(cpf))), **same_space("Contact")}, pluck="name", limit=1
	)
	if found:
		return found[0]
	contact = frappe.new_doc("Contact")
	contact.first_name = person_name(name) if (name or "").strip() else formatted
	contact.cpf = formatted
	contact.insert(ignore_permissions=True)
	return contact.name


def organization_for_cnpj(cnpj, name: str = "", values: dict | None = None) -> str:
	"""Organização com esse CNPJ; senão uma de mesmo nome ainda sem CNPJ; senão uma nova."""
	digits = only_digits(cnpj)
	formatted = format_cnpj(digits)
	values = {"cnpj": formatted, **(values or {})}
	same_cnpj = frappe.get_all(
		"CRM Organization",
		{"cnpj": ("in", (formatted, digits)), **same_space("CRM Organization")},
		pluck="name",
		limit=1,
	)
	if same_cnpj:
		return same_cnpj[0]

	name = (name or "").strip() or formatted
	homonym = frappe.db.get_value(
		"CRM Organization", {"organization_name": name}, ["name", "cnpj"], as_dict=True
	)
	if homonym and not homonym.cnpj:
		org = frappe.get_doc("CRM Organization", homonym.name)
		org.update(values)
		org.save(ignore_permissions=True)
		return org.name
	if homonym:
		# Mesmo nome, outra empresa: o nome é a chave da organização, então vai com o CNPJ junto
		name = f"{name} ({formatted})"

	org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": name, **values})
	org.insert(ignore_permissions=True)
	return org.name


def party_for_document(document, name: str = "", values: dict | None = None) -> tuple[str, str]:
	"""("Contact", nome) para CPF ou ("CRM Organization", nome) para CNPJ; documento inválido para com erro."""
	kind = document_kind(document)
	if kind == CPF:
		return "Contact", contact_for_cpf(document, name)
	if kind == CNPJ:
		return "CRM Organization", organization_for_cnpj(document, name, values)
	frappe.throw(_("{0} is not a valid CPF or CNPJ.").format(document), title=_("Invalid document"))
