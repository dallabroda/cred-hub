"""Caminho para o cadastro de quem chega sem conta (credtech#25).

Entrar numa empresa é só por convite do administrador dela. Quem não tem conta é levado ao cadastro
do site central (hub), para criar a própria empresa:

- login com Google/Microsoft/GitHub de um e-mail sem conta (hook `oauth_without_account` do frappe):
  no hub, abre /cadastro com o e-mail e o nome já preenchidos; no site de uma empresa, abre o cadastro
  do hub avisando que, para entrar naquela empresa, é preciso convite;
- tela de login do hub (hook `login_signup_url`): link "Cadastre-se", para o cadastro do site (credtech#280);
- "Teste grátis" do site (credtech#278): `social_signup` abre o login social do hub direto no provedor e
  `start_signup` guarda o e-mail digitado no site e devolve o endereço do /cadastro já preenchido.
"""

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import validate_email_address
from frappe.utils.oauth import get_oauth2_authorize_url

from cred_hub.signup import _domain, signup_open

PREFILL_KEY = "credhub_signup_prefill:{}"
PREFILL_SECONDS = 30 * 60
# Botões do site → Social Login Key do hub (todas pelo Auth0; a "auth0" é a do Google)
SOCIAL_KEYS = {"google": "auth0", "microsoft": "microsoft", "github": "github"}
# Site institucional que chama o start_signup e lê a resposta (CORS)
SITE_ORIGINS = ("https://credtechdigital.com.br", "https://www.credtechdigital.com.br")


def login_signup_url():
	# "Não tem uma conta? Cadastre-se" do login leva ao cadastro do site, já no HubTech (Figma, credtech#280)
	if signup_open():
		return f"https://{_domain()}/signup/?produto=hubtech"
	return None


def oauth_without_account(email, data):
	# Cadastro fechado: o frappe mostra "Conta não encontrada, peça um convite ao administrador"
	if not frappe.conf.get("credhub_signup_open"):
		return None
	if not frappe.conf.get("credhub_signup"):
		return f"https://hub.{_domain()}/cadastro?sem_conta=1"

	# O e-mail fica no servidor, não no endereço da página
	key = frappe.generate_hash(length=24)
	frappe.cache.set_value(
		PREFILL_KEY.format(key),
		{"email": email, "full_name": _full_name(data)},
		expires_in_sec=PREFILL_SECONDS,
	)
	return f"/cadastro?de={key}"


@frappe.whitelist(allow_guest=True, methods=["GET"])
def social_signup(provider=None):
	"""Google/Microsoft/GitHub do site. O link do provedor precisa de um `state` de uso único, criado na hora
	(o mesmo que a tela de login cria); por isso o site aponta para cá e não direto para o Auth0. Quem não tem
	conta volta pelo `oauth_without_account` para o /cadastro preenchido; quem tem, entra."""
	key = SOCIAL_KEYS.get(provider)
	if not signup_open() or not key or not frappe.db.get_value("Social Login Key", key, "enable_social_login"):
		return _redirect("/login")
	return _redirect(get_oauth2_authorize_url(key, None))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=60 * 60)
def start_signup(email=None):
	"""E-mail digitado no "Teste grátis" do site: devolve o endereço do /cadastro com ele preenchido, sem pôr o e-mail
	no endereço. O site chama por fetch sem cookies (assim quem já está logado no hub não cai na checagem de CSRF)
	e vai para o `location` devolvido."""
	origin = frappe.get_request_header("Origin")
	if origin in SITE_ORIGINS:
		frappe.local.response_headers.set("Access-Control-Allow-Origin", origin)
	if not signup_open():
		return {"location": "/login"}
	email = (email or "").strip().lower()[:140]
	if not validate_email_address(email):
		return {"location": "/cadastro"}
	key = frappe.generate_hash(length=24)
	frappe.cache.set_value(
		PREFILL_KEY.format(key), {"email": email, "from_site": 1}, expires_in_sec=PREFILL_SECONDS
	)
	return {"location": f"/cadastro?de={key}"}


def _redirect(location):
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location


def get_prefill(key):
	if not key or not key.isalnum():
		return {}
	return frappe.cache.get_value(PREFILL_KEY.format(key)) or {}


def _full_name(data):
	name = data.get("name") or " ".join(
		part
		for part in (
			data.get("given_name") or data.get("first_name"),
			data.get("family_name") or data.get("last_name"),
		)
		if part
	)
	return (name or "").strip()[:140]
