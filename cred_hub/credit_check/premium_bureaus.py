"""Registro dos bureaus "Premium" (pagos, sob demanda, só liberados após 'Pre-Approved' no
pré-filtro de baixo custo — mesmo gate de `clients/serasa.py:assert_pre_approved_for_bureau`,
que é genérico apesar do nome do módulo).

Cada bureau aqui roda com a conta/token da própria CredTech (DirectD) e é revendido com margem
fixa (decidida pelo dono, 30/10/2026). O Serasa Relato PJ (`clients/serasa.py`) fica de fora deste
registro: usa o contrato e a credencial do próprio cliente, não tem custo pra CredTech repassar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cred_hub.credit_check.clients.directd import (
	fetch_boa_vista_risco_pj,
	fetch_dossie_credito_completo,
	fetch_scr_bacen,
)


@dataclass
class PremiumBureau:
	key: str
	label: str
	fetch: Callable[[str, str], dict[str, Any]]  # (documento, token) -> dados
	cost: float          # quanto a CredTech paga à DirectD por consulta
	client_price: float  # quanto cobrar do cliente (preço fixo decidido pelo dono)
	credential_setting: str  # campo de Cred Hub Credit Settings com o token


PREMIUM_BUREAUS: dict[str, PremiumBureau] = {
	"boavista_risco": PremiumBureau(
		key="boavista_risco",
		label="Boa Vista - Risco Positivo PJ",
		fetch=fetch_boa_vista_risco_pj,
		cost=14.03,
		client_price=18.00,
		credential_setting="directd_token",
	),
	"dossie_quod": PremiumBureau(
		key="dossie_quod",
		label="Dossiê QUOD - Completo",
		fetch=fetch_dossie_credito_completo,
		cost=6.99,
		client_price=10.00,
		credential_setting="directd_token",
	),
	"scr_bacen": PremiumBureau(
		key="scr_bacen",
		label="SCR Analítico - Resumo BACEN",
		fetch=fetch_scr_bacen,
		cost=3.90,
		client_price=6.00,
		credential_setting="directd_token",
	),
}
