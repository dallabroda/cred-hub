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
	"daily": ["cred_hub.cnpj.enrich.update_years_in_business", "cred_hub.billing.daily_check"],
	# Rede de segurança dos leads da Meta: o que o webhook não trouxe (credtech#51)
	"cron": {"*/10 * * * *": ["cred_hub.meta_leads.catch_up"]},
}

# Quem chega sem conta vai para o cadastro de empresa nova (credtech#25)
oauth_without_account = "cred_hub.signup_access.oauth_without_account"
login_signup_url = "cred_hub.signup_access.login_signup_url"

doc_events = {
	# Limite de usuários do plano (2 grátis + extras pagos pelo Asaas, credtech#25)
	"User": {"validate": "cred_hub.billing.validate_user"},
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
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
}
