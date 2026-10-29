"""Integração sob demanda com Bureaus Finais de Crédito (Serasa Experian e Assertiva Localize).

Regra de Segurança Arquitetural:
Consultas a bureaus finais (tarifadas por relatório completo, R$ 25 a R$ 40) só podem ser acionadas
quando o lead estiver estritamente 'Pre-Approved' (Aprovado no Pré-Filtro) da Camada 3.
"""

from __future__ import annotations

from typing import Any


class BureauGateBlocked(Exception):
	"""Disparada quando uma consulta a birô caro é tentada sem aprovação prévia no pré-filtro."""


def assert_pre_approved_for_bureau(verdict: str, cnpj: str) -> None:
	"""Garante que o CNPJ passou na esteira preliminar antes de consumir birô caro."""
	if verdict != "Pre-Approved":
		raise BureauGateBlocked(
			f"Consulta ao birô final bloqueada para o CNPJ {cnpj}: parecer atual é '{verdict}'. "
			"Apenas leads com 'Pre-Approved' podem ser consultados para evitar custos desnecessários."
		)


def trigger_serasa_relato(
	cnpj: str,
	user: str,
	verdict: str,
	credentials: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Simula ou aciona a chamada ao Serasa Relato PJ com trava de pré-aprovação."""
	assert_pre_approved_for_bureau(verdict, cnpj)

	# Registra a intenção de consulta autorizada
	return {
		"status": "Solicitado",
		"provider": "Serasa Experian",
		"service": "Relato PJ",
		"cnpj": cnpj,
		"requested_by": user,
		"authorized": True,
		"message": "Consulta autorizada pós-aprovação no pré-filtro.",
	}
