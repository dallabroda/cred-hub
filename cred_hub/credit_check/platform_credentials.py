"""Credenciais da própria CredTech (plataforma — 1 conta só, compartilhada por todas as empresas
clientes), nunca do Cred Hub Credit Settings de cada empresa. Vêm do site_config/common_site_config,
gravadas pelo deploy — mesmo padrão do ASAAS_API_KEY em billing.py.

Decisão do dono (02/11/2026, credtech#316): todo bureau pago usa a chave do PRÓPRIO cliente
(Infosimples, DirectD, Serasa, Escavador, Assertiva, CENPROT) — a CredTech só faz a ponte e não
cobra consulta. Isso substitui a decisão de 30/10 (Infosimples e DirectD na conta da CredTech com
20% de margem). Sobra aqui só o Portal da Transparência (CGU): a chave é gratuita, então continua
1 só, da CredTech, sem custo pra ninguém.
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


def platform_cgu_api_key() -> str:
	"""Chave do Portal da Transparência (CGU) da conta da CredTech — grátis, sem custo pro cliente."""
	return _conf("credhub_cgu_platform_api_key", "Chave do Portal da Transparência da plataforma")


def platform_bigdatacorp_token(allow_empty: bool = True) -> str:
	"""Chave Master da BigDataCorp da conta de revendedor da CredTech (`AccessToken:TokenId`),
	lida de `frappe.conf.credhub_bigdatacorp_platform_token` ou da variável de ambiente `BIGDATACORP_API_TOKEN`."""
	import os

	try:
		import frappe
		conf_val = (frappe.conf.get("credhub_bigdatacorp_platform_token") or "") if getattr(frappe, "conf", None) else ""
	except Exception:
		conf_val = ""
	token = (conf_val or os.environ.get("BIGDATACORP_API_TOKEN") or "").strip()
	if not token and not allow_empty:
		raise PlatformCredentialNotConfigured(
			"Credencial Master da BigDataCorp não configurada neste site (credhub_bigdatacorp_platform_token)."
		)
	return token
