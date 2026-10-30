"""Bureaus do épico credtech#290: Assertiva Localize (contatos da organização) e CENPROT (protesto no funil de
cobrança). Credenciais da própria empresa em Cred Hub Provider Settings (só admins); toda consulta paga e toda
operação no cartório ficam em Cred Hub Bureau Query (auditoria LGPD e cache para não pagar duas vezes)."""

import frappe

SETTINGS = "Cred Hub Provider Settings"


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def secret(settings, fieldname: str) -> str:
	return settings.get_password(fieldname, raise_exception=False) or ""
