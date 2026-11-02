"""Validação de documento ao salvar (credtech#315): CPF não entra em Organização e CNPJ não entra no
CPF do Contato.

Só valida quando o documento é novo ou mudou: registros antigos com o documento no lugar errado
continuam editáveis até serem convertidos. CPF com dígito errado só gera aviso, como o CNPJ
(decisão do dono, 24/10/2026).
"""

import frappe
from frappe import _

from cred_hub.party.document import CNPJ, CPF, document_kind, format_cpf, is_valid_cpf


def validate_organization(doc, method=None):
	if not doc.get("cnpj") or not doc.has_value_changed("cnpj"):
		return
	if document_kind(doc.cnpj) == CPF:
		# Texto longo de propósito: o _() precisa do texto inteiro para a tradução
		message = _("{0} is a CPF. People go in Contacts (CPF field); Organizations are only for companies with a CNPJ.")
		frappe.throw(message.format(format_cpf(doc.cnpj)), title=_("This is a CPF"))


def validate_contact(doc, method=None):
	if not doc.meta.has_field("cpf") or not doc.get("cpf") or not doc.has_value_changed("cpf"):
		return
	if document_kind(doc.cpf) == CNPJ:
		message = _("{0} is a CNPJ. Companies go in Organizations; the CPF field of a contact only takes a person's CPF.")
		frappe.throw(message.format(doc.cpf), title=_("This is a CNPJ"))
	doc.cpf = format_cpf(doc.cpf)
	if not is_valid_cpf(doc.cpf):
		frappe.msgprint(
			_("The CPF {0} looks invalid (the check digits do not match).").format(doc.cpf),
			indicator="orange",
			alert=True,
		)
