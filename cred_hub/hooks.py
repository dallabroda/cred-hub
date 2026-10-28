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
	],
	# Rede de segurança dos leads da Meta: o que o webhook não trouxe (credtech#51)
	"cron": {"*/10 * * * *": ["cred_hub.meta_leads.catch_up"]},
}

# Chaves da API do HubTech (credtech#175): Bearer hub_..., escopos, rate limit e log das escritas
auth_hooks = ["cred_hub.api_keys.authenticate"]
after_request = ["cred_hub.api_keys.after_request"]
# O usuário técnico de uma chave nunca entra pela tela
on_session_creation = "cred_hub.api_keys.block_login"
# O crm esconde o usuário técnico das listas, dos membros dos funis e da distribuição
crm_hidden_users = ["cred_hub.api_keys.technical_users"]

# Quem chega sem conta vai para o cadastro de empresa nova (credtech#25)
oauth_without_account = "cred_hub.signup_access.oauth_without_account"
# Assunto do e-mail de boas-vindas do Frappe (com o link para criar a senha): "Boas-vindas ao HubTech CRM"
welcome_email = "cred_hub.site_setup.welcome_subject"
# Conta de envio do sistema (site_setup.EMAIL_ACCOUNT e o nome antigo): o crm a esconde da tela e não deixa editar
crm_system_email_accounts = ["HubTech CRM", "Cred Hub"]
# Linha do tempo do card: o lead automático aparece com o nome da origem, não como Guest (credtech#188)
crm_automatic_entries = ["cred_hub.lead_intake.automatic_entries"]
login_signup_url = "cred_hub.signup_access.login_signup_url"

doc_events = {
	# Limite de usuários do plano (2 grátis + extras pagos pelo Asaas, credtech#25)
	# O usuário técnico das chaves da API não vira humano nem ganha papéis (credtech#175)
	"User": {"validate": ["cred_hub.api_keys.validate_user", "cred_hub.billing.validate_user"]},
	"CRM Invitation": {"before_insert": "cred_hub.billing.validate_invitation"},
	"CRM Organization": {
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
	# Card criado de um lead com CNPJ: o CNPJ vai para a Organização do card (épico #66)
	"CRM Deal": {
		"after_insert": "cred_hub.cnpj.card.after_insert",
	},
	"CRM Lead": {
		# Empresa pelo domínio do e-mail (credtech#183): em fila, nunca atrasa a entrada do lead
		"after_insert": "cred_hub.email_company.finder.on_lead_insert",
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
}
