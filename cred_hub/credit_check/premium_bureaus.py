"""Registro dos bureaus finais sob demanda que rodam pela DirectD (Boa Vista, Dossiê QUOD, SCR
Bacen). O Serasa Relato PJ (`clients/serasa.py`) é outro contrato e fica fora deste registro.

Quem paga (decisão do dono, 02/11/2026, credtech#316): todos usam o token DirectD do PRÓPRIO
cliente (Cred Hub Credit Settings > `directd_token`) — a DirectD cobra direto na conta dele e a
CredTech não cobra nada. `price_reference` é o preço de tabela da DirectD, mostrado na tela só como
referência ("cobrado pelo provedor na sua conta"): o contrato do cliente pode ter outro preço.

Sem trava de pré-aprovação (decisão de 02/11): antes de consultar, a tela mostra o resultado do
pré-filtro, o motivo e o custo, e o operador confirma — ver `request_bureau_final` em api.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cred_hub.credit_check.clients.directd import (
	fetch_boa_vista_acerta_pf,
	fetch_boa_vista_risco_pj,
	fetch_dossie_credito_completo,
	fetch_nivel_socioeconomico,
	fetch_processos_completa,
	fetch_scr_bacen,
)

__all__ = ["PREMIUM_BUREAUS", "PremiumBureau"]


@dataclass
class PremiumBureau:
	key: str
	label: str
	fetch: Callable[[str, str], dict[str, Any]]  # (documento, token) -> dados
	price_reference: float  # preço de tabela da DirectD por consulta (só referência, quem cobra é a DirectD)
	enabled_field: str  # campo do Cred Hub Credit Settings que liga este bureau
	tier: int = 2  # nível sugerido no funil progressivo (1 = Inicial, 2 = Médio, 3 = Profundo)


PREMIUM_BUREAUS: dict[str, PremiumBureau] = {
	"boavista_risco": PremiumBureau(
		key="boavista_risco",
		label="Boa Vista - Risco Positivo PJ",
		fetch=fetch_boa_vista_risco_pj,
		price_reference=14.03,
		enabled_field="boavista_risco_enabled",
		tier=3,
	),
	"boavista_acerta_pf": PremiumBureau(
		key="boavista_acerta_pf",
		label="Boa Vista - Acerta Completo Positivo PF",
		fetch=fetch_boa_vista_acerta_pf,
		price_reference=14.03,
		enabled_field="boavista_acerta_pf_enabled",
		tier=3,
	),
	"dossie_quod": PremiumBureau(
		key="dossie_quod",
		label="Dossiê QUOD - Completo",
		fetch=fetch_dossie_credito_completo,
		price_reference=6.99,
		enabled_field="dossie_quod_enabled",
		tier=2,
	),
	"scr_bacen": PremiumBureau(
		key="scr_bacen",
		label="SCR Analítico - Resumo BACEN",
		fetch=fetch_scr_bacen,
		price_reference=3.90,
		enabled_field="scr_bacen_enabled",
		tier=2,
	),
	"processos_completa": PremiumBureau(
		key="processos_completa",
		label="Processos Judiciais - Completa (com Penhoras)",
		fetch=fetch_processos_completa,
		price_reference=3.30,
		enabled_field="processos_completa_enabled",
		tier=3,
	),
	"nivel_socioeconomico": PremiumBureau(
		key="nivel_socioeconomico",
		label="Nível Socioeconômico e Renda Presumida PF",
		fetch=fetch_nivel_socioeconomico,
		price_reference=0.36,
		enabled_field="nivel_socioeconomico_enabled",
		tier=1,
	),
}
