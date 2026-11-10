"""Testes unitários do backend BigDataCorp (Localize / Recuperação de Crédito e Esteira de Crédito PJ/PF)."""

from unittest.mock import MagicMock, patch
import pytest

from cred_hub.credit_check.clients.bigdatacorp import (
	BigDataCorpCreditAuthError,
	calculate_proprietary_score_pj,
	fetch_fiscal_bdc,
	fetch_kyc_compliance_bdc,
	fetch_lawsuits_bdc,
	fetch_localize_completo,
	fetch_localize_telefones,
	fetch_pf_basico,
	fetch_pf_completo,
	fetch_pj_basico,
	fetch_pj_completo,
)
from cred_hub.credit_check.bureau_vertical import (
	BUREAU_PRODUCTS,
	build_bigquery_leads_sql,
	evaluate_with_credit_engine,
	get_catalog_dict,
	run_product_query,
)
from cred_hub.integrations import bigdatacorp as bdc_localize
from cred_hub.integrations.bigdatacorp import BigDataCorpClient, BigDataCorpError

SAMPLE_BDC_CNPJ_PAYLOAD = {
	"QueryId": "7a0c1b61-a085-421a-8d00-b880e011d68e",
	"Result": [
		{
			"MatchKeys": "doc{13123234000178}",
			"Relationships": {
				"Relationships": [
					{
						"RelatedEntityTaxIdNumber": "32106328800",
						"RelatedEntityTaxIdType": "CPF",
						"RelatedEntityName": "ROBERTO DE BRITO MARTINS",
						"RelationshipType": "QSA",
						"RelationshipName": "SOCIO-ADMINISTRADOR",
						"OwnershipPercentage": 100.0,
						"RelationshipEndDate": "9999-12-31T23:59:59.9999999",
					},
					{
						"RelatedEntityTaxIdNumber": "61733513000104",
						"RelatedEntityTaxIdType": "CNPJ",
						"RelatedEntityName": "RENAN APARECIDO FIGUEIREDO SOUZA LTDA",
						"RelationshipType": "OWNERSHIP",
						"RelationshipName": "SOCIO-ADMINISTRADOR",
					},
				]
			},
			"ExtendedPhones": {
				"Phones": [
					{
						"AreaCode": "11",
						"Number": "40893425",
						"Type": "WORK",
						"ValidationStatus": "FOUND IN ABRT",
						"IsMain": True,
						"IsRecent": True,
						"IsActive": True,
						"IsInDoNotCallList": False,
						"CurrentCarrier": "VIVO",
					}
				]
			},
			"RelatedPeoplePhones": [
				{
					"AreaCode": "11",
					"Number": "998142390",
					"Type": "RELATED - 32106328800 - QSA - MOBILE",
					"ValidationStatus": "FOUND IN ABRT",
					"IsMain": True,
					"IsRecent": True,
					"IsActive": True,
					"IsInDoNotCallList": False,
					"CurrentCarrier": "VIVO",
					"PhoneTotalPassages": 14,
				},
				{
					"AreaCode": "11",
					"Number": "971024410",
					"Type": "RELATED - 32106328800 - QSA - MOBILE",
					"ValidationStatus": "FOUND IN ABRT",
					"IsMain": False,
					"IsRecent": False,
					"IsActive": True,
					"IsInDoNotCallList": True,
					"CurrentCarrier": "TIM",
				},
			],
			"ExtendedEmails": {
				"Emails": [
					{
						"EmailAddress": "financeiro@mrbombas.online",
						"Priority": 1,
						"IsMain": True,
						"IsRecent": True,
					}
				]
			},
			"RelatedPeopleEmails": [
				{
					"EmailAddress": "roberto.martins@mrbombas.com.br",
					"Type": "RELATED - 32106328800 - QSA - PERSONAL",
					"Priority": 1,
					"IsMain": True,
					"IsRecent": True,
				}
			],
			"ExtendedAddresses": {
				"Addresses": [
					{
						"Typology": "R",
						"AddressMain": "VARGEM GRANDE",
						"Number": "7",
						"Complement": "EDIF 09 SALA 05",
						"Neighborhood": "TATUAPE",
						"City": "SAO PAULO",
						"State": "SP",
						"ZipCode": "03316-020",
						"Priority": 1,
						"IsMain": True,
						"IsRecent": True,
					}
				]
			},
			"RelatedPeopleAddresses": [
				{
					"Typology": "R",
					"AddressMain": "SERRA DE BRAGANCA",
					"Number": "1140",
					"Complement": "APTO 82",
					"Neighborhood": "VILA GOMES CARDIM",
					"City": "SAO PAULO",
					"State": "SP",
					"ZipCode": "03318000",
					"Type": "RELATED - 32106328800 - QSA - HOME",
					"Priority": 1,
					"IsMain": True,
					"IsRecent": True,
				}
			],
			"KycData": {
				"IsCurrentlyPEP": False,
				"PEPHistory": [],
				"SanctionsHistory": [
					{
						"Source": "ofac",
						"Type": "Money Laundering",
						"MatchRate": 4,
						"IsCurrentlyPresentOnSource": False,
					}
				],
			},
			"GovernmentDebtors": {
				"TotalDebtValue": "0,0",
				"TotalDebts": 0,
				"Debts": [],
			},
			"LawsuitsDistributionData": {
				"TotalLawsuits": 2,
				"PartyTypeDistribution": {"DEFENDANT": 1, "AUTHOR": 1},
				"TypeDistribution": {"ACAO TRABALHISTA": 1, "PROCEDIMENTO COMUM": 1},
				"CourtTypeDistribution": {"TRABALHISTA": 1, "CIVEL": 1},
			},
			"Collections": {
				"IsCurrentlyOnCollection": False,
				"Last30DaysCollectionOccurrences": 0,
				"Last90DaysCollectionOccurrences": 1,
				"Last180DaysCollectionOccurrences": 2,
				"Last365DaysCollectionOccurrences": 2,
				"ConsecutiveCollectionMonths": 0,
			},
			"ActivityIndicators": {
				"IncomeRange": "R$ 1,2M a R$ 2,4M",
				"EmployeesRange": "10 a 49",
				"ActivityLevel": "ALTO",
			},
		}
	],
}

SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD = {
	"QueryId": "core-adv-998877",
	"Result": [
		{
			**SAMPLE_BDC_CNPJ_PAYLOAD["Result"][0],
			"CompanyEvolutionData": {
				"AverageCapital1YearAgo": 100000.0,
				"AverageCapital3YearsAgo": 50000.0,
				"AverageCapital5YearsAgo": 50000.0,
				"AverageQtyEmployees1YearAgo": 25.0,
				"AverageQtyEmployees3YearsAgo": 12.0,
				"HasQSAChangedAnytime": False,
				"TotalChanges365Days": 0,
				"YearOverYearGrowthRateStatus1YearAgo": "GROW UP",
			},
			"DigitalFinanceBehaviors": {
				"CreditSeeker": "B",
				"PaymentServicesUser": "A",
				"OnlineBankingUser": "A",
				"CreditCardScore": "B",
				"OnlineInvestor": "C",
			},
			# Formatos conforme exemplos da documentação oficial da BigDataCorp
			"OwnersLawsuitsDistributionData": {
				"TotalOwners": 1,
				"TotalLawsuits": 1,
				"TotalLawsuitsAsAuthor": 1,
				"TotalLawsuitsAsDefendant": 0,
			},
			"OwnersKycData": {
				"CompanyOwnersKycData": {},
				"PeopleOwnersKycData": {
					"32106328800": {"PEPHistory": [], "IsCurrentlyPEP": True, "SanctionsHistory": [], "IsCurrentlySanctioned": False},
				},
			},
			"EconomicGroupRelationship": {
				"Relationships": [
					{"RelatedEntityTaxIdNumber": "13123234000178", "RelatedEntityTaxIdType": "CNPJ", "RelatedEntityName": "MR BOMBAS LTDA"},
					{"RelatedEntityTaxIdNumber": "61733513000104", "RelatedEntityTaxIdType": "CNPJ", "RelatedEntityName": "RENAN APARECIDO FIGUEIREDO SOUZA LTDA"},
					{"RelatedEntityTaxIdNumber": "32106328800", "RelatedEntityTaxIdType": "CPF", "RelatedEntityName": "ROBERTO DE BRITO MARTINS"},
				],
			},
			"PublicDisclosedDocumentsExposure": {
				"IsPresentInPublicDisclosedDocuments": False,
				"TotalOccurrences": 0,
				"OccurrencesBySource": {},
			},
			"SintegraData": {
				"State": "SP (CCC)",
				"Entries": [
					{
						"StateRegistration": "158599546110",
						"DocNumberStatus": "Sem restrição",
						"Status": "Habilitado",
						"StateRegistrationType": "IE Normal",
						"TributationRegime": "Normal",
					}
				],
			},
			"Lawsuits": {
				"Lawsuits": [
					{
						"Number": "1002345-88.2024.8.26.0100",
						"CourtName": "TJSP",
						"Type": "PROCEDIMENTO COMUM",
						"Value": 12500.0,
					}
				]
			},
		}
	],
}


def test_localize_partners_and_inline_contacts_without_extra_cpf_call():
	"""Garante que `partners` extrai o CPF desmascarado + contatos inline do sócio sem precisar pagar outra consulta."""
	assert bdc_localize.protocol_of(SAMPLE_BDC_CNPJ_PAYLOAD) == "7a0c1b61-a085-421a-8d00-b880e011d68e"

	found_partners = bdc_localize.partners(SAMPLE_BDC_CNPJ_PAYLOAD)
	assert len(found_partners) == 1
	partner = found_partners[0]
	assert partner["cpf"] == "32106328800"
	assert partner["name"] == "ROBERTO DE BRITO MARTINS"
	assert partner["share"] == 100.0
	assert len(partner["other_companies"]) == 1
	assert partner["other_companies"][0]["cnpj"] == "61733513000104"

	# Telefones exclusivos do sócio já extraídos da chamada do CNPJ
	partner_phones = bdc_localize.phones(SAMPLE_BDC_CNPJ_PAYLOAD, partner_cpf="32106328800")
	assert len(partner_phones) == 2
	assert partner_phones[0]["phone"] == "+5511998142390"
	assert partner_phones[0]["whatsapp"] is True
	assert partner_phones[0]["dnd"] is False
	assert partner_phones[1]["phone"] == "+5511971024410"
	assert partner_phones[1]["dnd"] is True  # PROCON Não Perturbe

	# E-mails e Endereços (com CEP de 8 dígitos para CENPROT) do sócio
	partner_emails = bdc_localize.emails(SAMPLE_BDC_CNPJ_PAYLOAD, partner_cpf="32106328800")
	assert partner_emails == ["roberto.martins@mrbombas.com.br"]

	partner_addrs = bdc_localize.addresses(SAMPLE_BDC_CNPJ_PAYLOAD, partner_cpf="32106328800")
	assert len(partner_addrs) == 1
	assert partner_addrs[0]["street"] == "RUA SERRA DE BRAGANCA"
	assert partner_addrs[0]["zip"] == "03318000"


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_fetch_pj_basico_and_proprietary_score(mock_post):
	mock_resp = MagicMock()
	mock_resp.status_code = 200
	mock_resp.ok = True
	mock_resp.json.return_value = SAMPLE_BDC_CNPJ_PAYLOAD
	mock_post.return_value = mock_resp

	res = fetch_pj_basico("13.123.234/0001-78", "my_access_token:my_token_id")
	assert res["plano"] == "PJ_BASICO"
	assert res["cnpj"] == "13123234000178"
	assert res["compliance"]["sanctioned"] is False  # MatchRate=4 ignorado (<80)
	assert res["fiscal"]["pgfn"]["status"] == "Regular"
	assert res["processos"]["defendant_count"] == 1
	assert res["cobrancas"]["last_90_days"] == 1
	assert res["score"]["modelo"] == "PROPRIETARIO_CORE_BDC"
	assert 700 <= res["score"]["score"] <= 800


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_fetch_pj_completo_100_percent_core_without_quod(mock_post):
	resp_core = MagicMock(status_code=200, ok=True)
	resp_core.json.return_value = SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD
	mock_post.return_value = resp_core

	res = fetch_pj_completo("13.123.234/0001-78", "my_access_token:my_token_id")
	assert res["plano"] == "PJ_COMPLETO"
	assert res["cogs_estimado"] == 0.90  # Com Sintegra padrão (sem basic_data redundante e sem processes detalhado)
	assert res["sintegra_consultado"] is True
	assert res["score"]["modelo"] == "PROPRIETARIO_CORE_BDC"
	assert res["score"]["score"] >= 700
	assert res["evolucao_empresa"]["crescimento_1_ano"] == "GROW UP"
	assert res["comportamento_financeiro_digital"]["busca_por_credito_nota"] == "B"
	assert res["risco_socios_e_grupo"]["total_processos_dos_socios"] == 1
	assert res["risco_socios_e_grupo"]["total_empresas_grupo_economico"] == 2
	assert res["risco_socios_e_grupo"]["exposicao_negativa_global"]["presente"] is False
	assert res["risco_socios_e_grupo"]["kyc_socios"]["socios_pep"] == 1
	assert res["risco_socios_e_grupo"]["kyc_socios"]["socios_sancionados"] == 0
	assert "owners_kyc" in res["datasets_consultados"]
	# Sintegra consultado de verdade na API On-demand (2ª chamada), com UF quando conhecida
	assert res["sintegra"]["habilitada"] is True
	assert res["sintegra"]["inscricoes"][0]["inscricao_estadual"] == "158599546110"
	ondemand_calls = [c for c in mock_post.call_args_list if c.args[0].endswith("/ondemand")]
	assert len(ondemand_calls) == 1
	assert ondemand_calls[0].kwargs["json"]["Datasets"] == "ondemand_sintegra_company"
	assert len(res["indicadores_positivos"]) == 7


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_smart_gating_rules_save_costs_automatically(mock_post):
	resp_core = MagicMock(status_code=200, ok=True)
	resp_core.json.return_value = SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD
	mock_post.return_value = resp_core

	# 1. Curto-circuito OpenCNPJ para empresa BAIXADA -> Custo R$ 0,00 e 0 chamadas na BigDataCorp
	res_inapta = fetch_pj_basico(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		opencnpj_data={"situacao_cadastral": "BAIXADA"},
	)
	assert res_inapta["short_circuited"] is True
	assert res_inapta["cogs_estimado"] == 0.0
	assert mock_post.call_count == 0

	# 2. Empresa 100% Serviços (CNAE 6201-5/01 TI) -> Pula Sintegra automaticamente (cai de R$ 0,46 para R$ 0,36)
	res_servico = fetch_pj_basico(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		opencnpj_data={"situacao_cadastral": "ATIVA", "cnae_fiscal": "6201501"},
	)
	assert res_servico["sintegra_consultado"] is False
	assert res_servico["cogs_estimado"] == 0.36

	# 3. Empresa MEI recém-aberta de Serviços no PJ Completo -> Pula Sintegra, company_evolution e economic_group
	res_mei_novo = fetch_pj_completo(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		opencnpj_data={
			"situacao_cadastral": "ATIVA",
			"cnae_fiscal": "6201501",
			"opcao_pelo_mei": True,
			"data_inicio_atividade": "2026-10-01",
		},
	)
	assert res_mei_novo["sintegra_consultado"] is False
	assert "company_evolution" not in res_mei_novo["datasets_consultados"]
	assert "economic_group_relationships" not in res_mei_novo["datasets_consultados"]
	assert res_mei_novo["cogs_estimado"] == 0.68


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_fetch_pf_basico_and_completo_without_quod(mock_post):
	resp_pf = MagicMock(status_code=200, ok=True)
	resp_pf.json.return_value = {
		"QueryId": "pf-core-123",
		"Result": [
			{
				"FinancialRisk": {
					"FinancialRiskScore": 785,
					"FinancialRiskLevel": "B",
					"EstimatedIncomeRange": "R$ 10.000 a R$ 15.000",
					"TotalAssets": "R$ 500K a R$ 1M",
				},
				"FamilyFinancialRisk": {
					"TotalMembers": 3,
					"TotalCurrentlyEmployedMembers": 1,
					"MembersCurrentlyOnCollection": 0,
					"FamilyFinancialRiskScore": 450,
					"FamilyFinancialRiskLevel": "D",
				},
				"IndebtednessQuestion": {"LikelyInDebt": False},
				"DigitalFinanceBehaviors": {
					"CreditSeeker": "C",
					"CreditCardScore": "A",
					"OnlineBankingUser": "A",
				},
				"Vehicles": [
					{"Brand": "TOYOTA", "Model": "COROLLA XEI 2.0", "FuelType": "FLEX"},
					{"Brand": "HONDA", "Model": "CG 160 FAN", "FuelType": "GASOLINE"},
				],
				"Processes": {
					"TotalLawsuits": 1,
					"TotalLawsuitsAsDefendant": 1,
					"Lawsuits": [{"Number": "0001234-56.2025.8.26.0100", "CourtName": "TJSP", "Type": "EXECUCAO", "Value": 9800.0}],
				},
			}
		],
	}
	mock_post.return_value = resp_pf

	res_basico = fetch_pf_basico("321.063.288-00", "my_access_token:my_token_id")
	assert res_basico["plano"] == "PF_BASICO"
	assert res_basico["cogs_estimado"] == 0.48
	assert res_basico["score"]["score"] == 785
	assert res_basico["provavel_inadimplente"] is False

	res_comp = fetch_pf_completo("321.063.288-00", "my_access_token:my_token_id")
	assert res_comp["plano"] == "PF_COMPLETO"
	assert res_comp["cogs_estimado"] == 0.86
	assert res_comp["risco_familiar"]["nivel"] == "D"
	assert res_comp["risco_familiar"]["score"] == 450
	assert res_comp["comportamento_financeiro_digital"]["cartao_credito_nota"] == "A"
	assert res_comp["veiculos_vinculados"] == 2
	# Detalhe de processos em /pessoas vem na chave `Processes`
	assert res_comp["processos"]["highest_value"] == 9800.0

	res_comp_detail = fetch_pf_completo("321.063.288-00", "my_access_token:my_token_id", include_detailed_lawsuits=True)
	assert res_comp_detail["cogs_estimado"] == 0.94


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_sintegra_uses_uf_and_failure_is_not_charged(mock_post):
	ok_resp = MagicMock(status_code=200, ok=True)
	ok_resp.json.return_value = SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD
	fail_resp = MagicMock(status_code=500, ok=False, text="fonte indisponível")

	def route(url, json, headers, timeout):
		return fail_resp if url.endswith("/ondemand") else ok_resp

	mock_post.side_effect = route
	res = fetch_pj_basico(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		opencnpj_data={"situacao_cadastral": "ATIVA", "cnae_fiscal": "4663000", "uf": "sp"},
	)
	ondemand_call = next(c for c in mock_post.call_args_list if c.args[0].endswith("/ondemand"))
	assert ondemand_call.kwargs["json"]["q"] == "doc{13123234000178},uf{SP}"
	assert res["sintegra_consultado"] is False
	assert "erro" in res["sintegra"]
	assert res["cogs_estimado"] == 0.36  # Sintegra falhou: não entra no custo


def test_partner_sanction_lowers_score():
	base = {"has_bankruptcy_recovery": False, "defendant_count": 0}
	fiscal = {"pgfn": {"total_debt": 0}}
	clean = calculate_proprietary_score_pj(base, fiscal, {"sanctioned": False}, {}, advanced={"risco_socios_e_grupo": {"kyc_socios": {"socios_sancionados": 0}}})
	hit = calculate_proprietary_score_pj(base, fiscal, {"sanctioned": False}, {}, advanced={"risco_socios_e_grupo": {"kyc_socios": {"socios_sancionados": 1}}})
	assert clean["score"] - hit["score"] == 150
	assert any("sanção ativa" in m for m in hit["motivos"])


def test_revised_score_methodology_maturity_sintegra_and_hard_caps():
	base = {"has_bankruptcy_recovery": False, "defendant_count": 0}
	fiscal = {"pgfn": {"total_debt": 0}}
	coll_clean = {"is_currently_on_collection": False, "last_90_days": 0, "last_365_days": 0}

	# 1. Empresa recém-aberta (< 6 meses) não pode ficar na Faixa A (800+)
	new_co = calculate_proprietary_score_pj(
		base,
		fiscal,
		{"sanctioned": False},
		{},
		opencnpj_data={"data_inicio_atividade": "2026-10-01"},
	)
	assert new_co["score"] == 670
	assert new_co["faixa_score"] == "MODERADO (C)"

	# 2. Empresa consolidada (5+ anos, capital >= 500k, atividade alta, sem cobrança) atinge Faixa A (> 880)
	solid_co = calculate_proprietary_score_pj(
		base,
		fiscal,
		{"sanctioned": False},
		coll_clean,
		activity={"activity_level": "ALTO", "estimated_revenue_range": "R$ 10M a R$ 50M"},
		opencnpj_data={"data_inicio_atividade": "2015-01-10", "capital_social": "1000000.00"},
	)
	assert solid_co["score"] >= 880
	assert solid_co["faixa_score"] == "BAIXO (A)"

	# 3. Sintegra inapto penaliza -200 pontos
	sintegra_bad = calculate_proprietary_score_pj(
		base,
		fiscal,
		{"sanctioned": False},
		coll_clean,
		sintegra={"inscricoes": [{"inscricao_estadual": "123", "situacao": "Inapto"}], "habilitada": False},
	)
	assert sintegra_bad["score"] == 580

	# 4. Sanção ativa da própria empresa aplica Hard Cap na Faixa E (<= 350), mesmo para empresa grande/antiga
	sanctioned_co = calculate_proprietary_score_pj(
		base,
		fiscal,
		{"sanctioned": True},
		coll_clean,
		activity={"activity_level": "ALTO", "estimated_revenue_range": "R$ 10M a R$ 50M"},
		opencnpj_data={"data_inicio_atividade": "2015-01-10", "capital_social": "1000000.00"},
	)
	assert sanctioned_co["score"] <= 350
	assert sanctioned_co["faixa_score"] == "MUITO ALTO (E)"

	# 5. Protestos em cartório (quando consultados) reduzem o score conforme volume/valor
	protested_co = calculate_proprietary_score_pj(
		base,
		fiscal,
		{"sanctioned": False},
		coll_clean,
		protests={"has_protests": True, "protests_count": 4, "total_value": 18000.0},
	)
	assert protested_co["score"] == 670


@patch("cred_hub.credit_check.clients.bigdatacorp.fetch_protests_nacional")
@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_pj_completo_protests_smart_gating(mock_post, mock_protests):
	resp_core = MagicMock(status_code=200, ok=True)
	resp_core.json.return_value = SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD
	mock_post.return_value = resp_core
	mock_protests.return_value = {
		"has_protests": True,
		"protests_count": 2,
		"total_value": 4200.0,
		"cartorios_distintos": 1,
		"cidades_distintas": 1,
	}

	# 1. Empresa ativa sem trava eliminatória -> Consulta Protestos Nacional (R$ 0,90 + R$ 2,95 = R$ 3,85)
	res_ok = fetch_pj_completo(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		directd_token="directd_tok_123",
	)
	assert res_ok["protestos_consultado"] is True
	assert res_ok["protestos"]["protests_count"] == 2
	assert res_ok["cogs_estimado"] == 3.85
	assert mock_protests.call_count == 1

	# 2. Empresa com Falência/RJ na BigDataCorp -> Smart Gating pula a DirectD e economiza R$ 2,95!
	mock_protests.reset_mock()
	bankrupt_payload = {
		"QueryId": "ko-999",
		"Result": [
			{
				**SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD["Result"][0],
				"LawsuitsDistributionData": {
					"TotalLawsuits": 1,
					"PartyTypeDistribution": {"DEFENDANT": 1},
					"TypeDistribution": {"RECUPERACAO JUDICIAL": 1},
				},
			}
		],
	}
	resp_core.json.return_value = bankrupt_payload
	res_ko = fetch_pj_completo(
		"13.123.234/0001-78",
		"my_access_token:my_token_id",
		directd_token="directd_tok_123",
	)
	assert res_ko["protestos_consultado"] is False
	assert res_ko["protestos"].get("skipped_by_smart_gating") is True
	assert res_ko["cogs_estimado"] == 0.90
	assert mock_protests.call_count == 0


def test_missing_token_raises_auth_error():
	with pytest.raises(BigDataCorpCreditAuthError):
		fetch_pj_basico("13123234000178", "")


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_fetch_localize_telefones_and_completo_for_cpf_and_cnpj(mock_post):
	"""Valida os Produtos 5 (Localize Só Telefones) e 6 (Localize Completo c/ CENPROT) tanto para CPF quanto para CNPJ."""
	resp_cnpj = MagicMock(status_code=200, ok=True)
	resp_cnpj.json.return_value = SAMPLE_BDC_CNPJ_PAYLOAD

	resp_cpf = MagicMock(status_code=200, ok=True)
	resp_cpf.json.return_value = {
		"QueryId": "loc-pf-111",
		"Result": [
			{
				"BasicData": {
					"Name": "ROBERTO DE BRITO MARTINS",
					"TaxIdStatus": "REGULAR",
					"BirthDate": "1985-04-12",
					"MotherName": "MARIA DE BRITO",
					"HasObitIndication": False,
				},
				"ExtendedPhones": SAMPLE_BDC_CNPJ_PAYLOAD["Result"][0]["ExtendedPhones"],
				"ExtendedEmails": SAMPLE_BDC_CNPJ_PAYLOAD["Result"][0]["ExtendedEmails"],
				"ExtendedAddresses": SAMPLE_BDC_CNPJ_PAYLOAD["Result"][0]["ExtendedAddresses"],
			}
		],
	}

	# 1. Produto 5 (Só Telefones) no CPF -> COGS R$ 0,10
	mock_post.return_value = resp_cpf
	res_pf_tel = fetch_localize_telefones("321.063.288-00", "tok:id")
	assert res_pf_tel["plano"] == "LOCALIZE_TELEFONES"
	assert res_pf_tel["tipo_documento"] == "CPF"
	assert res_pf_tel["cogs_estimado"] == 0.10
	assert res_pf_tel["nome"] == "ROBERTO DE BRITO MARTINS"
	assert len(res_pf_tel["telefones"]) == 1
	assert res_pf_tel["emails"] == []
	assert res_pf_tel["enderecos"] == []

	# 2. Produto 6 (Localize Completo) no CPF -> COGS R$ 0,22
	res_pf_comp = fetch_localize_completo("321.063.288-00", "tok:id")
	assert res_pf_comp["plano"] == "LOCALIZE_COMPLETO"
	assert res_pf_comp["cogs_estimado"] == 0.22
	assert len(res_pf_comp["emails"]) == 1
	assert len(res_pf_comp["enderecos"]) == 1
	assert res_pf_comp["enderecos"][0]["zip"] == "03316020"

	# 3. Produto 5 (Só Telefones) no CNPJ -> COGS R$ 0,16 fixos (empresa + todos os sócios)
	mock_post.return_value = resp_cnpj
	res_pj_tel = fetch_localize_telefones("13.123.234/0001-78", "tok:id", opencnpj_data={"razao_social": "MR BOMBAS LTDA"})
	assert res_pj_tel["plano"] == "LOCALIZE_TELEFONES"
	assert res_pj_tel["tipo_documento"] == "CNPJ"
	assert res_pj_tel["cogs_estimado"] == 0.16
	assert len(res_pj_tel["socios"]) == 1
	assert res_pj_tel["socios"][0]["cpf"] == "32106328800"
	assert len(res_pj_tel["socios"][0]["telefones"]) == 2
	assert res_pj_tel["socios"][0]["emails"] == []
	assert res_pj_tel["socios"][0]["enderecos"] == []

	# 4. Produto 6 (Localize Completo c/ CENPROT) no CNPJ -> COGS R$ 0,40 fixos
	res_pj_comp = fetch_localize_completo("13.123.234/0001-78", "tok:id")
	assert res_pj_comp["plano"] == "LOCALIZE_COMPLETO"
	assert res_pj_comp["cogs_estimado"] == 0.40
	assert res_pj_comp["socios"][0]["emails"] == ["roberto.martins@mrbombas.com.br"]
	assert res_pj_comp["socios"][0]["enderecos"][0]["zip"] == "03318000"


@patch("cred_hub.credit_check.clients.bigdatacorp.requests.post")
def test_bureau_vertical_catalog_credit_engine_and_bigquery_leads(mock_post):
	"""Valida o catálogo de 6 produtos, o Motor de Crédito Automatizado e o Gerador SQL de Leads B2B (BigQuery)."""
	catalog = get_catalog_dict()
	assert len(catalog) == 6
	assert set(BUREAU_PRODUCTS.keys()) == {
		"PJ_BASICO",
		"PJ_COMPLETO",
		"PF_BASICO",
		"PF_COMPLETO",
		"LOCALIZE_TELEFONES",
		"LOCALIZE_COMPLETO",
	}

	mock_resp = MagicMock(status_code=200, ok=True)
	mock_resp.json.return_value = SAMPLE_ADVANCED_CORE_CNPJ_PAYLOAD
	mock_post.return_value = mock_resp

	res = run_product_query(
		"PJ_BASICO",
		"13.123.234/0001-78",
		"tok:id",
		opencnpj_data={"situacao_cadastral": "ATIVA", "cnae_fiscal": "6201501", "data_inicio_atividade": "2018-01-10"},
		requested_amount=10000.0,
	)
	assert "motor_credito" in res
	motor = res["motor_credito"]
	assert motor["verdict"] in ("Approve", "Approve with reservations")
	assert motor["suggested_limit_brl"] >= 15000.0

	# Testa SQL parametrizada do Gerador de Leads B2B no BigQuery
	sql, params = build_bigquery_leads_sql(
		uf="sp",
		cnaes=["6201-5/01", "4663-0/00"],
		portes=["ME", "EPP"],
		capital_social_min=50000,
		excluir_mei=True,
		somente_com_telefone=True,
		limit=250,
	)
	assert "basedosdados.br_rf_cnpj.estabelecimentos" in sql
	assert "est.sigla_uf = @uf" in sql
	assert params["uf"] == "SP"
	assert params["cnaes"] == ["6201501", "4663000"]
	assert params["portes"] == ["01", "03"]
	assert params["capital_min"] == 50000.0
	assert params["limit"] == 250

