"""Registro dos bureaus "Premium" (pagos, sob demanda, só liberados após 'Pre-Approved' no
pré-filtro de baixo custo — mesmo gate de `clients/serasa.py:assert_pre_approved_for_bureau`,
que é genérico apesar do nome do módulo).

Cada bureau aqui roda com o mesmo token DirectD da PRÓPRIA CredTech (plataforma, 1 só, compartilhada
por todas as empresas clientes — `platform_credentials.platform_directd_token`) usado também pelos
produtos DirectD automáticos em `enrich.py` (Score, Protestos Nacional, Processos, FGTS, Sintegra/
CCC — decisão do dono, 30/10/2026: DirectD inteiro passou a ser conta da CredTech, não do cliente).
É revendido com margem fixa (bureaus Premium) ou 20% (automáticos) — por isso toda chamada é paga
com o saldo de créditos da empresa (`billing.spend_credits`), nunca com credencial do cliente.

Só Escavador e Serasa continuam com chave/contrato do próprio cliente (ele paga direto, sem
intermediar pela carteira de créditos). O Serasa Relato PJ (`clients/serasa.py`) fica de fora deste
registro por esse motivo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cred_hub.credit_check.clients.directd import (
	fetch_boa_vista_risco_pj,
	fetch_dossie_credito_completo,
	fetch_scr_bacen,
)
from cred_hub.credit_check.platform_credentials import (
	PlatformCredentialNotConfigured as PlatformDirectDNotConfigured,
	platform_directd_token,
)

# Reexportados para quem já importava daqui (request_bureau_final em api.py)
__all__ = ["PREMIUM_BUREAUS", "PremiumBureau", "PlatformDirectDNotConfigured", "platform_directd_token"]


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
