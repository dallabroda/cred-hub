"""Padrões de cada site (empresa) do Cred Hub, num lugar só (#25).

Rodado pelo `deploy.sh credhub` em todo site existente e pelo cadastro self-service em cada empresa nova:

	bench --site <site> execute cred_hub.site_setup.apply_from_env

Pode repetir. Os segredos chegam por variável de ambiente (nunca na linha de comando nem no Git):
SMTP_ADDRESS, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, MAILER_SENDER_EMAIL (e-mail pelo Brevo, as mesmas
credenciais do Cred Chat) e AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET (login pelo Auth0 do Cred Chat).
Sem as variáveis de um grupo, aquele grupo é pulado (útil em desenvolvimento).

Antes de 25/10/2026 isto morava em scripts do repositório da plataforma (site_email.py, site_social_login.py,
site_translations.py) e em comandos soltos no deploy.sh.
"""

import json
import os
import re

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

LANGUAGE = "pt-BR"
TIME_ZONE = "America/Sao_Paulo"
CURRENCY = "BRL"

EMAIL_ACCOUNT = "Cred Hub"
WELCOME = "Cred Hub - Welcome"

# Os nomes continuam em inglês no banco e no código (o Frappe CRM depende deles); só a tela muda, conforme o
# idioma de cada usuário. Status de lead e etapas padrão são "translated_doctype" no Frappe CRM.
TRANSLATIONS = {
	# Etapas padrão do funil
	"Qualification": "Qualificação",
	"Demo/Making": "Demonstração",
	"Proposal/Quotation": "Proposta/Orçamento",
	"Negotiation": "Negociação",
	"Ready to Close": "Pronto para fechar",
	"Won": "Ganho",
	"Lost": "Perdido",
	# Status de lead
	"New": "Novo",
	"Contacted": "Contatado",
	"Nurture": "Em nutrição",
	"Qualified": "Qualificado",
	"Unqualified": "Desqualificado",
	"Junk": "Descartado",
}

# pt-BR por padrão; inglês para quem escolheu inglês (o idioma é do usuário)
WELCOME_SUBJECT = "{% if doc.language == 'en' %}Welcome to Cred Hub{% else %}Bem-vindo ao Cred Hub{% endif %}"
WELCOME_MESSAGE = """{% set url = frappe.utils.get_url("/crm") %}
{% if doc.language == 'en' %}
<p>Hi {{ doc.first_name or "" }},</p>
<p>Your Cred Hub account is ready.</p>
<p><a href="{{ url }}">Open Cred Hub</a> and sign in with Google, Microsoft or GitHub using this e-mail, or with your password.</p>
<p>If you can't see your company's data yet, ask your company's administrator to give you access.</p>
<p>CredTech team</p>
{% else %}
<p>Olá, {{ doc.first_name or "" }}!</p>
<p>Sua conta no Cred Hub está pronta.</p>
<p><a href="{{ url }}">Acesse o Cred Hub</a> e entre com Google, Microsoft ou GitHub usando este e-mail, ou com a sua senha.</p>
<p>Se ainda não aparecerem os dados da sua empresa, peça acesso ao administrador da sua empresa.</p>
<p>Equipe CredTech</p>
{% endif %}"""

SOCIAL_REDIRECT_URL = "/api/method/frappe.integrations.oauth2_logins.custom/auth0"
# Nome do registro (não muda depois de criado), nome no botão, conexão do Auth0, ícone. Todos voltam pela mesma
# URL (`.../custom/auth0`): o estado do login não depende do provedor. A URL é relativa, então cada empresa
# volta para o próprio endereço (coberto pelo curinga `*.credtechdigital.com.br` nos callbacks do Auth0).
SOCIAL_PROVIDERS = [
	("auth0", "Google", "google-oauth2", "google.svg"),
	("microsoft", "Microsoft", "windowslive", "office_365.svg"),
	("github", "GitHub", "github", "github.svg"),
]


def apply_from_env(commit=True):
	"""Entrada do deploy e do cadastro (bench execute; não é exposta por HTTP)."""
	env = os.environ
	email = None
	if env.get("SMTP_ADDRESS") and env.get("SMTP_PASSWORD"):
		email = {
			"server": env["SMTP_ADDRESS"],
			"port": int(env.get("SMTP_PORT") or 587),
			"login": env["SMTP_USERNAME"],
			"password": env["SMTP_PASSWORD"],
			"sender": env["MAILER_SENDER_EMAIL"],
		}
	auth0 = None
	if env.get("AUTH0_DOMAIN") and env.get("AUTH0_CLIENT_SECRET"):
		auth0 = {
			"domain": env["AUTH0_DOMAIN"],
			"client_id": env["AUTH0_CLIENT_ID"],
			"client_secret": env["AUTH0_CLIENT_SECRET"],
		}
	return apply(email=email, auth0=auth0, commit=commit)


def apply(email=None, auth0=None, commit=True):
	done = [brazil_defaults(), access_by_invite_only(), translations()]
	if email:
		done.append(email_setup(email))
	if auth0:
		done.append(social_login(auth0))
	if commit:
		frappe.db.commit()
		frappe.clear_cache()
	return done


def brazil_defaults():
	"""Assistente de configuração (idioma, país, fuso e moeda; também liga o agendador), BRL, pt-BR para
	usuários novos (o CRM usa o idioma do usuário, não o do sistema) e erros sem traceback (#73)."""
	if not frappe.is_setup_complete():
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		setup_complete(
			{"language": "Português Brasileiro", "country": "Brazil", "timezone": TIME_ZONE, "currency": CURRENCY}
		)
	make_property_setter("User", "language", "default", LANGUAGE, "Link", validate_fields_for_doctype=False)
	frappe.db.set_single_value("System Settings", "allow_error_traceback", 0)
	frappe.db.set_value("Currency", CURRENCY, "enabled", 1)
	if not frappe.db.get_value("User", "Administrator", "language"):
		frappe.db.set_value("User", "Administrator", "language", LANGUAGE)
	# Moeda do CRM (painel e valores): o CRM trava o campo depois de escolhido, então só preenche se vazio
	if not frappe.db.get_single_value("FCRM Settings", "currency"):
		frappe.db.set_single_value("FCRM Settings", "currency", CURRENCY)
	# A instalação grava o fuso padrão do Frappe (Asia/Kolkata) nos defaults globais e de Administrator/Guest
	time_zone = frappe.db.get_single_value("System Settings", "time_zone") or TIME_ZONE
	frappe.db.set_default("time_zone", time_zone)
	frappe.db.sql(
		"update `tabDefaultValue` set defvalue = %s where defkey = 'time_zone' and defvalue = 'Asia/Kolkata'",
		time_zone,
	)
	return "padrões pt-BR/BRL"


def access_by_invite_only():
	"""Decisão do dono (#27): só entra quem o admin convidou. Sem cadastro por e-mail em /login#signup."""
	frappe.db.set_single_value("Website Settings", "disable_signup", 1)
	return "acesso só por convite"


def translations():
	changed = 0
	for source, target in TRANSLATIONS.items():
		name = frappe.db.get_value("Translation", {"language": LANGUAGE, "source_text": source, "context": ""})
		doc = frappe.get_doc("Translation", name) if name else frappe.new_doc("Translation")
		if name and doc.translated_text == target:
			continue
		doc.update({"language": LANGUAGE, "source_text": source, "translated_text": target, "context": ""})
		doc.save(ignore_permissions=True)
		changed += 1
	return f"traduções: {changed} gravada(s)"


def _address(sender):
	# "Nome <email>" ou só o e-mail
	match = re.search(r"<([^>]+)>", sender)
	return (match.group(1) if match else sender).strip()


def email_setup(cfg):
	"""Conta de envio "Cred Hub" (Brevo), boas-vindas a todo usuário novo e nome "Cred Hub" nos e-mails."""
	name = frappe.db.get_value("Email Account", {"email_account_name": EMAIL_ACCOUNT})
	doc = frappe.get_doc("Email Account", name) if name else frappe.new_doc("Email Account")
	doc.update(
		{
			"email_account_name": EMAIL_ACCOUNT,
			"email_id": _address(cfg["sender"]),
			"enable_incoming": 0,
			"enable_outgoing": 1,
			"default_outgoing": 1,
			"smtp_server": cfg["server"],
			"smtp_port": cfg["port"],
			"use_tls": 1,
			"use_ssl_for_outgoing": 0,
			"auth_method": "Basic",
			"login_id_is_different": 1,
			"login_id": cfg["login"],
			"password": cfg["password"],
			"awaiting_password": 0,
			"always_use_account_email_id_as_sender": 1,
			"always_use_account_name_as_sender_name": 1,
			"send_unsubscribe_message": 0,
			"create_contact": 0,
		}
	)
	# O save testa o login no SMTP: credencial errada para aqui, com o erro
	doc.save(ignore_permissions=True)

	welcome = frappe.get_doc("Notification", WELCOME) if frappe.db.exists("Notification", WELCOME) else frappe.new_doc("Notification")
	welcome.update(
		{
			"enabled": 1,
			"channel": "Email",
			"document_type": "User",
			"event": "New",
			# Usuário criado com "Send Welcome Email" já recebe o e-mail do Frappe (com link de senha)
			"condition": 'doc.name not in ("Administrator", "Guest") and not doc.send_welcome_email',
			"subject": WELCOME_SUBJECT,
			"message_type": "HTML",
			"message": WELCOME_MESSAGE,
			"sender": EMAIL_ACCOUNT,
		}
	)
	welcome.set("recipients", [{"receiver_by_document_field": "email"}])
	if welcome.is_new():
		welcome.insert(ignore_permissions=True, set_name=WELCOME)
	else:
		welcome.save(ignore_permissions=True)
	frappe.db.set_single_value("System Settings", "app_name", "Cred Hub")
	return f"e-mail ({doc.email_id})"


def social_login(cfg):
	"""Botões Google, Microsoft e GitHub pelo Auth0 do Cred Chat (#27). Não criam usuário (sign_ups = Deny):
	quem foi convidado entra com o mesmo e-mail."""
	domain = cfg["domain"].removeprefix("https://").rstrip("/")
	for name, label, connection, icon in SOCIAL_PROVIDERS:
		exists = frappe.db.exists("Social Login Key", name)
		doc = frappe.get_doc("Social Login Key", name) if exists else frappe.new_doc("Social Login Key")
		doc.update(
			{
				"provider_name": label,
				"social_login_provider": "Custom",
				"enable_social_login": 1,
				"client_id": cfg["client_id"],
				"client_secret": cfg["client_secret"],
				"custom_base_url": 1,
				"base_url": f"https://{domain}",
				"authorize_url": "/authorize",
				"access_token_url": "/oauth/token",
				"api_endpoint": "/userinfo",
				"redirect_url": SOCIAL_REDIRECT_URL,
				"auth_url_data": json.dumps(
					{"response_type": "code", "scope": "openid profile email", "connection": connection}
				),
				"user_id_property": "sub",
				"sign_ups": "Deny",
				"icon": f"/assets/frappe/icons/social/{icon}",
			}
		)
		if exists:
			doc.save(ignore_permissions=True)
		else:
			doc.insert(ignore_permissions=True, set_name=name)
	return "login Google/Microsoft/GitHub"
