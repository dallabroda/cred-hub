"""Credenciais da própria CredTech (plataforma — 1 conta só, compartilhada por todas as empresas
clientes), nunca do Cred Hub Credit Settings de cada empresa. Vêm do site_config/common_site_config,
gravadas pelo deploy — mesmo padrão do ASAAS_API_KEY em billing.py.

Decisão do dono (30/10/2026): Escavador e Serasa continuam com a chave do próprio cliente (conta
dele, ele paga direto). Infosimples e DirectD (todos os produtos, automáticos e Premium) passam a
rodar na conta da CredTech — cobrados do cliente via carteira de créditos, com markup de 20% —, e
o Portal da Transparência (CGU) também passa a ser 1 chave só da CredTech, sem markup (é grátis).
"""

from __future__ import annotations


class PlatformCredentialNotConfigured(Exception):
	"""Uma credencial da plataforma (CredTech, não do cliente) não está configurada neste site."""


def _conf(key: str, label: str) -> str:
	import frappe

	value = frappe.conf.get(key) or ""
	if not value:
		raise PlatformCredentialNotConfigured(f"{label} não configurado neste site ({key}).")
	return value


def platform_directd_token() -> str:
	"""Token DirectD da conta da CredTech — cobre todos os produtos (automáticos e Premium)."""
	return _conf("credhub_directd_platform_token", "Token DirectD da plataforma")


def platform_infosimples_token() -> str:
	"""Token Infosimples da conta da CredTech."""
	return _conf("credhub_infosimples_platform_token", "Token Infosimples da plataforma")


def platform_cgu_api_key() -> str:
	"""Chave do Portal da Transparência (CGU) da conta da CredTech — grátis, sem markup."""
	return _conf("credhub_cgu_platform_api_key", "Chave do Portal da Transparência da plataforma")
