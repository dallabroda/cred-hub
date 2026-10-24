"""Tabela de-para CNAE -> setor de atuação (CRM Industry), aprovada pelo dono em 24/10/2026.

Os nomes dos setores ficam em inglês no banco (regra de idioma); a tradução pt-BR está em
`cred_hub/locale/pt_BR.po`. Áreas grandes descem ao grupo/classe do CNAE (3 ou 4 dígitos); o resto vai
pela divisão (2 dígitos). Vale o prefixo mais longo que casar com o código do CNAE.
"""

from cred_hub.cnpj.mapping import only_digits

# (prefixos do CNAE, setor em inglês, setor em pt-BR)
SECTORS = (
	# Agro, extração e indústria
	(("01",), "Agriculture and Livestock", "Agricultura e pecuária"),
	(("02", "03"), "Forestry, Fishing and Aquaculture", "Silvicultura, pesca e aquicultura"),
	(("05", "06", "07", "08", "09"), "Mining, Oil and Gas", "Mineração, petróleo e gás"),
	(("10", "11", "12"), "Food and Beverage Manufacturing", "Indústria de alimentos e bebidas"),
	(("13", "14", "15"), "Textile, Apparel and Footwear Manufacturing", "Indústria têxtil, vestuário e calçados"),
	(("16", "17", "18"), "Wood, Paper and Printing", "Indústria de madeira, papel e gráfica"),
	(("19", "20", "21", "22"), "Chemicals, Pharmaceuticals and Plastics", "Indústria química, farmacêutica e plásticos"),
	(("23", "24", "25"), "Minerals, Metallurgy and Metal Products", "Indústria de minerais, metalurgia e produtos de metal"),
	(("26", "27"), "Electronics and Electrical Equipment", "Indústria eletroeletrônica"),
	(("28", "29", "30"), "Machinery and Vehicle Manufacturing", "Indústria de máquinas e veículos"),
	(("31", "32", "33"), "Furniture and Other Manufacturing", "Indústria de móveis e outras"),
	# Energia e construção
	(("35",), "Electricity and Gas", "Energia elétrica e gás"),
	(("36", "37", "38", "39"), "Water, Sanitation and Waste", "Água, saneamento e resíduos"),
	(("41",), "Building Construction and Development", "Construção de edifícios e incorporação"),
	(("42",), "Infrastructure Works", "Obras de infraestrutura"),
	(("43",), "Specialized Construction Services", "Serviços especializados de construção"),
	# Comércio
	(("45",), "Vehicle Sales and Repair", "Comércio e manutenção de veículos"),
	(("46",), "Wholesale and Distribution", "Comércio atacadista e distribuição"),
	(("471", "472"), "Supermarkets and Food Retail", "Supermercados e comércio de alimentos"),
	(("473",), "Fuel Stations", "Postos de combustível"),
	(("474", "475"), "Building Materials, Furniture and Appliances Retail", "Materiais de construção, móveis e eletro"),
	(("476",), "Bookstores, Stationery and Cultural Goods", "Livrarias, papelarias e artigos culturais"),
	(("477",), "Pharmacies, Perfumeries and Opticians", "Farmácias, perfumarias e óticas"),
	(("4781", "4782", "4783"), "Apparel, Footwear and Accessories Retail", "Vestuário, calçados e acessórios"),
	(("4784", "4785", "4789", "479"), "Other Retail", "Outros varejos"),
	# Transporte e turismo
	(("4911", "493", "494"), "Road and Rail Freight Transport", "Transporte rodoviário de cargas"),
	(("4912", "492", "495"), "Passenger Transport", "Transporte de passageiros"),
	(("50", "51"), "Water and Air Transport", "Transporte aquaviário e aéreo"),
	(("52", "53"), "Warehousing, Logistics and Postal Services", "Armazenagem, logística e correios"),
	(("55",), "Hotels and Lodging", "Hotéis e hospedagem"),
	(("56",), "Restaurants, Bars and Food Services", "Restaurantes, bares e alimentação"),
	(("79",), "Travel Agencies and Tourism", "Agências de viagem e turismo"),
	# Tecnologia, mídia e comunicação
	(("58", "59", "60"), "Publishing, Audiovisual and Broadcasting", "Editoras, audiovisual e rádio/TV"),
	(("61",), "Telecommunications and Internet", "Telecomunicações e internet"),
	(("6201", "6202", "6203"), "Software Development", "Desenvolvimento de software"),
	(("6204", "6209"), "IT Consulting and Services", "Consultoria e serviços de TI"),
	(("63",), "Data, Hosting and Web Portals", "Dados, hospedagem e portais"),
	# Finanças e imóveis
	(("64",), "Banking, Credit and Financing", "Bancos, crédito e financeiras"),
	(("65",), "Insurance and Pension Funds", "Seguros e previdência"),
	(("66",), "Brokerage and Financial Services", "Corretoras e serviços financeiros"),
	(("68",), "Real Estate", "Imobiliárias e administração de imóveis"),
	# Serviços profissionais e empresariais
	(("691",), "Legal Services", "Advocacia e serviços jurídicos"),
	(("692",), "Accounting and Auditing", "Contabilidade e auditoria"),
	(("70",), "Management Consulting", "Consultoria em gestão"),
	(("71", "72"), "Engineering, Architecture and Research", "Engenharia, arquitetura e pesquisa"),
	(("73",), "Advertising, Marketing and Market Research", "Publicidade, marketing e pesquisa de mercado"),
	(("74", "75"), "Design, Photography and Veterinary", "Design, fotografia e veterinária"),
	(("77",), "Vehicle and Equipment Rental", "Aluguel de veículos e equipamentos"),
	(("78",), "Recruitment and Temporary Staffing", "Recrutamento e mão de obra temporária"),
	(("80", "81"), "Security, Cleaning and Facilities", "Segurança, limpeza e facilities"),
	(("82",), "Administrative Services and Call Centers", "Serviços administrativos e call center"),
	# Setor público, educação, saúde e outros
	(("84",), "Public Administration", "Administração pública"),
	(("85",), "Education", "Educação e ensino"),
	(("861",), "Hospitals", "Hospitais"),
	(("862", "863", "864"), "Clinics, Dental Offices and Laboratories", "Clínicas, consultórios, odontologia e laboratórios"),
	(("865", "866", "869"), "Other Health Services", "Outros serviços de saúde"),
	(("87", "88"), "Social Assistance and Care", "Assistência social e cuidados"),
	(("90", "91", "92"), "Culture, Arts and Entertainment", "Cultura, artes e entretenimento"),
	(("93",), "Sports, Gyms and Leisure", "Esportes, academias e lazer"),
	(("94",), "Associations, Unions and NGOs", "Associações, sindicatos e ONGs"),
	(("95", "96"), "Repairs and Personal Services", "Reparos e serviços pessoais"),
	(("97", "98", "99"), "Other Activities", "Outros"),
)

_BY_PREFIX = {prefix: sector for prefixes, sector, _pt in SECTORS for prefix in prefixes}
_LONGEST = max(len(p) for p in _BY_PREFIX)


def sector_for_cnae(cnae) -> str | None:
	"""Setor (nome em inglês) do código CNAE; aceita "6204000", "6204-0/00" ou "6204000 - Consultoria…"."""
	code = only_digits(str(cnae or "").split(" - ")[0])
	if len(code) < 2:
		return None
	for size in range(min(_LONGEST, len(code)), 1, -1):
		sector = _BY_PREFIX.get(code[:size])
		if sector:
			return sector
	return None
