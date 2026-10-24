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
scheduler_events = {"daily": ["cred_hub.cnpj.enrich.update_years_in_business"]}

doc_events = {
	"CRM Organization": {
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": "cred_hub.cnpj.events.on_update",
	},
	"CRM Lead": {
		"validate": "cred_hub.cnpj.events.validate",
		"on_update": "cred_hub.cnpj.events.on_update",
	},
}
