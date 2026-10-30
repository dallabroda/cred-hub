"""Registro dos bureaus "Premium" (pagos, sob demanda, só liberados após 'Pre-Approved' no
pré-filtro de baixo custo — mesmo gate de `clients/serasa.py:assert_pre_approved_for_bureau`,
que é genérico apesar do nome do módulo).

Cada bureau aqui roda com a conta/token DirectD da PRÓPRIA CredTech (plataforma, 1 só,
compartilhada por todas as empresas clientes) e é revendido com margem fixa (decidida pelo dono,
30/10/2026) — por isso a chamada é paga com o saldo de créditos da empresa (`billing.spend_credits`),
nunca com o token DirectD que o cliente cadastra em Cred Hub Credit Settings pra esteira automática
(esse token é da conta do próprio cliente, usado só pelos 6 provedores automáticos de
`enrich.py:_build_providers`). Misturar os dois já foi um bug real desta esteira: o cliente pagaria
a DirectD direto pela própria conta E a CredTech cobraria margem em cima, cobrando duas vezes pela
mesma consulta.

O Serasa Relato PJ (`clients/serasa.py`) fica de fora deste registro: usa o contrato e a credencial
do próprio cliente, sem custo pra CredTech repassar nem débito de créditos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cred_hub.credit_check.clients.directd import (
	fetch_boa_vista_risco_pj,
	fetch_dossie_credito_completo,
	fetch_scr_bacen,
)


class PlatformDirectDNotConfigured(Exception):
	"""O token DirectD da própria CredTech (plataforma) não está configurado neste ambiente."""


def platform_directd_token() -> str:
	"""Token DirectD da conta da CredTech (não a do cliente), vindo do site_config/common_site_config
	— gravado pelo deploy, igual ASAAS_API_KEY. Nunca lido do Cred Hub Credit Settings do cliente."""
	import frappe

	token = frappe.conf.get("credhub_directd_platform_token") or ""
	if not token:
		raise PlatformDirectDNotConfigured(
			"Token DirectD da plataforma (credhub_directd_platform_token) não configurado neste site."
		)
	return token


@dataclass
class PremiumBureau:
	key: str
	label: str
	fetch: Callable[[str, str], dict[str, Any]]  # (documento, token) -> dados
	cost: float          # quanto a CredTech paga à DirectD por consulta
	client_price: float  # quanto debitar da carteira de créditos da empresa (preço fixo)


PREMIUM_BUREAUS: dict[str, PremiumBureau] = {
	"boavista_risco": PremiumBureau(
		key="boavista_risco",
		label="Boa Vista - Risco Positivo PJ",
		fetch=fetch_boa_vista_risco_pj,
		cost=14.03,
		client_price=18.00,
	),
	"dossie_quod": PremiumBureau(
		key="dossie_quod",
		label="Dossiê QUOD - Completo",
		fetch=fetch_dossie_credito_completo,
		cost=6.99,
		client_price=10.00,
	),
	"scr_bacen": PremiumBureau(
		key="scr_bacen",
		label="SCR Analítico - Resumo BACEN",
		fetch=fetch_scr_bacen,
		cost=3.90,
		client_price=6.00,
	),
}
