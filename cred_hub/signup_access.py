"""Caminho para o cadastro de quem chega sem conta (credtech#25).

Entrar numa empresa é só por convite do administrador dela. Quem não tem conta é levado ao cadastro
do site central (hub), para criar a própria empresa:

- login com Google/Microsoft/GitHub de um e-mail sem conta (hook `oauth_without_account` do frappe):
  no hub, abre /cadastro com o e-mail e o nome já preenchidos; no site de uma empresa, abre o cadastro
  do hub avisando que, para entrar naquela empresa, é preciso convite;
- tela de login do hub (hook `login_signup_url`): link "Criar minha empresa".
"""

import frappe

from cred_hub.signup import _domain, signup_open

PREFILL_KEY = "credhub_signup_prefill:{}"
PREFILL_SECONDS = 30 * 60


def login_signup_url():
	if signup_open():
		return "/cadastro"
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
