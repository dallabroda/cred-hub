app_name = "cred_hub"
app_title = "Cred Hub"
app_publisher = "CredTech Digital"
app_description = "Customizações da CredTech para o Frappe CRM"
app_email = "eduardo_dallabrida@credtechdigital.com.br"
app_license = "agpl-3.0"

required_apps = ["crm"]

# Campos e layouts são (re)aplicados em toda instalação e migração, de forma idempotente
after_install = "cred_hub.install.after_install"
after_migrate = "cred_hub.install.after_install"

# Tempo de mercado (anos) muda com o tempo: recalculado todo dia
scheduler_events = {
	# Cobrança: diminuição na virada, tolerância vencida e aumento sem pagamento (credtech#25)
	"daily": [
		"cred_hub.cnpj.enrich.update_years_in_business",
		"cred_hub.billing.daily_check",
		"cred_hub.api_keys.delete_old_logs",
		# Webhooks: registro de entregas por 30 dias e outbox encerrada por 7 (credtech#375)
		"cred_hub.webhooks.delete_old_records",
	],
	# Status dos títulos em protesto no cartório (CENPROT, credtech#290)
	"daily_long": ["cred_hub.integrations.protest.sync_all"],
	# Rede de segurança dos leads da Meta: o que o webhook não trouxe (credtech#51)
	"cron": {
		"*/10 * * * *": ["cred_hub.meta_leads.catch_up"],
		# Webhooks: envia o que está na outbox (o disparo logo após o commit adianta; credtech#375)
		"* * * * *": ["cred_hub.webhooks.dispatch_outbox"],
		# Inadimplentes do ERP no funil de Cobrança (credtech#367): de manhã, depois da liquidação do dia
		"30 7 * * *": ["cred_hub.erp.sync.scheduled"],
		# Régua de cobrança (credtech#367): o passo do dia de cada título aberto, pelo WhatsApp do SmartChat
		"0 9 * * *": ["cred_hub.collection.reminders.scheduled"],
	},
}

# Chaves da API do HubTech (credtech#175): Bearer hub_..., escopos, rate limit e log das escritas
auth_hooks = ["cred_hub.api_keys.authenticate"]
after_request = ["cred_hub.api_keys.after_request"]
# O usuário técnico de uma chave nunca entra pela tela
on_session_creation = "cred_hub.api_keys.block_login"
# O crm esconde o usuário técnico das listas, dos membros dos funis e da distribuição
crm_hidden_users = ["cred_hub.api_keys.technical_users"]
# Chave com "Campos e regras" (fields_admin) cria e altera campos e formulários pela API (credtech#375)
crm_field_admin = ["cred_hub.api_keys.is_fields_admin_call"]

# Quem chega sem conta vai para o cadastro de empresa nova (credtech#25)
oauth_without_account = "cred_hub.signup_access.oauth_without_account"
# Assunto do e-mail de boas-vindas do Frappe (com o link para criar a senha): "Boas-vindas ao HubTech CRM"
welcome_email = "cred_hub.site_setup.welcome_subject"
# Conta de envio do sistema (site_setup.EMAIL_ACCOUNT e o nome antigo): o crm a esconde da tela e não deixa editar
crm_system_email_accounts = ["HubTech CRM", "Cred Hub"]
# Linha do tempo do card: o lead automático aparece com o nome da origem, não como Guest (credtech#188)
crm_automatic_entries = ["cred_hub.lead_intake.automatic_entries"]
login_signup_url = "cred_hub.signup_access.login_signup_url"

_OPENAPI = "cred_hub.api_schema.clear_openapi_cache"

doc_events = {
	# Limite de usuários do plano (2 grátis + extras pagos pelo Asaas, credtech#25)
	# O usuário técnico das chaves da API não vira humano nem ganha papéis (credtech#175)
	"User": {"validate": ["cred_hub.api_keys.validate_user", "cred_hub.billing.validate_user"]},
	"CRM Invitation": {"before_insert": "cred_hub.billing.validate_invitation"},
	# CPF é Contato e CNPJ é Organização (credtech#315): a validação do documento roda antes da do CNPJ
	"CRM Organization": {
		"validate": ["cred_hub.party.events.validate_organization", "cred_hub.cnpj.events.validate"],
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
	# Card criado de um lead com CNPJ: o CNPJ vai para a Organização do card (épico #66)
	# Mudança de etapa do funil dispara esteira progressiva de crédito se configurado (credtech#260)
	"CRM Deal": {
		"validate": "cred_hub.credit_check.onboarding.validate_deal_stage_gate",
		"after_insert": [
			"cred_hub.cnpj.card.after_insert",
			"cred_hub.webhooks.on_deal_insert",
		],
		# Funil de Cobrança (CENPROT, #290), esteira de crédito (#260/#302) e webhooks de saída
		"on_update": [
			"cred_hub.integrations.protest.on_deal_update",
			"cred_hub.credit_check.events.on_deal_update",
			# Card entered the configured stage of the credit funnel: cedente goes to the ERP (credtech#367)
			"cred_hub.erp.cedente.on_deal_update",
			"cred_hub.webhooks.on_deal_update",
		],
		"on_trash": "cred_hub.webhooks.on_deal_trash",
	},
	"Contact": {
		"validate": "cred_hub.party.events.validate_contact",
		"after_insert": "cred_hub.webhooks.on_contact_insert",
		"on_update": "cred_hub.webhooks.on_contact_update",
	},
	"CRM Task": {
		"after_insert": "cred_hub.integrations.google_calendar.sync_task_to_google_calendar",
		"on_update": "cred_hub.integrations.google_calendar.sync_task_to_google_calendar",
		"on_trash": "cred_hub.integrations.google_calendar.on_task_trash",
	},
	# OpenAPI por empresa (credtech#375): campos, funis e etapas mudaram, a especificação é gerada de novo
	"Custom Field": {"on_update": _OPENAPI, "on_trash": _OPENAPI},
	"Property Setter": {"on_update": _OPENAPI, "on_trash": _OPENAPI},
	"CRM Pipeline": {"on_update": _OPENAPI, "on_trash": _OPENAPI},
	"CRM Deal Status": {"on_update": _OPENAPI, "on_trash": _OPENAPI},
	"CRM Lead": {
		# Empresa pelo domínio do e-mail (credtech#183): em fila, nunca atrasa a entrada do lead
		"after_insert": "cred_hub.email_company.finder.on_lead_insert",
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
}
