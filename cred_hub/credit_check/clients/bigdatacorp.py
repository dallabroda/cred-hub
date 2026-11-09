"""Cliente HTTP e Normalizador para a Plataforma BigDataCorp — Esteira de Crédito & Bureau Próprio (100% Core, Sem Quod/Quantum).

Prepara o backend do HubTech CRM (e do futuro Portal Independente do Bureau) para substituir
todas as integrações de consulta de crédito e compliance (Escavador, Infosimples, DirectD,
Serasa e CGU) usando exclusivamente os datasets Core e On-Demand da BigDataCorp + OpenCNPJ
(sem depender de bureaus terceiros caros do Marketplace como Quod ou Quantum), mantendo
apenas a CENPROT para envio de títulos a protesto.

Níveis de Produto implementados (todos com 500 consultas gratuitas/mês nos datasets Core).
Custos conferidos na documentação da BigDataCorp em 08/11/2026 (faixa 1–10.000 consultas/mês):
1. `fetch_pj_basico(cnpj, token)` — COGS R$ 0,36 (ou R$ 0,46 c/ Sintegra via `/ondemand` `ondemand_sintegra_company`):
   Chama `/empresas` em lote único (`history_basic_data,relationships,kyc,
   activity_indicators,collections,government_debtors,lawsuits_distribution_data`) e calcula
   o Score Proprietário PJ (0–1000).
2. `fetch_pj_completo(cnpj, token)` — COGS R$ 0,80 (R$ 0,90 c/ Sintegra; 100% Core BDC, zero Quod/Quantum):
   Adiciona ao Básico os datasets avançados da BigDataCorp:
   - `company_evolution` (R$ 0,06): Evolução histórica de funcionários, capital social, faturamento e crescimento YoY (1, 3 e 5 anos)
   - `digital_finance_behaviors` (R$ 0,06): Intensidade de busca por crédito (`CreditSeeker`), score de cartão (`CreditCardScore`), internet banking e meios de pagamento (notas A a H convertidas em termômetro 0–100)
   - `owners_lawsuits_distribution_data` (R$ 0,10): Raio-X de processos judiciais nas costas dos sócios
   - `owners_kyc` (R$ 0,10): PEP e sanções de cada sócio PF (penaliza o score se algum sócio tiver sanção ativa)
   - `economic_group_relationships` (R$ 0,06): Mapeamento completo do grupo econômico (1º, 2º e 3º grau)
   - `global_adverse_exposure` (R$ 0,06): Presença em vazamentos e documentos públicos adversos (ICIJ, offshore leaks etc.)
   - `processes` (R$ 0,08, sob demanda): Lista completa de processos judiciais com CNJ, partes e valores
3. `fetch_pf_basico(cpf, token)` — COGS R$ 0,48:
   Chama `/pessoas` (`basic_data,addresses_extended,phones_extended,financial_risk,
   indebtedness_question,government_debtors,lawsuits_distribution_data,kyc`).
4. `fetch_pf_completo(cpf, token)` — COGS R$ 0,86 (ou R$ 0,94 com `processes` detalhado; 100% Core BDC):
   Adiciona ao PF Básico: `digital_finance_behaviors` (R$ 0,06), `family_financial_risk` (R$ 0,14 — risco do núcleo familiar),
   `vehicles` (R$ 0,06 — veículos vinculados ao CPF), `profession_data` (R$ 0,06) e `global_adverse_exposure` (R$ 0,06).
"""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any

import requests

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.clients.directd import DirectDError, fetch_protests_nacional
from cred_hub.credit_check.raw_capture import record
from cred_hub.integrations.bigdatacorp import (
	DEFAULT_BASE_URL,
	addresses as extract_addresses,
	emails as extract_emails,
	first_entity,
	parse_credentials,
	partners as extract_partners,
	phones as extract_phones,
)

DEFAULT_TIMEOUT = 25

# Regra 1: Sem `basic_data` (R$ 0,02) no PJ pois OpenCNPJ já entrega Razão Social, CNAE, Abertura, Porte e Capital Social de graça!
PJ_BASICO_DATASETS = (
	"history_basic_data,"
	"relationships,"
	"kyc,"
	"activity_indicators,"
	"collections,"
	"government_debtors,"
	"lawsuits_distribution_data"
)

# Regra 4: `processes` detalhado (R$ 0,08) é chamado sob demanda apenas se `defendant_count > 0`
PJ_COMPLETO_EXTRA_DATASETS = (
	"company_evolution,"
	"digital_finance_behaviors,"
	"owners_lawsuits_distribution_data,"
	"owners_kyc,"
	"economic_group_relationships,"
	"global_adverse_exposure"
)

PJ_COMPLETO_ALL_DATASETS = f"{PJ_BASICO_DATASETS},{PJ_COMPLETO_EXTRA_DATASETS}"

PF_BASICO_DATASETS = (
	"basic_data,"
	"addresses_extended,"
	"phones_extended,"
	"financial_risk,"
	"indebtedness_question,"
	"government_debtors,"
	"lawsuits_distribution_data,"
	"kyc"
)

# Regra 4 (PF): `processes` detalhado (R$ 0,08) é chamado sob demanda apenas se `defendant_count > 0`
PF_COMPLETO_EXTRA_DATASETS = (
	"digital_finance_behaviors,"
	"family_financial_risk,"
	"vehicles,"
	"profession_data,"
	"global_adverse_exposure"
)

PF_COMPLETO_ALL_DATASETS = f"{PF_BASICO_DATASETS},{PF_COMPLETO_EXTRA_DATASETS}"

# Produto 5: Localize Só Telefones / WhatsApp (CPF = R$ 0,10 | CNPJ = R$ 0,16 fixos p/ empresa + todos os sócios)
LOCALIZE_TELEFONES_CPF_DATASETS = "basic_data,phones_extended"
LOCALIZE_TELEFONES_CNPJ_DATASETS = "relationships,phones_extended,related_people_phones"

# Produto 6: Localize Completo c/ E-mails + Endereços CENPROT (CPF = R$ 0,22 | CNPJ = R$ 0,40 fixos p/ empresa + todos os sócios)
LOCALIZE_COMPLETO_CPF_DATASETS = "basic_data,phones_extended,emails_extended,addresses_extended"
LOCALIZE_COMPLETO_CNPJ_DATASETS = (
	"relationships,"
	"phones_extended,"
	"related_people_phones,"
	"emails_extended,"
	"related_people_emails,"
	"addresses_extended,"
	"related_people_addresses"
)

# Divisões CNAE (2 primeiros dígitos) 100% Prestação de Serviços / Isentas de Inscrição Estadual (Sintegra)
PURE_SERVICE_CNAE_DIVISIONS = frozenset(
	list(range(41, 44))  # Construção Civil (ISS)
	+ [55]               # Alojamento / Hotelaria
	+ [59, 60]           # Audiovisual / Rádio / TV
	+ list(range(62, 100))  # TI, Financeiro, Imobiliário, Jurídico, Contábil, Consultoria, Saúde, Educação, Serviços
)

INACTIVE_RECEITA_STATUSES = frozenset({"BAIXADA", "INAPTA", "SUSPENSA", "NULA"})

# Preços da faixa 1–10.000 consultas/mês, conferidos na documentação oficial da BigDataCorp
# (exemplos de chamadas api/bigdatacorp/plataforma/reference) em 08/11/2026.
# `basic_data` custa R$ 0,03 em /empresas e R$ 0,04 em /pessoas — usamos o maior para não subestimar.
DATASET_UNIT_COSTS = {
	"basic_data": 0.04,
	"history_basic_data": 0.04,
	"relationships": 0.04,
	"kyc": 0.06,
	"activity_indicators": 0.06,
	"collections": 0.06,
	"government_debtors": 0.06,
	"lawsuits_distribution_data": 0.04,
	"company_evolution": 0.06,
	"digital_finance_behaviors": 0.06,
	"owners_lawsuits_distribution_data": 0.10,
	"owners_kyc": 0.10,
	"economic_group_relationships": 0.06,
	"global_adverse_exposure": 0.06,
	"processes": 0.08,
	"ondemand_sintegra_company": 0.10,
	"addresses_extended": 0.06,
	"phones_extended": 0.06,
	"emails_extended": 0.06,
	"related_people_phones": 0.06,
	"related_people_emails": 0.06,
	"related_people_addresses": 0.06,
	"financial_risk": 0.06,
	"indebtedness_question": 0.10,
	"family_financial_risk": 0.14,
	"vehicles": 0.06,
	"profession_data": 0.06,
}

DIRECTD_PROTESTS_BASICA_COST = 2.95


def estimate_datasets_cost(datasets_csv: str, include_sintegra: bool = False) -> float:
	"""Calcula o COGS estimado com base nos datasets efetivamente disparados."""
	items = [d.strip() for d in (datasets_csv or "").split(",") if d.strip()]
	total = sum(DATASET_UNIT_COSTS.get(d, 0.04) for d in items)
	if include_sintegra:
		total += DATASET_UNIT_COSTS["ondemand_sintegra_company"]
	return round(total, 2)


def is_inactive_on_receita(opencnpj_data: dict[str, Any] | None) -> bool:
	"""Regra 2: Verifica se a empresa já está Baixada/Inapta/Suspensa/Nula no OpenCNPJ gratuito."""
	if not isinstance(opencnpj_data, dict):
		return False
	status = str(
		opencnpj_data.get("situacao_cadastral")
		or opencnpj_data.get("registration_status")
		or ""
	).strip().upper()
	return status in INACTIVE_RECEITA_STATUSES


def requires_sintegra_by_cnae(opencnpj_data: dict[str, Any] | None = None, cnae: Any = None) -> bool:
	"""Regra 3: Sintegra fica padrão (True) para Comércio, Indústria, Agro e Transporte, e pula automaticamente
	quando o CNAE (principal e secundários) for 100% Prestação de Serviços (economizando R$ 0,10)."""
	codes: list[str] = []
	if cnae:
		codes.append(only_digits(str(cnae)))
	if isinstance(opencnpj_data, dict):
		main_cnae = opencnpj_data.get("cnae_fiscal") or opencnpj_data.get("cnae_principal") or opencnpj_data.get("cnae")
		if isinstance(main_cnae, dict):
			main_cnae = main_cnae.get("codigo") or main_cnae.get("code")
		if main_cnae:
			codes.append(only_digits(str(main_cnae)))
		sec_list = opencnpj_data.get("cnaes_secundarios") or []
		if isinstance(sec_list, list):
			for item in sec_list:
				code = item.get("codigo") or item.get("code") if isinstance(item, dict) else item
				if code:
					codes.append(only_digits(str(code)))
	valid_codes = [c for c in codes if len(c) >= 2]
	if not valid_codes:
		return True  # Sem CNAE informado, mantém Sintegra ligado por padrão
	for code in valid_codes:
		division = int(code[:2])
		if division not in PURE_SERVICE_CNAE_DIVISIONS:
			return True
	return False


def _company_has_at_least_one_year(opencnpj_data: dict[str, Any] | None) -> bool:
	"""Regra 5: Retorna False se a empresa tiver menos de 365 dias de abertura no OpenCNPJ."""
	if not isinstance(opencnpj_data, dict):
		return True
	raw_date = str(
		opencnpj_data.get("data_inicio_atividade")
		or opencnpj_data.get("foundation_date")
		or ""
	).strip()
	if not raw_date:
		return True
	for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
		try:
			opened = datetime.strptime(raw_date[:10], fmt).date()
			return (date.today() - opened).days >= 365
		except ValueError:
			continue
	return True


def _company_is_mei(opencnpj_data: dict[str, Any] | None) -> bool:
	"""Regra 6: Retorna True se a empresa for MEI no OpenCNPJ (dispensa consulta de grupo econômico)."""
	if not isinstance(opencnpj_data, dict):
		return False
	if bool(opencnpj_data.get("opcao_pelo_mei") or opencnpj_data.get("opcao_mei") or opencnpj_data.get("is_mei")):
		return True
	porte = str(opencnpj_data.get("porte") or "").upper()
	return "MEI" in porte


def _company_has_pf_partners(opencnpj_data: dict[str, Any] | None) -> bool:
	"""Regra 7: Retorna False quando o QSA do OpenCNPJ contém exclusivamente Sócios Pessoa Jurídica (Holdings/Offshores),
	economizando R$ 0,20 ao pular `owners_lawsuits_distribution_data` e `owners_kyc`."""
	if not isinstance(opencnpj_data, dict):
		return True
	qsa = opencnpj_data.get("qsa") or opencnpj_data.get("socios")
	if not isinstance(qsa, list) or not qsa:
		return True
	for socio in qsa:
		if not isinstance(socio, dict):
			continue
		ident = str(socio.get("identificador_de_socio") or socio.get("tipo_socio") or "").upper()
		doc = only_digits(str(socio.get("cnpj_cpf_do_socio") or socio.get("documento") or ""))
		# No layout da RFB, identificador 2 = Pessoa Física (1 = Pessoa Jurídica, 3 = Estrangeiro)
		if ident in ("2", "PF", "PESSOA FISICA", "PESSOA FÍSICA"):
			return True
		if len(doc) == 11 or "*" in str(socio.get("cnpj_cpf_do_socio") or ""):
			return True
		if ident not in ("1", "PJ", "PESSOA JURIDICA", "PESSOA JURÍDICA") and len(doc) != 14:
			return True
	return False

RE_BANKRUPTCY = re.compile(r"fal[eê]ncia|recupera[cç][aã]o judicial|insolv[eê]ncia|concordata", re.I)

# Mapa de conversão das notas A-H da BigDataCorp (DigitalFinanceBehaviors) para escala 0-100
GRADE_TO_SCORE = {
	"A": 95,
	"B": 82,
	"C": 70,
	"D": 55,
	"E": 40,
	"F": 25,
	"G": 15,
	"H": 5,
}


class BigDataCorpCreditError(Exception):
	"""Erro base na consulta de crédito via BigDataCorp."""


class BigDataCorpCreditAuthError(BigDataCorpCreditError):
	"""Credenciais AccessToken / TokenId ausentes ou inválidas (401/403)."""


def _parse_float_br(val: Any) -> float:
	if val in (None, ""):
		return 0.0
	if isinstance(val, (int, float)):
		return float(val)
	text = str(val).strip()
	if "," in text and "." in text:
		text = text.replace(".", "").replace(",", ".")
	elif "," in text:
		text = text.replace(",", ".")
	try:
		return float(text)
	except ValueError:
		return 0.0


def _grade_to_pct(grade: Any, default: int = 50) -> int:
	key = str(grade or "").strip().upper()[:1]
	return GRADE_TO_SCORE.get(key, default)


def _post_bdc(
	endpoint: str,
	datasets: str,
	q: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
	access_token, token_id = parse_credentials(token)
	if not access_token:
		raise BigDataCorpCreditAuthError("Token da BigDataCorp (AccessToken) não informado.")

	url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
	headers = {
		"AccessToken": access_token,
		"Accept": "application/json",
		"Content-Type": "application/json",
	}
	if token_id:
		headers["TokenId"] = token_id

	body = {"Datasets": datasets, "q": q, "Limit": 1}
	try:
		response = requests.post(url, json=body, headers=headers, timeout=timeout)
	except requests.RequestException as e:
		raise BigDataCorpCreditError(f"Falha de rede ao consultar BigDataCorp ({endpoint}): {e}") from e

	if response.status_code in (401, 403):
		raise BigDataCorpCreditAuthError(f"Credenciais BigDataCorp recusadas ou sem saldo ({response.status_code}).")
	if not response.ok:
		raise BigDataCorpCreditError(f"Erro HTTP {response.status_code} na BigDataCorp ({endpoint}): {response.text[:250]}")

	try:
		payload = response.json()
	except ValueError as e:
		raise BigDataCorpCreditError(f"Resposta não-JSON da BigDataCorp em {endpoint}.") from e

	record("BigDataCorp", f"{endpoint}:{datasets}", payload)
	return payload if isinstance(payload, dict) else {}


# Parsers Normalizadores (Compatíveis com `enrich.py`, `rules.py` e Novo Bureau) ------------------------------


def parse_kyc_compliance(payload: dict[str, Any], min_match: int = 80) -> dict[str, Any]:
	"""Extrai PEP, CEIS, CNEP, CEPIM, OFAC, ONU, EU, UK, Interpol e Trabalho Escravo do dataset `kyc`."""
	entity = first_entity(payload)
	kyc = entity.get("KycData") if isinstance(entity.get("KycData"), dict) else {}
	return _parse_kyc_block(kyc, min_match)


def _parse_kyc_block(kyc: dict[str, Any], min_match: int = 80) -> dict[str, Any]:
	"""Normaliza um bloco KYC da BigDataCorp (mesmo formato em `kyc` e em cada sócio de `owners_kyc`)."""
	is_pep = bool(kyc.get("IsCurrentlyPEP"))
	pep_history = kyc.get("PEPHistory") if isinstance(kyc.get("PEPHistory"), list) else []
	sanctions_raw = kyc.get("SanctionsHistory") if isinstance(kyc.get("SanctionsHistory"), list) else []

	active_sanctions: list[dict[str, Any]] = []
	ceis_count = 0
	cnep_count = 0
	ofac_count = 0
	slave_labor_count = 0

	for item in sanctions_raw:
		if not isinstance(item, dict):
			continue
		match_rate = _parse_float_br(item.get("MatchRate", 100))
		is_active = item.get("IsCurrentlyPresentOnSource", True)
		if match_rate < min_match or not is_active:
			continue

		source = str(item.get("Source") or "").upper()
		std_type = str(item.get("StandardizedSanctionType") or item.get("Type") or "").upper()
		if "CEIS" in source:
			ceis_count += 1
		elif "CNEP" in source:
			cnep_count += 1
		elif "OFAC" in source:
			ofac_count += 1
		elif "MTE" in source or "SLAVERY" in std_type or "ESCRAVO" in std_type:
			slave_labor_count += 1

		active_sanctions.append(
			{
				"source": source,
				"type": item.get("Type") or std_type,
				"standardized_type": std_type,
				"match_rate": match_rate,
				"start_date": item.get("StartDate") or "",
				"end_date": item.get("EndDate") or "",
			}
		)

	return {
		"sanctioned": len(active_sanctions) > 0,
		"is_pep": is_pep,
		"pep_count": len(pep_history),
		"ceis_count": ceis_count,
		"cnep_count": cnep_count,
		"ofac_count": ofac_count,
		"slave_labor_count": slave_labor_count,
		"sanctions": active_sanctions,
		"raw": kyc,
	}


def parse_owners_kyc(payload: dict[str, Any], min_match: int = 80) -> dict[str, Any]:
	"""Extrai PEP e sanções de cada sócio PF do dataset `owners_kyc` (chave `OwnersKycData.PeopleOwnersKycData`)."""
	entity = first_entity(payload)
	owners = entity.get("OwnersKycData") if isinstance(entity.get("OwnersKycData"), dict) else {}
	people = owners.get("PeopleOwnersKycData") if isinstance(owners.get("PeopleOwnersKycData"), dict) else {}

	socios: list[dict[str, Any]] = []
	for cpf, block in people.items():
		if not isinstance(block, dict):
			continue
		parsed = _parse_kyc_block(block, min_match)
		socios.append(
			{
				"cpf": only_digits(str(cpf)),
				"is_pep": parsed["is_pep"],
				"sanctioned": parsed["sanctioned"],
				"sanctions": parsed["sanctions"],
			}
		)

	return {
		"consultado": bool(owners),
		"total_socios": len(socios),
		"socios_pep": sum(1 for s in socios if s["is_pep"]),
		"socios_sancionados": sum(1 for s in socios if s["sanctioned"]),
		"socios": socios,
	}


def parse_global_adverse_exposure(payload: dict[str, Any]) -> dict[str, Any]:
	"""Extrai `global_adverse_exposure` (chave `PublicDisclosedDocumentsExposure`): vazamentos e documentos públicos adversos."""
	entity = first_entity(payload)
	data = (
		entity.get("PublicDisclosedDocumentsExposure")
		if isinstance(entity.get("PublicDisclosedDocumentsExposure"), dict)
		else {}
	)
	return {
		"presente": bool(data.get("IsPresentInPublicDisclosedDocuments")),
		"ocorrencias": int(data.get("TotalOccurrences") or 0),
		"fontes": dict(data.get("OccurrencesBySource") or {}),
		"ultima_ocorrencia": str(data.get("LastOccurrenceDate") or ""),
	}


def parse_sintegra(payload: dict[str, Any]) -> dict[str, Any]:
	"""Extrai `ondemand_sintegra_company` (chave `SintegraData`): inscrições estaduais e habilitação na SEFAZ."""
	entity = first_entity(payload)
	data = entity.get("SintegraData") if isinstance(entity.get("SintegraData"), dict) else {}
	inscricoes = [
		{
			"inscricao_estadual": str(e.get("StateRegistration") or ""),
			"situacao": str(e.get("Status") or ""),
			"situacao_documento": str(e.get("DocNumberStatus") or ""),
			"tipo": str(e.get("StateRegistrationType") or ""),
			"regime": str(e.get("TributationRegime") or ""),
		}
		for e in data.get("Entries") or []
		if isinstance(e, dict)
	]
	return {
		"uf": str(data.get("State") or ""),
		"inscricoes": inscricoes,
		"habilitada": any("HABILITAD" in i["situacao"].upper() and "NAO" not in i["situacao"].upper() for i in inscricoes),
	}


def _company_uf(opencnpj_data: dict[str, Any] | None) -> str:
	if not isinstance(opencnpj_data, dict):
		return ""
	uf = str(opencnpj_data.get("uf") or opencnpj_data.get("estado") or "").strip().upper()
	return uf if len(uf) == 2 and uf.isalpha() else ""


def fetch_sintegra_bdc(
	cnpj: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	uf: str = "",
) -> dict[str, Any]:
	"""Consulta o Sintegra pela API On-demand da BigDataCorp (`ondemand_sintegra_company`, R$ 0,10).

	A UF é recomendada pela documentação; sem ela a BigDataCorp tenta descobrir em bases internas.
	"""
	digits = only_digits(cnpj)
	q = f"doc{{{digits}}},uf{{{uf}}}" if uf else f"doc{{{digits}}}"
	payload = _post_bdc("ondemand", "ondemand_sintegra_company", q, token, base_url, timeout)
	return parse_sintegra(payload)


def _maybe_fetch_sintegra(
	digits: str,
	token: str,
	base_url: str,
	timeout: int,
	opencnpj_data: dict[str, Any] | None,
	should_query: bool,
) -> tuple[bool, dict[str, Any]]:
	"""Dispara o Sintegra só quando a regra de CNAE pede. Se a consulta falhar, não conta o custo e registra o erro."""
	if not should_query:
		return False, {}
	try:
		return True, fetch_sintegra_bdc(digits, token, base_url, timeout, uf=_company_uf(opencnpj_data))
	except BigDataCorpCreditAuthError:
		raise
	except BigDataCorpCreditError as e:
		return False, {"erro": str(e)}


def parse_lawsuits_distribution(payload: dict[str, Any]) -> dict[str, Any]:
	"""Converte `LawsuitsDistributionData` (R$ 0,04) ou o detalhe do dataset `processes` (R$ 0,08) para o contrato de `enrich.py`."""
	entity = first_entity(payload)
	dist = entity.get("LawsuitsDistributionData") if isinstance(entity.get("LawsuitsDistributionData"), dict) else {}
	# Dataset `processes`: em /empresas a chave da resposta é `Lawsuits`; em /pessoas é `Processes`.
	lawsuits_full = next(
		(entity[k] for k in ("Lawsuits", "Processes") if isinstance(entity.get(k), dict)),
		{},
	)

	total_count = int(dist.get("TotalLawsuits") or lawsuits_full.get("TotalLawsuits") or 0)

	party_dist = dist.get("PartyTypeDistribution") if isinstance(dist.get("PartyTypeDistribution"), dict) else {}
	defendant_count = sum(
		int(v or 0)
		for k, v in party_dist.items()
		if str(k).upper() in ("CLAIMED", "DEFENDANT", "PASSIVE", "REU", "EXECUTADO", "RECLAMADO")
	)
	plaintiff_count = sum(
		int(v or 0)
		for k, v in party_dist.items()
		if str(k).upper() in ("CLAIMANT", "AUTHOR", "ACTIVE", "AUTOR", "EXEQUENTE", "RECLAMANTE")
	)
	if not party_dist and lawsuits_full:
		defendant_count = int(lawsuits_full.get("TotalLawsuitsAsDefendant") or 0)
		plaintiff_count = int(lawsuits_full.get("TotalLawsuitsAsAuthor") or 0)

	type_dist = dist.get("TypeDistribution") if isinstance(dist.get("TypeDistribution"), dict) else {}
	cnj_proc_dist = dist.get("CnjProcedureTypeDistribution") if isinstance(dist.get("CnjProcedureTypeDistribution"), dict) else {}
	cnj_subj_dist = dist.get("CnjSubjectDistribution") if isinstance(dist.get("CnjSubjectDistribution"), dict) else {}

	all_labels = " ".join([*type_dist.keys(), *cnj_proc_dist.keys(), *cnj_subj_dist.keys()])
	has_bankruptcy = bool(RE_BANKRUPTCY.search(all_labels))

	critical_lawsuits: list[dict[str, Any]] = []
	highest_value = _parse_float_br(dist.get("HighestLawsuitValue") or 0.0)
	for lw in lawsuits_full.get("Lawsuits") or []:
		if not isinstance(lw, dict):
			continue
		val = _parse_float_br(lw.get("Value"))
		if val > highest_value:
			highest_value = val
		subj = str(lw.get("MainSubject") or lw.get("Type") or "")
		if RE_BANKRUPTCY.search(subj):
			has_bankruptcy = True
		critical_lawsuits.append(
			{
				"numero_cnj": str(lw.get("Number") or ""),
				"tribunal": str(lw.get("CourtName") or ""),
				"assunto": subj,
				"valor": val,
				"status": str(lw.get("Status") or ""),
			}
		)

	return {
		"total_count": total_count,
		"defendant_count": defendant_count,
		"plaintiff_count": plaintiff_count,
		"highest_value": highest_value,
		"has_bankruptcy_recovery": has_bankruptcy,
		"type_distribution": type_dist,
		"court_type_distribution": dist.get("CourtTypeDistribution") or {},
		"critical_lawsuits": critical_lawsuits[:20],
	}


def parse_government_debtors(payload: dict[str, Any]) -> dict[str, Any]:
	"""Converte `GovernmentDebtors` (R$ 0,06 — PGFN, FGTS, INSS, SIDA) para o formato fiscal de `enrich.py`."""
	entity = first_entity(payload)
	gov = entity.get("GovernmentDebtors") if isinstance(entity.get("GovernmentDebtors"), dict) else {}
	total_debt = _parse_float_br(gov.get("TotalDebtValue"))
	total_count = int(gov.get("TotalDebts") or 0)
	debts = gov.get("Debts") if isinstance(gov.get("Debts"), list) else []

	fgts_debt = 0.0
	for item in debts:
		if not isinstance(item, dict):
			continue
		origin = str(item.get("DebtOrigin") or item.get("Source") or "").upper()
		if "FGTS" in origin:
			fgts_debt += _parse_float_br(item.get("ConsolidatedValue"))

	is_pgfn_regular = total_debt <= 0.0 and total_count == 0
	is_fgts_regular = fgts_debt <= 0.0

	return {
		"pgfn": {
			"total_debt": total_debt,
			"status": "Regular" if is_pgfn_regular else "Inscrito na Dívida Ativa",
			"inscriptions_count": total_count,
			"inscriptions": debts,
		},
		"cnd_federal": {
			"is_regular": is_pgfn_regular,
			"status": "Regular" if is_pgfn_regular else "Com Pendências PGFN",
			"pdf_url": "",
		},
		"cndt": {
			"is_regular": True,
			"status": "Regular",
			"pdf_url": "",
		},
		"cenprot": {
			"has_protests": False,
			"protests_count": 0,
		},
		"fgts": {
			"is_regular": is_fgts_regular,
			"status": "Regular" if is_fgts_regular else "Irregular (Débito FGTS)",
			"total_debt": fgts_debt,
		},
	}


def parse_collections_and_activity(payload: dict[str, Any]) -> dict[str, Any]:
	"""Extrai presença em cobrança (`Collections`) e faturamento presumido (`ActivityIndicators`)."""
	entity = first_entity(payload)
	coll = entity.get("Collections") if isinstance(entity.get("Collections"), dict) else {}
	act = entity.get("ActivityIndicators") if isinstance(entity.get("ActivityIndicators"), dict) else {}
	hist = entity.get("HistoryBasicData") if isinstance(entity.get("HistoryBasicData"), dict) else {}

	return {
		"collections": {
			"is_currently_on_collection": bool(coll.get("IsCurrentlyOnCollection")),
			"last_30_days": int(coll.get("Last30DaysCollectionOccurrences") or 0),
			"last_90_days": int(coll.get("Last90DaysCollectionOccurrences") or 0),
			"last_180_days": int(coll.get("Last180DaysCollectionOccurrences") or 0),
			"last_365_days": int(coll.get("Last365DaysCollectionOccurrences") or 0),
			"consecutive_months": int(coll.get("ConsecutiveCollectionMonths") or 0),
		},
		"activity_indicators": {
			"estimated_revenue_range": str(act.get("IncomeRange") or act.get("EstimatedRevenueRange") or ""),
			"employees_range": str(act.get("EmployeesRange") or ""),
			"activity_level": str(act.get("ActivityLevel") or ""),
		},
		"history": hist,
	}


def parse_advanced_core_pj(payload: dict[str, Any]) -> dict[str, Any]:
	"""Extrai os datasets avançados do PJ Completo (100% Core BDC: Evolução, Comportamento Financeiro Digital,
	Processos dos Sócios, KYC dos Sócios, Grupo Econômico e Mídia Negativa) e constrói os 7 Indicadores Comportamentais (0–100)."""
	entity = first_entity(payload)
	evo = entity.get("CompanyEvolutionData") if isinstance(entity.get("CompanyEvolutionData"), dict) else {}
	dfb = entity.get("DigitalFinanceBehaviors") if isinstance(entity.get("DigitalFinanceBehaviors"), dict) else {}
	partner_lw = (
		entity.get("OwnersLawsuitsDistributionData")
		if isinstance(entity.get("OwnersLawsuitsDistributionData"), dict)
		else {}
	)
	econ_group = (
		entity.get("EconomicGroupRelationship")
		if isinstance(entity.get("EconomicGroupRelationship"), dict)
		else {}
	)
	adverse = parse_global_adverse_exposure(payload)
	owners_kyc = parse_owners_kyc(payload)
	group_companies = {
		str(rel.get("RelatedEntityTaxIdNumber") or "")
		for rel in econ_group.get("Relationships") or []
		if isinstance(rel, dict) and str(rel.get("RelatedEntityTaxIdType") or "").upper() == "CNPJ"
	} - {""}
	coll = entity.get("Collections") if isinstance(entity.get("Collections"), dict) else {}

	# 1. Pontualidade / Ausência de Cobrança (0–100)
	if coll.get("IsCurrentlyOnCollection"):
		punctuality_score = 25
	elif int(coll.get("Last90DaysCollectionOccurrences") or 0) > 0:
		punctuality_score = 55
	elif int(coll.get("Last365DaysCollectionOccurrences") or 0) > 0:
		punctuality_score = 75
	else:
		punctuality_score = 92

	# 2. Pressão de Acionamento / Atraso Recente (0–100)
	delay_pressure = min(
		int(coll.get("Last30DaysCollectionOccurrences") or 0) * 35
		+ int(coll.get("Last90DaysCollectionOccurrences") or 0) * 20
		+ (40 if coll.get("IsCurrentlyOnCollection") else 0),
		100,
	)

	# 3. Busca Ativa por Crédito Digital (CreditSeeker A-H -> 0-100)
	credit_seeker_score = _grade_to_pct(dfb.get("CreditSeeker"), default=30)

	# 4. Comportamento e Uso de Cartão de Crédito (CreditCardScore A-H -> 0-100)
	credit_card_score = _grade_to_pct(dfb.get("CreditCardScore") or dfb.get("CreditCardScore_v2"), default=60)

	# 5. Digitalização Bancária & Meios de Pagamento (OnlineBankingUser + PaymentServicesUser -> 0-100)
	banking_score = int(
		(
			_grade_to_pct(dfb.get("OnlineBankingUser"), default=70)
			+ _grade_to_pct(dfb.get("PaymentServicesUser"), default=70)
		)
		/ 2
	)

	# 6. Trajetória de Crescimento Empresarial (CompanyEvolution YoY -> 0-100)
	growth_1y = str(evo.get("YearOverYearGrowthRateStatus1YearAgo") or "").upper()
	growth_3y = str(evo.get("YearOverYearGrowthRateStatus3YearsAgo") or "").upper()
	if "GROW" in growth_1y and "GROW" in growth_3y:
		growth_score = 90
	elif "GROW" in growth_1y:
		growth_score = 78
	elif "SHRINK" in growth_1y:
		growth_score = 35
	else:
		growth_score = 68

	# 7. Estabilidade Societária & Solidez do Grupo (0–100)
	qsa_changed = bool(evo.get("HasQSAChangedAnytime"))
	partner_lawsuits_total = int(partner_lw.get("TotalLawsuits") or 0)
	stability_score = max(min(90 - (15 if qsa_changed else 0) - min(partner_lawsuits_total * 5, 35), 100), 20)

	indicadores_comportamentais = [
		{"name": "1. Regularidade & Ausência de Cobrança (12m)", "value": float(punctuality_score)},
		{"name": "2. Pressão de Acionamento / Cobrança Recente", "value": float(delay_pressure)},
		{"name": "3. Busca Ativa por Crédito (Rastro Digital)", "value": float(credit_seeker_score)},
		{"name": "4. Saúde e Propensão em Cartão Corporativo", "value": float(credit_card_score)},
		{"name": "5. Maturidade em Internet Banking & Pagamentos", "value": float(banking_score)},
		{"name": "6. Trajetória de Crescimento (1 a 3 anos)", "value": float(growth_score)},
		{"name": "7. Estabilidade Societária & Jurídica dos Sócios", "value": float(stability_score)},
	]

	return {
		"evolucao_empresa": {
			"crescimento_1_ano": growth_1y or "ESTÁVEL",
			"crescimento_3_anos": growth_3y or "ESTÁVEL",
			"capital_atual_max": _parse_float_br(evo.get("MaxCapital")),
			"capital_medio_1_ano": _parse_float_br(evo.get("AverageCapital1YearAgo")),
			"funcionarios_medio_1_ano": int(evo.get("AverageQtyEmployees1YearAgo") or 0),
			"funcionarios_medio_3_anos": int(evo.get("AverageQtyEmployees3YearsAgo") or 0),
			"houve_alteracao_qsa": qsa_changed,
			"historico_mensal": list(evo.get("DataHistoryOverTime") or [])[:12],
		},
		"comportamento_financeiro_digital": {
			"busca_por_credito_nota": str(dfb.get("CreditSeeker") or "N/A"),
			"uso_meios_pagamento_nota": str(dfb.get("PaymentServicesUser") or "N/A"),
			"internet_banking_nota": str(dfb.get("OnlineBankingUser") or "N/A"),
			"cartao_credito_nota": str(dfb.get("CreditCardScore") or dfb.get("CreditCardScore_v2") or "N/A"),
			"investidor_online_nota": str(dfb.get("OnlineInvestor") or dfb.get("OnlineInvestor_v2") or "N/A"),
		},
		"risco_socios_e_grupo": {
			"total_processos_dos_socios": partner_lawsuits_total,
			"total_empresas_grupo_economico": len(group_companies),
			"exposicao_negativa_global": adverse,
			"kyc_socios": owners_kyc,
		},
		"indicadores_positivos": indicadores_comportamentais,
	}


def _company_age_days(opencnpj_data: dict[str, Any] | None) -> int | None:
	if not isinstance(opencnpj_data, dict):
		return None
	raw_date = str(
		opencnpj_data.get("data_inicio_atividade")
		or opencnpj_data.get("founded_date")
		or opencnpj_data.get("abertura")
		or ""
	).strip()
	if not raw_date:
		return None
	for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
		try:
			dt = datetime.strptime(raw_date[:10], fmt).date()
			return max(0, (date.today() - dt).days)
		except ValueError:
			continue
	return None


def _is_medium_or_large_company(
	opencnpj_data: dict[str, Any] | None,
	activity: dict[str, Any] | None,
	advanced: dict[str, Any] | None,
) -> bool:
	if isinstance(opencnpj_data, dict):
		cap = _parse_float_br(opencnpj_data.get("capital_social") or opencnpj_data.get("share_capital"))
		if cap >= 500_000:
			return True
		porte = str(opencnpj_data.get("porte") or "").upper()
		if any(k in porte for k in ("DEMAIS", "MEDIO", "MÉDIO", "GRANDE")):
			return True
	if isinstance(activity, dict):
		rev = str(activity.get("estimated_revenue_range") or "").upper()
		if any(k in rev for k in ("M", "MILH", "BILH")):
			return True
		emp = str(activity.get("employees_range") or "")
		nums = [int(n) for n in re.findall(r"\d+", emp)]
		if any(n >= 50 for n in nums):
			return True
	if isinstance(advanced, dict):
		evo = advanced.get("evolucao_empresa") or {}
		if float(evo.get("capital_atual_max") or 0) >= 500_000 or int(evo.get("funcionarios_medio_1_ano") or 0) >= 50:
			return True
	return False


def calculate_proprietary_score_pj(
	lawsuits: dict[str, Any],
	fiscal: dict[str, Any],
	kyc: dict[str, Any],
	collections: dict[str, Any],
	advanced: dict[str, Any] | None = None,
	activity: dict[str, Any] | None = None,
	sintegra: dict[str, Any] | None = None,
	opencnpj_data: dict[str, Any] | None = None,
	protests: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Calcula o Score Proprietário PJ (0 a 1000) sobre os dados Core da BigDataCorp + OpenCNPJ (+ Protestos se consultado)."""
	score = 750
	hard_cap = 1000
	reasons: list[str] = []

	# 1. Maturidade Societária & Porte (OpenCNPJ + ActivityIndicators)
	age_days = _company_age_days(opencnpj_data)
	if age_days is not None:
		if age_days < 180:
			score -= 80
			reasons.append("Empresa recém-constituída (< 6 meses de atividade)")
		elif age_days < 365:
			score -= 40
			reasons.append("Empresa com menos de 1 ano de atividade")
		elif age_days >= 1825:
			score += 60
			reasons.append("Maturidade societária consolidada (5+ anos de atividade)")
		elif age_days >= 1095:
			score += 35
			reasons.append("Maturidade societária (3+ anos de atividade)")

	cap_social = _parse_float_br((opencnpj_data or {}).get("capital_social") or (opencnpj_data or {}).get("share_capital"))
	if cap_social <= 0 and advanced:
		cap_social = float((advanced.get("evolucao_empresa") or {}).get("capital_atual_max") or 0.0)
	if cap_social >= 500_000:
		score += 25
		reasons.append("Capital social sólido (≥ R$ 500 mil)")
	elif cap_social >= 100_000:
		score += 15
		reasons.append("Capital social integralizado (≥ R$ 100 mil)")

	if isinstance(activity, dict) and activity:
		act_level = str(activity.get("activity_level") or "").upper()
		rev_range = str(activity.get("estimated_revenue_range") or "").upper()
		if any(k in act_level for k in ("INACTIVE", "INATIV", "PARALISAD", "VERY LOW", "MUITO BAIX")):
			score -= 80
			reasons.append("Nível de atividade operacional muito baixo ou inativo")
		elif any(k in act_level for k in ("LOW", "BAIX")):
			score -= 35
			reasons.append("Nível de atividade operacional baixo")
		elif any(k in act_level for k in ("HIGH", "ALTO")):
			score += 30
			reasons.append("Nível de atividade operacional elevado")
		if any(k in rev_range for k in ("M", "MILH", "BILH")):
			score += 25

	# 2. Travas Críticas (com Hard Cap na Faixa E)
	if lawsuits.get("has_bankruptcy_recovery"):
		score -= 500
		hard_cap = min(hard_cap, 250)
		reasons.append("Registro de falência ou recuperação judicial identificado")

	if kyc.get("sanctioned"):
		score -= 400
		hard_cap = min(hard_cap, 350)
		reasons.append("Presença ativa em listas restritivas / sanções")

	# 3. Regularidade Fiscal (PGFN / FGTS e Sintegra/SEFAZ)
	pgfn_debt = float((fiscal.get("pgfn") or {}).get("total_debt") or 0.0)
	if pgfn_debt > 0:
		penalty = min(int(150 + (pgfn_debt / 5000.0) * 10), 350)
		score -= penalty
		reasons.append(f"Inscrição ativa na Dívida da União / PGFN (R$ {pgfn_debt:,.2f})")

	if isinstance(sintegra, dict) and sintegra.get("inscricoes") and sintegra.get("habilitada") is False:
		score -= 200
		reasons.append("Inscrição Estadual (Sintegra/SEFAZ) inapta, suspensa ou não habilitada")

	# 4. Protestos em Cartório (quando consultado)
	protest_data = protests if isinstance(protests, dict) else (fiscal.get("cenprot") if isinstance(fiscal.get("cenprot"), dict) else {})
	if protest_data and (protest_data.get("has_protests") or int(protest_data.get("protests_count") or 0) > 0):
		p_count = int(protest_data.get("protests_count") or 1)
		p_val = float(protest_data.get("total_value") or 0.0)
		if p_val > 50_000 or p_count >= 10:
			p_pen = 180
		elif p_val > 5_000 or p_count >= 3:
			p_pen = 110
		else:
			p_pen = 60
		score -= p_pen
		reasons.append(f"{p_count} protesto(s) registrado(s) em cartório")

	# 5. Cobrança (Collections BDC)
	if collections.get("is_currently_on_collection"):
		score -= 150
		reasons.append("Empresa em cobrança ativa no momento")
		if int(collections.get("consecutive_months") or 0) >= 3:
			score -= 40
			reasons.append("Acionamento recorrente em cobrança por 3+ meses consecutivos")
	elif int(collections.get("last_90_days") or 0) > 0:
		score -= 75
		reasons.append("Passagem em cobrança nos últimos 90 dias")
	elif int(collections.get("last_365_days") or 0) > 0:
		score -= 35
		reasons.append("Histórico de passagem em cobrança nos últimos 12 meses")
	elif collections and "is_currently_on_collection" in collections:
		score += 30

	# 6. Processos Judiciais no Polo Passivo (ponderado pelo porte)
	def_count = int(lawsuits.get("defendant_count") or 0)
	if def_count > 0:
		is_large = _is_medium_or_large_company(opencnpj_data, activity, advanced)
		per_lawsuit = 10 if is_large else 15
		max_lawsuit_pen = 120 if is_large else 140
		score -= min(def_count * per_lawsuit, max_lawsuit_pen)
		reasons.append(f"{def_count} processo(s) judicial(is) no polo passivo")

	# 7. Indicadores Avançados (PJ Completo)
	if advanced:
		evo = advanced.get("evolucao_empresa") or {}
		if "GROW" in str(evo.get("crescimento_1_ano") or ""):
			score += 30
			reasons.append("Trajetória de crescimento operacional nos últimos 12 meses")
		elif "SHRINK" in str(evo.get("crescimento_1_ano") or ""):
			score -= 40
			reasons.append("Retração de porte/quadro operacional nos últimos 12 meses")

		if evo.get("houve_alteracao_qsa"):
			score -= 30
			reasons.append("Alteração recente no quadro societário (QSA)")

		dfb = advanced.get("comportamento_financeiro_digital") or {}
		if str(dfb.get("busca_por_credito_nota") or "").upper() in ("G", "H"):
			score -= 40
			reasons.append("Busca intensa por crédito digital no mercado")
		if str(dfb.get("cartao_credito_nota") or "").upper() in ("A", "B"):
			score += 20

		grupo = advanced.get("risco_socios_e_grupo") or {}
		socios_sancionados = int((grupo.get("kyc_socios") or {}).get("socios_sancionados") or 0)
		if socios_sancionados > 0:
			score -= 150
			reasons.append(f"{socios_sancionados} sócio(s) com sanção ativa em listas restritivas")

		socio_lw = int(grupo.get("total_processos_dos_socios") or 0)
		if socio_lw > 5:
			score -= min((socio_lw - 5) * 8, 60)
			reasons.append(f"Sócios acumulam {socio_lw} processos judiciais em seus CPFs")

		if (grupo.get("exposicao_negativa_global") or {}).get("presente"):
			score -= 80
			reasons.append("Presença em documentos públicos adversos / exposição negativa global")

	score = max(min(score, hard_cap), 100)
	if score >= 800:
		band = "BAIXO (A)"
	elif score >= 700:
		band = "MODERADO-BAIXO (B)"
	elif score >= 550:
		band = "MODERADO (C)"
	elif score >= 400:
		band = "ALTO (D)"
	else:
		band = "MUITO ALTO (E)"

	if not reasons:
		reasons.append("Regularidade fiscal, societária e ausência de apontamentos críticos")

	return {"score": score, "faixa_score": band, "motivos": reasons, "modelo": "PROPRIETARIO_CORE_BDC"}


# Funções de Alto Nível dos 4 Produtos do Bureau (100% Core BDC, Sem Quod/Quantum) ---------------------------


def _build_inactive_short_circuit(cnpj: str, plano: str, opencnpj_data: dict[str, Any]) -> dict[str, Any]:
	"""Retorno imediato a custo R$ 0,00 quando o OpenCNPJ já aponta empresa Baixada/Inapta/Suspensa/Nula."""
	status = str(
		opencnpj_data.get("situacao_cadastral") or opencnpj_data.get("registration_status") or "INAPTA"
	).upper()
	return {
		"cnpj": cnpj,
		"plano": plano,
		"cogs_estimado": 0.0,
		"short_circuited": True,
		"short_circuit_reason": f"Empresa com situação cadastral {status} na Receita Federal (OpenCNPJ gratuito)",
		"sintegra_consultado": False,
		"query_id": "",
		"score": {
			"score": 100,
			"faixa_score": "MUITO ALTO (E)",
			"motivos": [f"Situação cadastral {status} na Receita Federal — reprovação imediata sem custo de bureau"],
			"modelo": "PROPRIETARIO_CORE_BDC",
		},
		"capacidade_economica": {},
		"cobrancas": {},
		"fiscal": {},
		"processos": {"total_count": 0, "defendant_count": 0},
		"compliance": {"sanctioned": False, "is_pep": False},
		"quadro_societario": [],
		"historico_cadastral": {},
		"raw": {},
	}


def fetch_pj_basico(
	cnpj: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	opencnpj_data: dict[str, Any] | None = None,
	include_sintegra: bool | None = None,
	force_if_inactive: bool = False,
) -> dict[str, Any]:
	"""Produto 1: PJ Básico com Regras Inteligentes de Economia (Smart Gating):
	- Sem `basic_data` redundante (economiza R$ 0,02 em 100% das consultas; vem do OpenCNPJ gratuito).
	- Curto-circuito se empresa Baixada/Inapta no OpenCNPJ (custo R$ 0,00).
	- Sintegra ligado por padrão para Comércio/Indústria/Agro/Transporte (R$ 0,40 total) e pulado
	  automaticamente para CNAEs 100% Serviços (caindo para R$ 0,30 total).
	"""
	digits = only_digits(cnpj)
	if len(digits) != 14:
		raise ValueError(f"CNPJ inválido: {cnpj}")

	if not force_if_inactive and is_inactive_on_receita(opencnpj_data):
		return _build_inactive_short_circuit(digits, "PJ_BASICO", opencnpj_data or {})

	should_query_sintegra = (
		requires_sintegra_by_cnae(opencnpj_data) if include_sintegra is None else bool(include_sintegra)
	)

	payload = _post_bdc("empresas", PJ_BASICO_DATASETS, f"doc{{{digits}}}", token, base_url, timeout)
	sintegra_ok, sintegra = _maybe_fetch_sintegra(digits, token, base_url, timeout, opencnpj_data, should_query_sintegra)
	kyc = parse_kyc_compliance(payload)
	lawsuits = parse_lawsuits_distribution(payload)
	fiscal = parse_government_debtors(payload)
	extra = parse_collections_and_activity(payload)
	qsa = extract_partners(payload)
	prop_score = calculate_proprietary_score_pj(
		lawsuits,
		fiscal,
		kyc,
		extra["collections"],
		activity=extra["activity_indicators"],
		sintegra=sintegra if sintegra_ok else None,
		opencnpj_data=opencnpj_data,
	)
	cogs = estimate_datasets_cost(PJ_BASICO_DATASETS, include_sintegra=sintegra_ok)

	return {
		"cnpj": digits,
		"plano": "PJ_BASICO",
		"cogs_estimado": cogs,
		"short_circuited": False,
		"sintegra_consultado": sintegra_ok,
		"sintegra": sintegra,
		"query_id": payload.get("QueryId") or "",
		"score": prop_score,
		"capacidade_economica": extra["activity_indicators"],
		"cobrancas": extra["collections"],
		"fiscal": fiscal,
		"processos": lawsuits,
		"compliance": kyc,
		"quadro_societario": qsa,
		"historico_cadastral": extra["history"],
		"raw": payload,
	}


def _maybe_fetch_protests_with_smart_gating(
	digits: str,
	directd_token: str,
	should_query: bool,
	lawsuits: dict[str, Any],
	kyc: dict[str, Any],
	advanced: dict[str, Any],
	timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, dict[str, Any]]:
	"""Consulta Protestos Nacional IEPTB Básica (DirectD, R$ 2,95) no PJ Avançado com Smart Gating:
	- Pula automaticamente e economiza R$ 2,95 se a empresa já reprovou na etapa BigDataCorp
	  (Falência/RJ, Sanção ativa da empresa ou Sócio com sanção ativa).
	- Se a chamada falhar, não contabiliza o custo e registra o erro.
	"""
	empty = {
		"has_protests": False,
		"protests_count": 0,
		"total_value": 0.0,
		"cartorios_distintos": 0,
		"cidades_distintas": 0,
	}
	if not should_query or not (directd_token or "").strip():
		return False, empty

	socios_sancionados = int(((advanced.get("risco_socios_e_grupo") or {}).get("kyc_socios") or {}).get("socios_sancionados") or 0)
	if lawsuits.get("has_bankruptcy_recovery") or kyc.get("sanctioned") or socios_sancionados > 0:
		return False, {**empty, "skipped_by_smart_gating": True}

	try:
		res = fetch_protests_nacional(digits, directd_token.strip(), timeout=timeout)
		return True, res
	except DirectDError as e:
		return False, {**empty, "erro": str(e)}


def fetch_pj_completo(
	cnpj: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	opencnpj_data: dict[str, Any] | None = None,
	include_sintegra: bool | None = None,
	include_detailed_lawsuits: bool = False,
	force_if_inactive: bool = False,
	two_stage_knockout: bool = False,
	directd_token: str = "",
	include_protests: bool | None = None,
) -> dict[str, Any]:
	"""Produto 2: PJ Completo (Avançado — Core BigDataCorp + Protestos IEPTB Básica c/ Smart Gating, ZERO Quod/Quantum):
	- Sem `basic_data` redundante (-R$ 0,02).
	- Curto-circuito se Baixada/Inapta no OpenCNPJ (R$ 0,00).
	- Sintegra ligado por padrão para Comércio/Indústria/Transporte e pulado para Serviços (-R$ 0,10).
	- Pula `company_evolution` (-R$ 0,06) se a empresa tiver menos de 1 ano de abertura.
	- Pula `economic_group_relationships` (-R$ 0,06) se a empresa for MEI.
	- Pula `owners_lawsuits_distribution_data` e `owners_kyc` (-R$ 0,20) se o quadro societário tiver apenas sócios PJ (Holding).
	- Protestos Nacional IEPTB Básica (`R$ 2,95` via DirectD): incluído no PJ Avançado com Smart Gating — só é disparado
	  se a empresa não tiver reprovado antes no OpenCNPJ/BigDataCorp (Falência/RJ, Sanção da empresa ou Sócio sancionado).
	- Modo Knock-Out em 2 estágios opcional (`two_stage_knockout=True`): consulta primeiro os 3 datasets eliminatórios
	  (`government_debtors,lawsuits_distribution_data,collections` = R$ 0,16) e interrompe caso haja Falência/RJ.
	- Só dispara `processes` detalhado (R$ 0,08) se `defendant_count > 0` ou `include_detailed_lawsuits=True`.
	"""
	digits = only_digits(cnpj)
	if len(digits) != 14:
		raise ValueError(f"CNPJ inválido: {cnpj}")

	if not force_if_inactive and is_inactive_on_receita(opencnpj_data):
		return _build_inactive_short_circuit(digits, "PJ_COMPLETO", opencnpj_data or {})

	if two_stage_knockout:
		ko_datasets = "government_debtors,lawsuits_distribution_data,collections"
		ko_payload = _post_bdc("empresas", ko_datasets, f"doc{{{digits}}}", token, base_url, timeout)
		ko_lawsuits = parse_lawsuits_distribution(ko_payload)
		ko_fiscal = parse_government_debtors(ko_payload)
		ko_extra = parse_collections_and_activity(ko_payload)
		if ko_lawsuits.get("has_bankruptcy_recovery"):
			ko_score = calculate_proprietary_score_pj(ko_lawsuits, ko_fiscal, {"sanctioned": False}, ko_extra["collections"])
			return {
				"cnpj": digits,
				"plano": "PJ_COMPLETO",
				"cogs_estimado": estimate_datasets_cost(ko_datasets, include_sintegra=False),
				"short_circuited": True,
				"short_circuit_reason": "Knock-Out no Estágio 1: Falência ou Recuperação Judicial identificada",
				"sintegra_consultado": False,
				"protestos_consultado": False,
				"datasets_consultados": ko_datasets.split(","),
				"query_id": ko_payload.get("QueryId") or "",
				"score": ko_score,
				"capacidade_economica": ko_extra["activity_indicators"],
				"cobrancas": ko_extra["collections"],
				"fiscal": ko_fiscal,
				"processos": ko_lawsuits,
				"compliance": {"sanctioned": False, "is_pep": False},
				"quadro_societario": [],
				"historico_cadastral": {},
				"evolucao_empresa": {},
				"comportamento_financeiro_digital": {},
				"risco_socios_e_grupo": {},
				"indicadores_positivos": [],
				"raw": ko_payload,
			}

	should_query_sintegra = (
		requires_sintegra_by_cnae(opencnpj_data) if include_sintegra is None else bool(include_sintegra)
	)
	should_query_protests = bool(directd_token and directd_token.strip()) if include_protests is None else bool(include_protests)

	extra_list = [
		"digital_finance_behaviors",
		"global_adverse_exposure",
	]
	if _company_has_pf_partners(opencnpj_data):
		extra_list.extend(["owners_lawsuits_distribution_data", "owners_kyc"])
	if _company_has_at_least_one_year(opencnpj_data):
		extra_list.insert(0, "company_evolution")
	if not _company_is_mei(opencnpj_data):
		extra_list.append("economic_group_relationships")
	if include_detailed_lawsuits:
		extra_list.append("processes")

	datasets_csv = f"{PJ_BASICO_DATASETS},{','.join(extra_list)}"
	payload = _post_bdc("empresas", datasets_csv, f"doc{{{digits}}}", token, base_url, timeout)
	sintegra_ok, sintegra = _maybe_fetch_sintegra(digits, token, base_url, timeout, opencnpj_data, should_query_sintegra)
	kyc = parse_kyc_compliance(payload)
	lawsuits = parse_lawsuits_distribution(payload)
	fiscal = parse_government_debtors(payload)
	extra = parse_collections_and_activity(payload)
	qsa = extract_partners(payload)
	advanced = parse_advanced_core_pj(payload)

	protests_ok, protests = _maybe_fetch_protests_with_smart_gating(
		digits,
		directd_token,
		should_query_protests,
		lawsuits,
		kyc,
		advanced,
		timeout=timeout,
	)
	if protests_ok:
		fiscal["cenprot"] = protests

	prop_score = calculate_proprietary_score_pj(
		lawsuits,
		fiscal,
		kyc,
		extra["collections"],
		advanced=advanced,
		activity=extra["activity_indicators"],
		sintegra=sintegra if sintegra_ok else None,
		opencnpj_data=opencnpj_data,
		protests=protests if protests_ok else None,
	)
	cogs = round(
		estimate_datasets_cost(datasets_csv, include_sintegra=sintegra_ok)
		+ (DIRECTD_PROTESTS_BASICA_COST if protests_ok else 0.0),
		2,
	)

	return {
		"cnpj": digits,
		"plano": "PJ_COMPLETO",
		"cogs_estimado": cogs,
		"short_circuited": False,
		"sintegra_consultado": sintegra_ok,
		"sintegra": sintegra,
		"protestos_consultado": protests_ok,
		"protestos": protests,
		"datasets_consultados": datasets_csv.split(","),
		"query_id": payload.get("QueryId") or "",
		"score": prop_score,
		"capacidade_economica": extra["activity_indicators"],
		"cobrancas": extra["collections"],
		"fiscal": fiscal,
		"processos": lawsuits,
		"compliance": kyc,
		"quadro_societario": qsa,
		"historico_cadastral": extra["history"],
		"evolucao_empresa": advanced["evolucao_empresa"],
		"comportamento_financeiro_digital": advanced["comportamento_financeiro_digital"],
		"risco_socios_e_grupo": advanced["risco_socios_e_grupo"],
		"indicadores_positivos": advanced["indicadores_positivos"],
		"raw": payload,
	}


def fetch_pf_basico(
	cpf: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
	"""Produto 3: PF Básico — COGS R$ 0,48."""
	digits = only_digits(cpf)
	if len(digits) != 11:
		raise ValueError(f"CPF inválido: {cpf}")

	payload = _post_bdc("pessoas", PF_BASICO_DATASETS, f"doc{{{digits}}}", token, base_url, timeout)
	entity = first_entity(payload)
	fin_risk = entity.get("FinancialRisk") if isinstance(entity.get("FinancialRisk"), dict) else {}
	indebt = entity.get("IndebtednessQuestion") if isinstance(entity.get("IndebtednessQuestion"), dict) else {}

	return {
		"cpf": digits,
		"plano": "PF_BASICO",
		"cogs_estimado": estimate_datasets_cost(PF_BASICO_DATASETS),
		"query_id": payload.get("QueryId") or "",
		"score": {
			"score": int(fin_risk.get("FinancialRiskScore") or 0),
			"faixa_score": str(fin_risk.get("FinancialRiskLevel") or ""),
			"modelo": "BDC_FINANCIAL_RISK",
		},
		"renda_estimada": str(fin_risk.get("EstimatedIncomeRange") or ""),
		"patrimonio_estimado": str(fin_risk.get("TotalAssets") or ""),
		"provavel_inadimplente": bool(indebt.get("LikelyInDebt")),
		"fiscal": parse_government_debtors(payload),
		"processos": parse_lawsuits_distribution(payload),
		"compliance": parse_kyc_compliance(payload),
		"raw": payload,
	}


def fetch_pf_completo(
	cpf: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	include_detailed_lawsuits: bool = False,
) -> dict[str, Any]:
	"""Produto 4: PF Completo (Avançado) — COGS R$ 0,86 (ou R$ 0,94 com detalhamento de processos; 100% Core BDC)."""
	digits = only_digits(cpf)
	if len(digits) != 11:
		raise ValueError(f"CPF inválido: {cpf}")

	datasets_csv = f"{PF_COMPLETO_ALL_DATASETS},processes" if include_detailed_lawsuits else PF_COMPLETO_ALL_DATASETS
	payload = _post_bdc("pessoas", datasets_csv, f"doc{{{digits}}}", token, base_url, timeout)
	entity = first_entity(payload)
	fin_risk = entity.get("FinancialRisk") if isinstance(entity.get("FinancialRisk"), dict) else {}
	fam_risk = entity.get("FamilyFinancialRisk") if isinstance(entity.get("FamilyFinancialRisk"), dict) else {}
	indebt = entity.get("IndebtednessQuestion") if isinstance(entity.get("IndebtednessQuestion"), dict) else {}
	dfb = entity.get("DigitalFinanceBehaviors") if isinstance(entity.get("DigitalFinanceBehaviors"), dict) else {}
	vehicles = entity.get("Vehicles") if isinstance(entity.get("Vehicles"), list) else []

	return {
		"cpf": digits,
		"plano": "PF_COMPLETO",
		"cogs_estimado": estimate_datasets_cost(datasets_csv),
		"query_id": payload.get("QueryId") or "",
		"score": {
			"score": int(fin_risk.get("FinancialRiskScore") or 0),
			"faixa_score": str(fin_risk.get("FinancialRiskLevel") or ""),
			"modelo": "BDC_FINANCIAL_RISK_EXTENDED",
		},
		"renda_estimada": str(fin_risk.get("EstimatedIncomeRange") or ""),
		"patrimonio_estimado": str(fin_risk.get("TotalAssets") or ""),
		"risco_familiar": {
			"score": int(fam_risk.get("FamilyFinancialRiskScore") or 0),
			"nivel": str(fam_risk.get("FamilyFinancialRiskLevel") or ""),
			"total_membros": int(fam_risk.get("TotalMembers") or 0),
			"membros_empregados": int(fam_risk.get("TotalCurrentlyEmployedMembers") or 0),
			"membros_em_cobranca": int(fam_risk.get("MembersCurrentlyOnCollection") or 0),
		},
		"provavel_inadimplente": bool(indebt.get("LikelyInDebt")),
		"comportamento_financeiro_digital": {
			"busca_por_credito_nota": str(dfb.get("CreditSeeker") or "N/A"),
			"cartao_credito_nota": str(dfb.get("CreditCardScore") or dfb.get("CreditCardScore_v2") or "N/A"),
			"internet_banking_nota": str(dfb.get("OnlineBankingUser") or "N/A"),
		},
		"veiculos_vinculados": len(vehicles),
		"fiscal": parse_government_debtors(payload),
		"processos": parse_lawsuits_distribution(payload),
		"compliance": parse_kyc_compliance(payload),
		"raw": payload,
	}


def _build_localize_response(
	documento: str,
	plano: str,
	token: str,
	include_emails_and_addresses: bool,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	opencnpj_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Executa a busca de contatos (Produto 5 = Só Telefones/WhatsApp | Produto 6 = Localize Completo c/ E-mails e Endereços CENPROT)
	para CPF (11 dígitos) ou CNPJ (14 dígitos)."""
	digits = only_digits(documento)
	if len(digits) == 11:
		datasets_csv = LOCALIZE_COMPLETO_CPF_DATASETS if include_emails_and_addresses else LOCALIZE_TELEFONES_CPF_DATASETS
		payload = _post_bdc("pessoas", datasets_csv, f"doc{{{digits}}}", token, base_url, timeout)
		entity = first_entity(payload)
		basic = entity.get("BasicData") if isinstance(entity.get("BasicData"), dict) else {}
		found_phones = extract_phones(payload)
		found_emails = extract_emails(payload) if include_emails_and_addresses else []
		found_addrs = extract_addresses(payload) if include_emails_and_addresses else []
		return {
			"documento": digits,
			"tipo_documento": "CPF",
			"plano": plano,
			"cogs_estimado": estimate_datasets_cost(datasets_csv),
			"query_id": payload.get("QueryId") or "",
			"nome": str(basic.get("Name") or ""),
			"situacao_cadastral": str(basic.get("TaxIdStatus") or ""),
			"data_nascimento": str(basic.get("BirthDate") or ""),
			"nome_mae": str(basic.get("MotherName") or ""),
			"obito": bool(basic.get("HasObitIndication")),
			"telefones": found_phones,
			"total_telefones": len(found_phones),
			"total_whatsapp": sum(1 for p in found_phones if p.get("whatsapp")),
			"total_nao_perturbe": sum(1 for p in found_phones if p.get("dnd")),
			"emails": found_emails,
			"enderecos": found_addrs,
			"socios": [],
			"raw": payload,
		}

	if len(digits) == 14:
		datasets_csv = LOCALIZE_COMPLETO_CNPJ_DATASETS if include_emails_and_addresses else LOCALIZE_TELEFONES_CNPJ_DATASETS
		payload = _post_bdc("empresas", datasets_csv, f"doc{{{digits}}}", token, base_url, timeout)
		entity = first_entity(payload)
		ext_phones = entity.get("ExtendedPhones") if isinstance(entity.get("ExtendedPhones"), dict) else {}
		ext_emails = entity.get("ExtendedEmails") if isinstance(entity.get("ExtendedEmails"), dict) else {}
		ext_addrs = entity.get("ExtendedAddresses") if isinstance(entity.get("ExtendedAddresses"), dict) else {}
		company_only_payload = {
			"QueryId": payload.get("QueryId") or "",
			"Result": [
				{
					"ExtendedPhones": ext_phones,
					"ExtendedEmails": ext_emails,
					"ExtendedAddresses": ext_addrs,
				}
			],
		}
		company_phones = extract_phones(company_only_payload)
		company_emails = extract_emails(company_only_payload) if include_emails_and_addresses else []
		company_addrs = extract_addresses(company_only_payload) if include_emails_and_addresses else []

		socios_enriched: list[dict[str, Any]] = []
		for partner in extract_partners(payload):
			p_cpf = partner["cpf"]
			p_phones = extract_phones(payload, partner_cpf=p_cpf)
			p_emails = extract_emails(payload, partner_cpf=p_cpf) if include_emails_and_addresses else []
			p_addrs = extract_addresses(payload, partner_cpf=p_cpf) if include_emails_and_addresses else []
			socios_enriched.append(
				{
					"cpf": p_cpf,
					"nome": partner["name"],
					"qualificacao": partner["qualification"],
					"participacao": partner.get("share"),
					"outras_empresas": partner.get("other_companies") or [],
					"telefones": p_phones,
					"total_whatsapp": sum(1 for p in p_phones if p.get("whatsapp")),
					"emails": p_emails,
					"enderecos": p_addrs,
				}
			)

		all_phones = extract_phones(payload)
		oc = opencnpj_data or {}
		return {
			"documento": digits,
			"tipo_documento": "CNPJ",
			"plano": plano,
			"cogs_estimado": estimate_datasets_cost(datasets_csv),
			"query_id": payload.get("QueryId") or "",
			"nome": str(oc.get("razao_social") or oc.get("legal_name") or ""),
			"nome_fantasia": str(oc.get("nome_fantasia") or oc.get("trade_name") or ""),
			"situacao_cadastral": str(oc.get("situacao_cadastral") or oc.get("registration_status") or ""),
			"telefones_empresa": company_phones,
			"telefones": all_phones,
			"total_telefones": len(all_phones),
			"total_whatsapp": sum(1 for p in all_phones if p.get("whatsapp")),
			"total_nao_perturbe": sum(1 for p in all_phones if p.get("dnd")),
			"emails": company_emails,
			"enderecos": company_addrs,
			"socios": socios_enriched,
			"raw": payload,
		}

	raise ValueError(f"Documento inválido (informe um CPF de 11 dígitos ou CNPJ de 14 dígitos): {documento}")


def fetch_localize_telefones(
	documento: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	opencnpj_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Produto 5: Localize Só Telefones / WhatsApp (Enxuto):
	- CPF: COGS R$ 0,10 (`basic_data,phones_extended`)
	- CNPJ: COGS R$ 0,16 fixos (`relationships,phones_extended,related_people_phones`) — devolve todos os sócios
	  com CPF de 11 dígitos desmascarado + telefones da empresa + celulares/WhatsApp de TODOS os sócios.
	"""
	return _build_localize_response(
		documento=documento,
		plano="LOCALIZE_TELEFONES",
		token=token,
		include_emails_and_addresses=False,
		base_url=base_url,
		timeout=timeout,
		opencnpj_data=opencnpj_data,
	)


def fetch_localize_completo(
	documento: str,
	token: str,
	base_url: str = DEFAULT_BASE_URL,
	timeout: int = DEFAULT_TIMEOUT,
	opencnpj_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Produto 6: Localize Completo c/ E-mails + Endereços CENPROT:
	- CPF: COGS R$ 0,22 (`basic_data,phones_extended,emails_extended,addresses_extended`)
	- CNPJ: COGS R$ 0,40 fixos (`relationships,phones_extended,related_people_phones,emails_extended,
	  related_people_emails,addresses_extended,related_people_addresses`) — devolve todos os sócios com CPF
	  desmascarado + celulares/WhatsApp + e-mails + endereços residenciais c/ CEP de 8 dígitos prontos p/ CENPROT.
	"""
	return _build_localize_response(
		documento=documento,
		plano="LOCALIZE_COMPLETO",
		token=token,
		include_emails_and_addresses=True,
		base_url=base_url,
		timeout=timeout,
		opencnpj_data=opencnpj_data,
	)


# Adaptadores Drop-In para `cred_hub/credit_check/enrich.py` ---------------------------------------------------


def fetch_lawsuits_bdc(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
	"""Substituto direto de `escavador.fetch_lawsuits` e `directd.fetch_lawsuits_directd` (R$ 0,04)."""
	digits = only_digits(cnpj)
	payload = _post_bdc("empresas", "lawsuits_distribution_data", f"doc{{{digits}}}", token, base_url)
	return parse_lawsuits_distribution(payload)


def fetch_fiscal_bdc(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
	"""Substituto direto de `infosimples.fetch_fiscal_and_protests` e `directd.fetch_fgts_regularidade` (R$ 0,06)."""
	digits = only_digits(cnpj)
	payload = _post_bdc("empresas", "government_debtors", f"doc{{{digits}}}", token, base_url)
	return parse_government_debtors(payload)


def fetch_kyc_compliance_bdc(cnpj: str, token: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
	"""Substituto direto de `cgu.fetch_ceis_cnep` com cobertura ampliada (PEP, OFAC, ONU, MTE) (R$ 0,06)."""
	digits = only_digits(cnpj)
	payload = _post_bdc("empresas", "kyc", f"doc{{{digits}}}", token, base_url)
	return parse_kyc_compliance(payload)
