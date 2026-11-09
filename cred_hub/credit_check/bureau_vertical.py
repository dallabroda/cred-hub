"""Motor Vertical de Crédito, Birô de Consultas (PJ/PF), Localize (CPF/CNPJ) e Prospecção B2B integrado ao HubTech CRM.

Permite que a aplicação web dedicada (em seu próprio subdomínio) e o HubTech CRM compartilhem:
1. Catálogo oficial de 6 Produtos sobre BigDataCorp Core + OpenCNPJ:
   - `PJ_BASICO` (COGS R$ 0,36 a R$ 0,46)
   - `PJ_COMPLETO` (COGS R$ 0,80 a R$ 0,90 no Core BDC + Protestos IEPTB c/ Smart Gating)
   - `PF_BASICO` (COGS R$ 0,48)
   - `PF_COMPLETO` (COGS R$ 0,86)
   - `LOCALIZE_TELEFONES` (COGS R$ 0,10 no CPF | R$ 0,16 fixos no CNPJ p/ empresa + todos os sócios)
   - `LOCALIZE_COMPLETO` (COGS R$ 0,22 no CPF | R$ 0,40 fixos no CNPJ p/ empresa + todos os sócios c/ CEP CENPROT)
2. Motor de Crédito Automatizado (Política + Cálculo de Limite Sugerido + Veredito + Condicionantes).
3. Gerador de Leads B2B na base da Receita Federal via BigQuery (`basedosdados.br_rf_cnpj`).
4. Sincronização Nativa 1-Clique com o HubTech CRM (`CRM Organization`, `Contact` dos sócios c/ WhatsApp e CENPROT, e `CRM Deal`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import re
from typing import Any

from cred_hub.cnpj.mapping import format_cnpj, is_valid_cnpj, only_digits
from cred_hub.credit_check.clients.bigdatacorp import (
	fetch_localize_completo,
	fetch_localize_telefones,
	fetch_pf_basico,
	fetch_pf_completo,
	fetch_pj_basico,
	fetch_pj_completo,
)
from cred_hub.credit_check.platform_credentials import platform_bigdatacorp_token
from cred_hub.party.document import format_cpf, is_valid_cpf


@dataclass(frozen=True)
class BureauProductSpec:
	key: str
	label: str
	category: str  # "bureau_pj" | "bureau_pf" | "localize"
	accepts_cpf: bool
	accepts_cnpj: bool
	cogs_cpf: float
	cogs_cnpj: float
	suggested_price_brl: float
	description: str


BUREAU_PRODUCTS: dict[str, BureauProductSpec] = {
	"PJ_BASICO": BureauProductSpec(
		key="PJ_BASICO",
		label="Birô PJ Básico (Score Core + Fiscal + Processos + QSA)",
		category="bureau_pj",
		accepts_cpf=False,
		accepts_cnpj=True,
		cogs_cpf=0.0,
		cogs_cnpj=0.46,
		suggested_price_brl=4.90,
		description=(
			"Cadastro RFB, Sintegra Inteligente, Quadro Societário (CPF de 11 dígitos desmascarado), "
			"Faturamento Presumido, Cobranças Ativas, PGFN/FGTS, Processos Judiciais, KYC/Sanções e Score Proprietário (0–1000)."
		),
	),
	"PJ_COMPLETO": BureauProductSpec(
		key="PJ_COMPLETO",
		label="Birô PJ Avançado (Score + 7 Indicadores + Sócios + Grupo + Protestos)",
		category="bureau_pj",
		accepts_cpf=False,
		accepts_cnpj=True,
		cogs_cpf=0.0,
		cogs_cnpj=0.90,
		suggested_price_brl=12.90,
		description=(
			"Tudo do PJ Básico + Evolução da Empresa (1 a 3 anos), Comportamento Financeiro Digital (Busca por Crédito, Cartão, Banking), "
			"Processos e KYC dos Sócios, Grupo Econômico, Exposição Adversa e Protestos IEPTB com Smart Gating."
		),
	),
	"PF_BASICO": BureauProductSpec(
		key="PF_BASICO",
		label="Birô PF Básico (Score Risco + Renda + PGFN + Processos + KYC)",
		category="bureau_pf",
		accepts_cpf=True,
		accepts_cnpj=False,
		cogs_cpf=0.48,
		cogs_cnpj=0.0,
		suggested_price_brl=3.90,
		description=(
			"Cadastro RFB, Endereços e Telefones, Score de Risco Financeiro (0–1000), Renda e Patrimônio Estimados, "
			"Indicador Preditivo de Inadimplência, Dívida PGFN/FGTS, Processos Judiciais e KYC/Sanções."
		),
	),
	"PF_COMPLETO": BureauProductSpec(
		key="PF_COMPLETO",
		label="Birô PF Avançado (Score + Risco Familiar + Veículos + Comportamento Digital)",
		category="bureau_pf",
		accepts_cpf=True,
		accepts_cnpj=False,
		cogs_cpf=0.86,
		cogs_cnpj=0.0,
		suggested_price_brl=7.90,
		description=(
			"Tudo do PF Básico + Risco do Núcleo Familiar, Comportamento Financeiro Digital PF, "
			"Veículos Vinculados ao CPF, Vínculo como Servidor Público e Exposição Negativa Global."
		),
	),
	"LOCALIZE_TELEFONES": BureauProductSpec(
		key="LOCALIZE_TELEFONES",
		label="Localize Só Telefones & WhatsApp (CPF ou CNPJ c/ Sócios)",
		category="localize",
		accepts_cpf=True,
		accepts_cnpj=True,
		cogs_cpf=0.10,
		cogs_cnpj=0.16,
		suggested_price_brl=1.50,
		description=(
			"Localiza celulares e fixos validados na base ABRT com operadora, score de assertividade (0–100), "
			"indicador de WhatsApp e alerta Não Perturbe (PROCON). No CNPJ, traz os telefones da empresa e de TODOS os sócios com CPF desmascarado."
		),
	),
	"LOCALIZE_COMPLETO": BureauProductSpec(
		key="LOCALIZE_COMPLETO",
		label="Localize Completo: Telefones, WhatsApp, E-mails & Endereços CENPROT (CPF ou CNPJ)",
		category="localize",
		accepts_cpf=True,
		accepts_cnpj=True,
		cogs_cpf=0.22,
		cogs_cnpj=0.40,
		suggested_price_brl=2.90,
		description=(
			"Tudo do Localize Telefones + E-mails validados e Endereços residenciais completos com CEP de 8 dígitos "
			"(prontos para envio de títulos a protesto na CENPROT). No CNPJ, retorna empresa + todos os sócios de uma só vez."
		),
	),
}


# ==============================================================================
# 1. MOTOR DE CRÉDITO AUTOMATIZADO (POLÍTICA + LIMITE SUGERIDO + CONDICIONANTES)
# ==============================================================================

REVENUE_RANGE_MONTHLY_ESTIMATE_BRL: list[tuple[re.Pattern[str], float]] = [
	(re.compile(r"ACIMA DE.*500\s*M|BILH", re.I), 45_000_000.0),
	(re.compile(r"100\s*M.*500\s*M", re.I), 15_000_000.0),
	(re.compile(r"50\s*M.*100\s*M", re.I), 5_000_000.0),
	(re.compile(r"10\s*M.*50\s*M|10\s*M.*30\s*M", re.I), 1_500_000.0),
	(re.compile(r"4[.,]8\s*M.*10\s*M|5\s*M.*10\s*M", re.I), 550_000.0),
	(re.compile(r"1[.,]2\s*M.*2[.,]4\s*M|1\s*M.*5\s*M|360\s*K.*4[.,]8\s*M", re.I), 150_000.0),
	(re.compile(r"360\s*K|360\s*MIL|500\s*K", re.I), 40_000.0),
	(re.compile(r"81\s*K|81\s*MIL|MEI|100\s*K", re.I), 8_000.0),
]

INCOME_RANGE_PF_MONTHLY_ESTIMATE_BRL: list[tuple[re.Pattern[str], float]] = [
	(re.compile(r"20[.\s]*000|25[.\s]*000|30[.\s]*000|ACIMA", re.I), 22_000.0),
	(re.compile(r"10[.\s]*000.*15[.\s]*000|10[.\s]*000.*20[.\s]*000", re.I), 12_500.0),
	(re.compile(r"5[.\s]*000.*10[.\s]*000", re.I), 7_000.0),
	(re.compile(r"2[.\s]*000.*5[.\s]*000|3[.\s]*000", re.I), 3_500.0),
]


def estimate_monthly_capacity_brl(report_data: dict[str, Any]) -> float:
	"""Estima a capacidade mensal (faturamento mensal PJ ou renda mensal PF) em R$ para cálculo de limite."""
	plano = str(report_data.get("plano") or "")
	if plano.startswith("PF"):
		raw_income = str(report_data.get("renda_estimada") or "")
		for pattern, monthly_val in INCOME_RANGE_PF_MONTHLY_ESTIMATE_BRL:
			if pattern.search(raw_income):
				return monthly_val
		return 2_500.0

	cap = report_data.get("capacidade_economica") or {}
	raw_rev = str(cap.get("estimated_revenue_range") or "")
	for pattern, monthly_val in REVENUE_RANGE_MONTHLY_ESTIMATE_BRL:
		if pattern.search(raw_rev):
			return monthly_val
	return 25_000.0


def evaluate_with_credit_engine(
	report_data: dict[str, Any],
	policy_settings: dict[str, Any] | None = None,
	requested_amount: float = 0.0,
) -> dict[str, Any]:
	"""Aplica o Motor de Crédito sobre o retorno dos produtos `PJ_BASICO`, `PJ_COMPLETO`, `PF_BASICO` ou `PF_COMPLETO`."""
	cfg = policy_settings or {}
	score_block = report_data.get("score") or {}
	score = int(score_block.get("score") or 0)
	band = str(score_block.get("faixa_score") or "")

	reasons: list[str] = []
	warnings: list[str] = []
	conditions: list[str] = []

	# 1. Curto-circuito cadastral (Baixada / Inapta / Suspensa)
	if report_data.get("short_circuited"):
		reasons.append(str(report_data.get("short_circuit_reason") or "Situação cadastral irregular na Receita Federal."))

	# 2. Falência / Recuperação Judicial
	processos = report_data.get("processos") or {}
	if processos.get("has_bankruptcy_recovery"):
		if cfg.get("block_on_bankruptcy", True):
			reasons.append("Registro de falência ou recuperação judicial identificado.")
		else:
			warnings.append("Alerta de falência ou recuperação judicial identificado.")

	# 3. Compliance / Sanções (CEIS, CNEP, OFAC, ONU, Trabalho Escravo)
	compliance = report_data.get("compliance") or {}
	if compliance.get("sanctioned"):
		if cfg.get("block_on_ceis_cnep", True):
			reasons.append("Entidade com sanção ativa em listas restritivas (CEIS/CNEP/OFAC/MTE).")
		else:
			warnings.append("Entidade consta em listas restritivas de compliance.")

	# 4. Dívida Ativa da União (PGFN / FGTS)
	fiscal = report_data.get("fiscal") or {}
	pgfn_debt = float((fiscal.get("pgfn") or {}).get("total_debt") or 0.0)
	max_pgfn = float(cfg.get("max_pgfn_debt") or 0.0)
	if pgfn_debt > 0:
		if max_pgfn > 0 and pgfn_debt > max_pgfn:
			reasons.append(f"Dívida ativa na PGFN (R$ {pgfn_debt:,.2f}) excede o teto da política (R$ {max_pgfn:,.2f}).")
		else:
			warnings.append(f"Possui inscrição ativa na PGFN no valor de R$ {pgfn_debt:,.2f}.")
			conditions.append("Exigir comprovante de parcelamento regular da PGFN ou garantia real/aval.")

	# 5. Sintegra / Inscrição Estadual (PJ)
	sintegra = report_data.get("sintegra") or {}
	if report_data.get("sintegra_consultado") and sintegra.get("inscricoes") and sintegra.get("habilitada") is False:
		warnings.append("Inscrição Estadual (Sintegra/SEFAZ) não habilitada para emissão de NF-e.")
		conditions.append("Regularizar Inscrição Estadual na SEFAZ antes de operar mercadorias.")

	# 6. Cobranças Ativas (Collections BDC) e Inadimplência PF
	cobrancas = report_data.get("cobrancas") or {}
	if cobrancas.get("is_currently_on_collection"):
		warnings.append("Empresa em cobrança ativa no mercado no momento.")
		conditions.append("Operar apenas mediante confirmação de lastro (NF-e/canhoto) e aval dos sócios administradores.")
	if report_data.get("provavel_inadimplente"):
		warnings.append("Indicador preditivo aponta alta probabilidade de inadimplência (LikelyInDebt).")

	# 7. Processos Judiciais no Polo Passivo
	def_count = int(processos.get("defendant_count") or 0)
	max_def = int(cfg.get("max_lawsuits_defendant") or 0)
	if def_count > 0:
		if max_def > 0 and def_count > max_def:
			reasons.append(f"Quantidade de processos como réu ({def_count}) supera o limite da política ({max_def}).")
		else:
			warnings.append(f"Consta no polo passivo em {def_count} processo(s) judicial(is).")

	# 8. Risco dos Sócios (PJ Completo)
	grupo = report_data.get("risco_socios_e_grupo") or {}
	socios_sancionados = int(((grupo.get("kyc_socios") or {}).get("socios_sancionados") or 0))
	if socios_sancionados > 0:
		if cfg.get("alert_partner_restrictions", 1):
			warnings.append(f"{socios_sancionados} sócio(s) possui(em) sanção ativa em listas restritivas.")
			conditions.append("Vedar aval isolado de sócio sancionado; exigir garantia complementar.")

	# 8b. Protestos em Cartório IEPTB (PJ Completo com Smart Gating)
	protestos = report_data.get("protestos") or {}
	if protestos.get("has_protests"):
		prot_count = int(protestos.get("count") or 0)
		prot_val = float(protestos.get("total_value") or 0.0)
		if cfg.get("block_on_protest", False):
			reasons.append(f"Possui {prot_count} título(s) protestado(s) em cartório (total R$ {prot_val:,.2f}).")
		else:
			warnings.append(f"Possui {prot_count} título(s) protestado(s) em cartório (total R$ {prot_val:,.2f}).")
			conditions.append("Exigir certidão de baixa/anuência dos protestos ou garantia real/aval líquido.")

	# 9. Score Mínimo
	min_score = int(cfg.get("min_score") or 400)
	if score < min_score:
		reasons.append(f"Score calculado ({score}) abaixo do piso mínimo de aprovação ({min_score}).")

	# Cálculo do Limite Sugerido em R$
	monthly_cap = estimate_monthly_capacity_brl(report_data)
	if reasons or score < 400:
		suggested_limit = 0.0
	elif score >= 800 or "A" in band:
		suggested_limit = round(monthly_cap * 0.15, -2)
	elif score >= 700 or "B" in band:
		suggested_limit = round(monthly_cap * 0.10, -2)
	elif score >= 550 or "C" in band:
		suggested_limit = round(monthly_cap * 0.05, -2)
	else:
		suggested_limit = 0.0

	if requested_amount > 0 and suggested_limit > 0 and requested_amount > suggested_limit:
		warnings.append(
			f"Valor solicitado na operação (R$ {requested_amount:,.2f}) supera o limite técnico sugerido (R$ {suggested_limit:,.2f})."
		)
		conditions.append(f"Reduzir exposição inicial para R$ {suggested_limit:,.2f} ou exigir garantia adicional.")

	if reasons:
		verdict = "Reject"
		verdict_label = "Reprovado"
	elif warnings:
		verdict = "Approve with reservations"
		verdict_label = "Aprovado com Ressalvas"
	else:
		verdict = "Approve"
		verdict_label = "Aprovado"
		if not conditions:
			conditions.append("Aprovação padrão dentro do limite sugerido e acompanhamento mensal de carteira.")

	return {
		"verdict": verdict,
		"verdict_label": verdict_label,
		"score": score,
		"faixa_score": band,
		"suggested_limit_brl": suggested_limit,
		"estimated_monthly_capacity_brl": monthly_cap,
		"reasons": reasons,
		"warnings": warnings,
		"recommended_conditions": conditions[:4],
	}


# ==============================================================================
# 2. GERADOR DE LEADS B2B (RECEITA FEDERAL NO BIGQUERY: basedosdados.br_rf_cnpj)
# ==============================================================================

DEFAULT_BQ_PROJECT = "credtech-512315"
DEFAULT_BQ_DATASET = "basedosdados.br_rf_cnpj"


def build_bigquery_leads_sql(
	uf: str = "",
	cnaes: list[str] | None = None,
	id_municipio: str = "",
	cep_prefix: str = "",
	portes: list[str] | None = None,
	capital_social_min: float = 0.0,
	capital_social_max: float = 0.0,
	somente_simples: bool = False,
	excluir_mei: bool = True,
	idade_minima_anos: int = 0,
	somente_com_telefone: bool = False,
	limit: int = 100,
	dataset: str = DEFAULT_BQ_DATASET,
) -> tuple[str, dict[str, Any]]:
	"""Gera a query SQL segura e parametrizada para buscar leads B2B ativos na Receita Federal via BigQuery."""
	uf_clean = re.sub(r"[^A-Z]", "", (uf or "").strip().upper())[:2]
	mun_clean = only_digits(id_municipio)
	cep_clean = only_digits(cep_prefix)[:8]
	cnae_list = [only_digits(c) for c in (cnaes or []) if len(only_digits(c)) >= 5]

	porte_map = {"ME": "01", "EPP": "03", "DEMAIS": "05", "OTHER SIZE": "05", "01": "01", "03": "03", "05": "05", "1": "01", "3": "03", "5": "05"}
	porte_codes = sorted({porte_map[p.strip().upper()] for p in (portes or []) if p.strip().upper() in porte_map})

	safe_limit = max(1, min(int(limit or 100), 500))
	where_clauses = [
		"est.situacao_cadastral = '2'",  # Somente empresas ATIVAS na Receita Federal
	]
	params: dict[str, Any] = {"limit": safe_limit}

	if uf_clean:
		where_clauses.append("est.sigla_uf = @uf")
		params["uf"] = uf_clean
	if mun_clean:
		where_clauses.append("est.id_municipio = @id_municipio")
		params["id_municipio"] = mun_clean
	if cep_clean:
		where_clauses.append("STARTS_WITH(est.cep, @cep_prefix)")
		params["cep_prefix"] = cep_clean
	if cnae_list:
		where_clauses.append("est.cnae_fiscal_principal IN UNNEST(@cnaes)")
		params["cnaes"] = cnae_list
	if porte_codes:
		where_clauses.append("emp.porte IN UNNEST(@portes)")
		params["portes"] = porte_codes
	if float(capital_social_min or 0) > 0:
		where_clauses.append("emp.capital_social >= @capital_min")
		params["capital_min"] = float(capital_social_min)
	if float(capital_social_max or 0) > 0:
		where_clauses.append("emp.capital_social <= @capital_max")
		params["capital_max"] = float(capital_social_max)
	if excluir_mei:
		where_clauses.append("COALESCE(simp.opcao_mei, 0) = 0")
	if somente_simples:
		where_clauses.append("simp.opcao_simples = 1")
	if int(idade_minima_anos or 0) > 0:
		max_start_year = date.today().year - int(idade_minima_anos)
		where_clauses.append("est.data_inicio_atividade <= DATE(@max_start_date)")
		params["max_start_date"] = f"{max_start_year:04d}-{date.today().month:02d}-{date.today().day:02d}"
	if somente_com_telefone:
		where_clauses.append("(est.telefone_1 IS NOT NULL AND est.telefone_1 != '')")

	where_sql = "\n  AND ".join(where_clauses)
	sql = f"""
WITH latest_est AS (
  SELECT *
  FROM `{dataset}.estabelecimentos`
  WHERE `{dataset}.estabelecimentos`.ano = (SELECT MAX(ano) FROM `{dataset}.estabelecimentos`)
),
latest_emp AS (
  SELECT cnpj_basico, razao_social, natureza_juridica, capital_social, porte
  FROM `{dataset}.empresas`
  WHERE `{dataset}.empresas`.ano = (SELECT MAX(ano) FROM `{dataset}.empresas`)
),
latest_simp AS (
  SELECT cnpj_basico, opcao_simples, opcao_mei
  FROM `{dataset}.simples`
)
SELECT
  est.cnpj,
  emp.razao_social,
  est.nome_fantasia,
  est.cnae_fiscal_principal,
  emp.porte,
  emp.capital_social,
  est.data_inicio_atividade,
  est.sigla_uf AS uf,
  est.id_municipio,
  est.bairro,
  est.cep,
  est.ddd_1,
  est.telefone_1,
  est.email,
  COALESCE(simp.opcao_simples, 0) AS opcao_simples,
  COALESCE(simp.opcao_mei, 0) AS opcao_mei
FROM latest_est AS est
INNER JOIN latest_emp AS emp ON est.cnpj_basico = emp.cnpj_basico
LEFT JOIN latest_simp AS simp ON est.cnpj_basico = simp.cnpj_basico
WHERE {where_sql}
ORDER BY emp.capital_social DESC
LIMIT @limit
""".strip()
	return sql, params


# ==============================================================================
# 3. EXECUÇÃO UNIFICADA DOS 6 PRODUTOS + INTEGRAÇÃO NATIVA COM O HUBTECH CRM
# ==============================================================================


def run_product_query(
	product_key: str,
	document: str,
	token: str,
	opencnpj_data: dict[str, Any] | None = None,
	directd_token: str = "",
	policy_settings: dict[str, Any] | None = None,
	requested_amount: float = 0.0,
) -> dict[str, Any]:
	"""Executa qualquer um dos 6 produtos oficiais e, para os 4 produtos de Birô (PJ/PF),
	já anexa a decisão do Motor de Crédito (`motor_credito`)."""
	key = (product_key or "").strip().upper()
	if key not in BUREAU_PRODUCTS:
		raise ValueError(f"Produto inválido '{product_key}'. Opções válidas: {', '.join(BUREAU_PRODUCTS)}")

	spec = BUREAU_PRODUCTS[key]
	digits = only_digits(document)
	if len(digits) == 11 and not spec.accepts_cpf:
		raise ValueError(f"O produto {key} aceita apenas CNPJ (14 dígitos).")
	if len(digits) == 14 and not spec.accepts_cnpj:
		raise ValueError(f"O produto {key} aceita apenas CPF (11 dígitos).")
	if len(digits) not in (11, 14):
		raise ValueError(f"Documento inválido: {document}. Informe um CPF (11 dígitos) ou CNPJ (14 dígitos).")

	if key == "PJ_BASICO":
		result = fetch_pj_basico(digits, token, opencnpj_data=opencnpj_data)
	elif key == "PJ_COMPLETO":
		result = fetch_pj_completo(digits, token, opencnpj_data=opencnpj_data, directd_token=directd_token)
	elif key == "PF_BASICO":
		result = fetch_pf_basico(digits, token)
	elif key == "PF_COMPLETO":
		result = fetch_pf_completo(digits, token)
	elif key == "LOCALIZE_TELEFONES":
		result = fetch_localize_telefones(digits, token, opencnpj_data=opencnpj_data)
	else:
		result = fetch_localize_completo(digits, token, opencnpj_data=opencnpj_data)

	if spec.category in ("bureau_pj", "bureau_pf"):
		result["motor_credito"] = evaluate_with_credit_engine(
			result,
			policy_settings=policy_settings,
			requested_amount=requested_amount,
		)
	return result


def sync_result_to_hubtech_crm(
	result: dict[str, Any],
	create_deal: bool = False,
	deal_stage: str = "",
) -> dict[str, Any]:
	"""Sincroniza nativamente o retorno de qualquer um dos 6 produtos com o banco do HubTech CRM:
	- Cria ou atualiza a `CRM Organization` (quando CNPJ) e seus `Contact`s (sócios PF com WhatsApp e endereço CENPROT).
	- Cria ou atualiza o `Contact` individual (quando CPF).
	- Se `create_deal=True`, abre um `CRM Deal` no funil do HubTech CRM já vinculado à Organização/Contato.
	"""
	import frappe
	from cred_hub.cnpj.enrich import person_name
	from cred_hub.integrations.localize import upsert_contact

	plano = str(result.get("plano") or "")
	cnpj_digits = only_digits(result.get("cnpj") or (result.get("documento") if result.get("tipo_documento") == "CNPJ" else ""))
	cpf_digits = only_digits(result.get("cpf") or (result.get("documento") if result.get("tipo_documento") == "CPF" else ""))

	org_name = None
	contacts_synced = 0
	deal_name = None

	if len(cnpj_digits) == 14 and is_valid_cnpj(cnpj_digits):
		formatted_cnpj = format_cnpj(cnpj_digits)
		org_name = frappe.db.get_value("CRM Organization", {"cnpj": formatted_cnpj}, "name") or frappe.db.get_value(
			"CRM Organization", {"cnpj": cnpj_digits}, "name"
		)
		if org_name:
			org = frappe.get_doc("CRM Organization", org_name)
		else:
			org = frappe.new_doc("CRM Organization")
			org.organization_name = (
				result.get("nome")
				or result.get("razao_social")
				or f"Empresa {formatted_cnpj}"
			)
			org.cnpj = formatted_cnpj
			org.insert(ignore_permissions=True)
			org_name = org.name

		raw_payload = result.get("raw") if isinstance(result.get("raw"), dict) else {}
		protocol = str(result.get("query_id") or "")
		socios = result.get("socios") or result.get("quadro_societario") or []
		for partner in socios:
			if not isinstance(partner, dict) or not partner.get("cpf"):
				continue
			partner_norm = {
				"cpf": only_digits(partner.get("cpf")),
				"name": partner.get("nome") or partner.get("name") or "",
				"qualification": partner.get("qualificacao") or partner.get("qualification") or "Sócio",
				"share": partner.get("participacao") if "participacao" in partner else partner.get("share"),
			}
			inline_person = partner.get("inline_person_payload")
			if not inline_person and raw_payload:
				from cred_hub.integrations.bigdatacorp import extract_partner_inline_payload

				inline_person = extract_partner_inline_payload(raw_payload, partner_norm["cpf"])
			upsert_contact(org, partner_norm, inline_person, protocol)
			contacts_synced += 1

		if create_deal:
			deal = frappe.new_doc("CRM Deal")
			deal.organization = org.name
			if deal_stage and frappe.db.exists("CRM Deal Status", deal_stage):
				deal.status = deal_stage
			deal.insert(ignore_permissions=True)
			deal_name = deal.name
			motor = result.get("motor_credito") or {}
			if motor:
				deal.add_comment(
					"Comment",
					f"Birô/Motor de Crédito ({plano}): {motor.get('verdict_label')} | Score {motor.get('score')} ({motor.get('faixa_score')}) | Limite Sugerido: R$ {motor.get('suggested_limit_brl', 0):,.2f}",
				)

	elif len(cpf_digits) == 11 and is_valid_cpf(cpf_digits):
		formatted_cpf = format_cpf(cpf_digits)
		contact_name = frappe.db.get_value("Contact", {"cpf": formatted_cpf}, "name")
		if contact_name:
			contact = frappe.get_doc("Contact", contact_name)
		else:
			contact = frappe.new_doc("Contact")
			contact.first_name = person_name(result.get("nome") or f"CPF {formatted_cpf}")
			contact.cpf = formatted_cpf
			contact.insert(ignore_permissions=True)
		contacts_synced = 1

	return {
		"ok": True,
		"plano": plano,
		"organization": org_name,
		"contacts_synced": contacts_synced,
		"deal": deal_name,
	}


def get_catalog_dict() -> list[dict[str, Any]]:
	"""Retorna o catálogo serializável dos 6 produtos para o frontend da vertical e do HubTech CRM."""
	return [asdict(spec) for spec in BUREAU_PRODUCTS.values()]
