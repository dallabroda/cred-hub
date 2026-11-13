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
	# Idempotency-Key das escritas pela API: guardadas por 24 h (credtech#375)
	"hourly": ["cred_hub.api_idempotency.delete_expired"],
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

# Chaves da API do HubTech (credtech#175) e roteador REST soberano /api/v1/*: Bearer hub_..., escopos, rate limit e log
before_request = ["cred_hub.rest_v1.ensure_registered"]
auth_hooks = ["cred_hub.api_keys.authenticate"]
after_request = ["cred_hub.api_keys.after_request"]
# O usuário técnico de uma chave nunca entra pela tela
on_session_creation = "cred_hub.api_keys.block_login"
# O crm esconde o usuário técnico das listas, dos membros dos funis e da distribuição
crm_hidden_users = ["cred_hub.api_keys.technical_users"]
# Chave com "Campos e regras" (fields_admin) cria e altera campos e formulários pela API (credtech#375)
crm_field_admin = ["cred_hub.api_keys.is_fields_admin_call"]
# Arquivos lidos por uma chave seguem o card a que estão anexados (funis da chave; audit M10)
# Chave de teste (credtech#375): só enxerga e altera registros do sandbox (cred_hub.sandbox)
_SANDBOX_QUERY = "cred_hub.sandbox.query_conditions"
_SANDBOX_PERMISSION = "cred_hub.sandbox.has_permission"
_SANDBOX_GUARDED = (
	"Contact",
	"CRM Organization",
	"CRM Lead",
	"FCRM Note",
	"CRM Task",
	"Cred Hub Checklist Item",
	"Cred Hub Visit Report",
	"Cred Hub Credit Report",
	"CRM Pipeline",
	"CRM Deal Status",
)
permission_query_conditions = {
	"File": ["cred_hub.api_guard.file_query_conditions", _SANDBOX_QUERY],
	**{doctype: _SANDBOX_QUERY for doctype in _SANDBOX_GUARDED},
}
has_permission = {
	"File": ["cred_hub.api_guard.has_file_permission", _SANDBOX_PERMISSION],
	**{doctype: _SANDBOX_PERMISSION for doctype in _SANDBOX_GUARDED},
}

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
		"after_insert": ["cred_hub.webhooks.on_organization_insert"],
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update", "cred_hub.webhooks.on_organization_update"],
	},
	# Card criado de um lead com CNPJ: o CNPJ vai para a Organização do card (épico #66)
	# Mudança de etapa do funil dispara esteira progressiva de crédito se configurado (credtech#260)
	"CRM Deal": {
		"validate": [
			"cred_hub.credit_check.onboarding.validate_deal_stage_gate",
			# Sandbox da API: chave de teste só em cards do funil Sandbox, e o Sandbox só com registros de teste
			"cred_hub.sandbox.validate_deal",
		],
		"after_insert": [
			"cred_hub.cnpj.card.after_insert",
			"cred_hub.webhooks.on_deal_insert",
		],
		# Funil de Cobrança (CENPROT, #290), esteira de crédito (#260/#302) e webhooks de saída
		"on_update": [
			"cred_hub.integrations.protest.on_deal_update",
			"cred_hub.credit_check.events.on_deal_update",
			# Card entered the configured stage of the credit funnel: cedente goes to the ERP (credtech#367)
			# (through the sandbox: a Sandbox card never goes to the ERP)
			"cred_hub.sandbox.erp_cedente_on_deal_update",
			"cred_hub.webhooks.on_deal_update",
		],
		"on_trash": "cred_hub.webhooks.on_deal_trash",
	},
	"Contact": {
		"validate": ["cred_hub.party.events.validate_contact", "cred_hub.sandbox.validate_contact"],
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
	# O funil Sandbox da API (chaves de teste) não é excluído nem renomeado, nem suas etapas excluídas, enquanto houver
	# chave de teste não revogada ou dado de teste (credtech#375)
	"CRM Pipeline": {
		"validate": "cred_hub.sandbox.validate_pipeline",
		"before_rename": "cred_hub.sandbox.protect_pipeline",
		"on_update": _OPENAPI,
		"on_trash": ["cred_hub.sandbox.protect_pipeline", _OPENAPI],
	},
	"CRM Deal Status": {"on_update": _OPENAPI, "on_trash": ["cred_hub.sandbox.protect_stage", _OPENAPI]},
	"CRM Lead": {
		# Empresa pelo domínio do e-mail (credtech#183): em fila, nunca atrasa a entrada do lead
		"after_insert": "cred_hub.email_company.finder.on_lead_insert",
		"validate": ["cred_hub.cnpj.events.validate", "cred_hub.sandbox.validate_lead"],
		"on_update": ["cred_hub.cnpj.events.on_update", "cred_hub.company.events.on_update"],
	},
	"Cred Hub Checklist Item": {"validate": "cred_hub.sandbox.validate_organization_record"},
	"Cred Hub Visit Report": {"validate": "cred_hub.sandbox.validate_organization_record"},
}

# Sandbox da API (credtech#375): registros criados por chave de teste ganham a marca credhub_sandbox (indexada), que
# ninguém muda depois; notas, tarefas e arquivos também (para a sincronização e os webhooks)
for _doctype in ("Contact", "CRM Organization", "CRM Lead", "FCRM Note", "CRM Task", "File"):
	_events = doc_events.setdefault(_doctype, {})
	for _event, _method in (("before_insert", "cred_hub.sandbox.stamp"), ("validate", "cred_hub.sandbox.keep_flag")):
		_current = _events.get(_event) or []
		_events[_event] = [*([_current] if isinstance(_current, str) else _current), _method]

# Chave de teste nunca entrega um registro a uma pessoa nem menciona alguém (aviso e e-mail a gente real, credtech#375):
# responsável recusado antes do validate do crm (que já atribui), e @menção vira texto
for _doctype, _event, _method in (
	("CRM Deal", "before_validate", "cred_hub.sandbox.refuse_assignment"),
	("CRM Lead", "before_validate", "cred_hub.sandbox.refuse_assignment"),
	("CRM Task", "before_validate", "cred_hub.sandbox.refuse_assignment"),
	("Comment", "validate", "cred_hub.sandbox.strip_mentions"),
	("FCRM Note", "validate", "cred_hub.sandbox.strip_mentions"),
	("CRM Task", "validate", "cred_hub.sandbox.strip_mentions"),
):
	_events = doc_events.setdefault(_doctype, {})
	_current = _events.get(_event) or []
	_events[_event] = [*([_current] if isinstance(_current, str) else _current), _method]

# Conversão de lead pela chave de teste só encontra contatos e organizações do sandbox (sem oráculo de dado real)
extend_doctype_class = {"CRM Lead": ["cred_hub.sandbox.SandboxLeadMixin"]}

# Armazenamento de anexos no Amazon S3 (Pasta Digital, dossiês, pareceres e anexos de cards):
# quando `credhub_s3` está configurado no site/bench, todos os arquivos novos vão direto para o S3;
# sem configuração (dev local / testes unitários), cai automaticamente no disco local.
write_file = "cred_hub.storage.s3.write_file"
delete_file_data_content = "cred_hub.storage.s3.delete_file_data_content"
file_exists = "cred_hub.storage.s3.file_exists"
read_file_content = "cred_hub.storage.s3.read_file_content"
serve_private_file = "cred_hub.storage.s3.serve_private_file"
serve_public_file = "cred_hub.storage.s3.serve_public_file"
