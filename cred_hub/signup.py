"""Cadastro self-service de empresas (credtech#25).

Fluxo (decisões do dono, 25/10/2026): qualquer pessoa cria a própria empresa, sem aprovação, mas só depois de
confirmar o e-mail. Cada empresa ganha um site próprio, `<endereço>.credtechdigital.com.br`, com o responsável
como administrador. O cadastro nunca entra numa empresa existente (acesso a uma empresa é só por convite).

API pública (site central, `hub.credtechdigital.com.br`, com `credhub_signup: 1` no site_config e `credhub_signup_open: 1`
no common_site_config):
- check_subdomain(subdomain) -> {"available", "reason", "address"}
- suggest_subdomain(company_name) -> {"subdomain", "available", "reason", "address"}
- request_signup(company_name, subdomain, full_name, email) -> {"ok": True}; manda o link de confirmação
- confirm_signup(token) -> {"status", "address"}; põe a criação da empresa na fila
- signup_status(token) -> {"status", "address"}

A criação (provision) roda no worker `credhub_provision`, o único com o usuário de banco que cria bancos
(CREDHUB_PROVISIONER_DB_USER/PASSWORD); os segredos de e-mail e login vão para a empresa nova por variável de
ambiente, lidos do próprio site central.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import unicodedata

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, get_bench_path, get_url, now_datetime

DEFAULT_DOMAIN = "credtechdigital.com.br"
LINK_HOURS = 24
# Reenvios do link por pedido: sem limite, alguém poderia lotar a caixa de e-mail de outra pessoa
MAX_LINKS = 5
PROVISION_QUEUE = "credhub_provision"
DB_PREFIX = "chsite"
# Versão dos Termos de uso aceita no cadastro (data da Parte C, Cred Hub, em credtechdigital.com.br/termos)
TERMS_VERSION = "2026-10-25"
# Endereços da própria plataforma, de infraestrutura ou que confundem o cliente
RESERVED = frozenset(
	"""
	www app apps api apis chat hub bridge embed site sites mail email smtp imap pop pop3 webmail mx ns ns1 ns2
	dns ftp sftp ssh vpn cdn static assets media files img images admin administrator root sysadmin login
	logout signup cadastro conta contas account accounts auth oauth sso id portal painel dashboard status
	docs doc help ajuda suporte support blog news billing pagamento pagamentos cobranca financeiro
	credtech credtechdigital cred credhub credchat leticia test teste testes dev staging homolog demo
	autodiscover autoconfig localhost empresa exemplo suaempresa sua-empresa minhaempresa
	""".split()
)
SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$")
LEGAL_SUFFIXES = ("ltda", "me", "mei", "epp", "eireli", "sa", "ss", "slu")
# Failed também segura o endereço: a CredTech tenta de novo (retry_provision) ou apaga o cadastro
OPEN_STATUSES = ("Pending Confirmation", "Confirmed", "Provisioning", "Active", "Failed")


class AlreadyHasCompanyError(frappe.ValidationError):
	"""O e-mail já criou uma empresa (exc_type na resposta da API, para a tela)."""


def signup_open():
	"""Cadastro aberto: este é o site central (`credhub_signup` no site_config) e a CredTech abriu o cadastro para
	todos (`credhub_signup_open` no common_site_config; decisão do dono, 26/10/2026: fechado até a cobrança de
	usuários extras ficar pronta)."""
	return bool(frappe.conf.get("credhub_signup") and frappe.conf.get("credhub_signup_open"))


def _require_central_site():
	if not signup_open():
		frappe.throw(_("Company signup is not available here."), frappe.PermissionError)


def _domain():
	return frappe.conf.get("credhub_signup_domain") or DEFAULT_DOMAIN


def _site_name(subdomain):
	return f"{subdomain}.{_domain()}"


def _site_exists(site_name):
	return os.path.isdir(os.path.join(get_bench_path(), "sites", site_name))


def _hash(token):
	return hashlib.sha256(token.encode()).hexdigest()


def availability(subdomain, exclude=None):
	"""(disponível, motivo): motivo é "invalid", "reserved" ou "taken"."""
	subdomain = (subdomain or "").strip().lower()
	if not SUBDOMAIN_RE.match(subdomain) or "--" in subdomain:
		return False, "invalid"
	if subdomain in RESERVED:
		return False, "reserved"
	if _site_exists(_site_name(subdomain)):
		return False, "taken"
	holding = frappe.get_all(
		"Cred Hub Signup",
		filters={"subdomain": subdomain, "status": ("in", OPEN_STATUSES), "name": ("!=", exclude or "")},
		fields=["status", "expires_on"],
	)
	now = now_datetime()
	# Link vencido e não confirmado não segura o endereço (o status vira Expired se alguém clicar nele)
	if any(row.status != "Pending Confirmation" or (row.expires_on and row.expires_on > now) for row in holding):
		return False, "taken"
	return True, None


def _availability_response(subdomain):
	available, reason = availability(subdomain)
	return {
		"subdomain": subdomain,
		"available": available,
		"reason": reason,
		"address": f"https://{_site_name(subdomain)}" if available else None,
	}


def _reason_message(reason):
	return {
		"invalid": _("Use 3 to 30 lowercase letters, numbers or hyphens, starting and ending with a letter or number."),
		"reserved": _("This address is reserved. Please choose another one."),
		"taken": _("This address is already in use. Please choose another one."),
	}[reason]


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60)
def check_subdomain(subdomain: str):
	_require_central_site()
	subdomain = (subdomain or "").strip().lower()
	response = _availability_response(subdomain)
	response["message"] = _reason_message(response["reason"]) if response["reason"] else None
	return response


def slugify(company_name):
	text = unicodedata.normalize("NFKD", company_name or "").encode("ascii", "ignore").decode().lower()
	words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
	while len(words) > 1:
		if words[-1] in LEGAL_SUFFIXES:
			words.pop()
		elif len(words) > 2 and words[-2:] == ["s", "a"]:  # "S/A", "S.A."
			del words[-2:]
		else:
			break
	slug = "-".join(words)[:30].strip("-")
	return slug


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60)
def suggest_subdomain(company_name: str):
	"""Sugere um endereço livre a partir do nome da empresa (a pessoa pode trocar)."""
	_require_central_site()
	base = slugify(company_name)
	if len(base) < 3:
		base = (base + "-empresa").strip("-")[:30]
	candidate = base
	for n in range(2, 50):
		if availability(candidate)[0]:
			break
		suffix = f"-{n}"
		candidate = base[: 30 - len(suffix)].strip("-") + suffix
	response = _availability_response(candidate)
	response["message"] = _reason_message(response["reason"]) if response["reason"] else None
	return response


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def request_signup(company_name: str, subdomain: str, full_name: str, email: str, accepted_terms: int = 0):
	"""Registra o pedido e manda o link de confirmação. A empresa só é criada depois do clique (dono).
	Pedido repetido (mesmo e-mail e mesmo endereço, link ainda válido) manda um link novo."""
	_require_central_site()
	company_name = (company_name or "").strip()
	full_name = (full_name or "").strip()
	email = (email or "").strip().lower()
	subdomain = (subdomain or "").strip().lower()
	if not company_name or not full_name:
		frappe.throw(_("Fill in the company name and your name."), frappe.MandatoryError)
	if not frappe.utils.validate_email_address(email):
		frappe.throw(_("Enter a valid e-mail address."), frappe.InvalidEmailAddressError)
	if not frappe.utils.cint(accepted_terms):
		frappe.throw(_("Accept the Terms of Use to create your company."), frappe.ValidationError)

	token = secrets.token_urlsafe(32)
	link = {
		"company_name": company_name[:140],
		"full_name": full_name[:140],
		"token_hash": _hash(token),
		"expires_on": add_to_date(now_datetime(), hours=LINK_HOURS),
		"request_ip": frappe.local.request_ip,
		# Prova do aceite: quando e qual versão dos Termos
		"terms_accepted_on": now_datetime(),
		"terms_version": TERMS_VERSION,
	}
	# Regra do dono (26/10/2026): cada pessoa (e-mail) cria no máximo uma empresa; funis são ilimitados.
	# A resposta não diz qual é a empresa (qualquer um poderia digitar o e-mail de outra pessoa): o endereço vai
	# por e-mail para o dono do e-mail.
	existing = frappe.db.get_value(
		"Cred Hub Signup",
		{"email": email, "status": ("in", ("Confirmed", "Provisioning", "Active"))},
		["name", "site_name", "company_name", "full_name", "subdomain"],
		as_dict=True,
	)
	if existing:
		_send_existing_company(existing, email)
		frappe.throw(
			_("This e-mail has already created a company on Cred Hub. We sent its address to this e-mail."),
			AlreadyHasCompanyError,
		)

	pending = _pending(subdomain, email)
	if pending:
		# O e-mail não chegou ou se perdeu: link novo para o mesmo pedido (o anterior deixa de valer)
		if (pending.links_sent or 1) >= MAX_LINKS:
			frappe.throw(_("Too many links sent for this signup. Please try again tomorrow."), frappe.RateLimitExceededError)
		pending.db_set({**link, "links_sent": (pending.links_sent or 1) + 1})
		_send_confirmation(pending, token)
		return {"ok": True}

	available, reason = availability(subdomain)
	if not available:
		frappe.throw(_reason_message(reason), frappe.ValidationError)
	recent = frappe.db.count(
		"Cred Hub Signup", {"email": email, "creation": (">", add_to_date(now_datetime(), hours=-24))}
	)
	if recent >= 3:
		frappe.throw(_("Too many signups with this e-mail today. Please try again tomorrow."), frappe.RateLimitExceededError)

	# Pedido anterior ainda sem confirmação, para outro endereço: o novo substitui (o link antigo deixa de valer)
	frappe.db.set_value(
		"Cred Hub Signup",
		{"email": email, "status": "Pending Confirmation", "subdomain": ("!=", subdomain)},
		"status",
		"Expired",
	)
	doc = frappe.get_doc(
		{
			"doctype": "Cred Hub Signup",
			"subdomain": subdomain,
			"email": email,
			"status": "Pending Confirmation",
			"links_sent": 1,
			**link,
		}
	).insert(ignore_permissions=True)
	_send_confirmation(doc, token)
	return {"ok": True}


def _pending(subdomain, email):
	"""Pedido do mesmo e-mail para o mesmo endereço ainda esperando a confirmação (link dentro da validade)."""
	name = frappe.db.get_value(
		"Cred Hub Signup",
		{
			"subdomain": subdomain,
			"email": email,
			"status": "Pending Confirmation",
			"expires_on": (">", now_datetime()),
		},
	)
	return frappe.get_doc("Cred Hub Signup", name) if name else None


def _send_confirmation(doc, token):
	link = get_url(f"/cadastro/confirmar?token={token}")
	frappe.sendmail(
		recipients=[doc.email],
		subject=_("Confirm your e-mail to create {0} on Cred Hub").format(doc.company_name),
		message=_(
			"<p>Hi, {0}!</p><p>To create <b>{1}</b> on Cred Hub, at <b>{2}</b>, confirm your e-mail:</p>"
			'<p><a href="{3}">Confirm and create my company</a></p>'
			"<p>The link is valid for {4} hours. If you did not ask for this, ignore this e-mail.</p>"
			"<p>CredTech team</p>"
		).format(
			frappe.utils.escape_html(doc.full_name),
			frappe.utils.escape_html(doc.company_name),
			_site_name(doc.subdomain),
			link,
			LINK_HOURS,
		),
		now=True,
	)


def _send_existing_company(existing, email):
	address = f"https://{existing.site_name or _site_name(existing.subdomain)}"
	frappe.sendmail(
		recipients=[email],
		subject=_("Your company on Cred Hub"),
		message=_(
			"<p>Hi, {0}!</p><p>This e-mail has already created <b>{1}</b> on Cred Hub. Each person creates one "
			'company; you can create as many funnels as you want inside it.</p><p><a href="{2}">Open {1}</a></p>'
			"<p>CredTech team</p>"
		).format(
			frappe.utils.escape_html(existing.full_name or ""),
			frappe.utils.escape_html(existing.company_name),
			address,
		),
		now=True,
	)


def _find(token):
	name = frappe.db.get_value("Cred Hub Signup", {"token_hash": _hash(token or "")}) if token else None
	if not name:
		frappe.throw(_("This link is invalid. Please sign up again."), frappe.DoesNotExistError)
	return frappe.get_doc("Cred Hub Signup", name)


def _status_response(doc):
	return {
		"status": doc.status,
		"company_name": doc.company_name,
		"address": f"https://{doc.site_name}" if doc.status == "Active" else None,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def confirm_signup(token: str):
	_require_central_site()
	doc = _find(token)
	if doc.status == "Pending Confirmation":
		if doc.expires_on and doc.expires_on < now_datetime():
			doc.db_set("status", "Expired")
		elif not availability(doc.subdomain, exclude=doc.name)[0]:
			# Alguém criou o mesmo endereço entre o pedido e a confirmação
			doc.db_set({"status": "Failed", "error": "subdomain taken before confirmation"})
		else:
			doc.db_set({"status": "Confirmed", "confirmed_on": now_datetime()})
			frappe.enqueue(
				"cred_hub.signup.provision",
				queue=PROVISION_QUEUE,
				timeout=1800,
				job_id=f"credhub-signup-{doc.name}",
				deduplicate=True,
				enqueue_after_commit=True,
				signup=doc.name,
			)
	return _status_response(doc)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60)
def signup_status(token: str):
	_require_central_site()
	return _status_response(_find(token))


# Criação da empresa (worker credhub_provision)


def _run(command, cwd, env=None, timeout=900):
	result = subprocess.run(
		command,
		cwd=cwd,
		env={**os.environ, **(env or {})},
		capture_output=True,
		text=True,
		timeout=timeout,
	)
	if result.returncode != 0:
		raise RuntimeError(f"{' '.join(command[:4])} failed: {result.stderr[-2000:]}")
	return result.stdout


def _bench(args, env=None, timeout=900):
	return _run(["bench", *args], get_bench_path(), env=env, timeout=timeout)


def _create_site(site):
	"""Site e banco novos (cred_hub.provision_site): senhas por ambiente, desfaz tudo se falhar."""
	bench = get_bench_path()
	# Prefixo fixo: o usuário de banco do cadastro só tem direitos em `chsite%` (o MariaDB não aceita conceder a
	# partir de um curinga com "_" escapado, que é o prefixo padrão do Frappe)
	db_name = f"{DB_PREFIX}{secrets.token_hex(8)}"
	_run(
		[os.path.join(bench, "env", "bin", "python"), "-m", "cred_hub.provision_site", site, db_name],
		os.path.join(bench, "sites"),
		timeout=1500,
	)


def _site_ready(site):
	"""Site já criado por uma tentativa anterior, com os apps instalados (retomar daí)."""
	return _site_exists(site) and "cred_hub" in _bench(["--site", site, "list-apps"])


def _site_secrets():
	"""E-mail (Brevo) e login (Auth0) do site central, passados à empresa nova por variável de ambiente."""
	from frappe.utils.password import get_decrypted_password

	env = {}
	account = frappe.db.get_value(
		"Email Account", {"email_account_name": "Cred Hub"}, ["name", "email_id", "smtp_server", "smtp_port", "login_id"], as_dict=True
	)
	if account:
		env.update(
			{
				"SMTP_ADDRESS": account.smtp_server,
				"SMTP_PORT": str(account.smtp_port or 587),
				"SMTP_USERNAME": account.login_id,
				"SMTP_PASSWORD": get_decrypted_password("Email Account", account.name, "password", raise_exception=False) or "",
				"MAILER_SENDER_EMAIL": account.email_id,
			}
		)
	key = frappe.db.get_value("Social Login Key", "auth0", ["base_url", "client_id"], as_dict=True)
	if key:
		env.update(
			{
				"AUTH0_DOMAIN": key.base_url,
				"AUTH0_CLIENT_ID": key.client_id,
				"AUTH0_CLIENT_SECRET": get_decrypted_password("Social Login Key", "auth0", "client_secret", raise_exception=False) or "",
			}
		)
	return {k: v for k, v in env.items() if v}


def provision(signup):
	doc = frappe.get_doc("Cred Hub Signup", signup)
	if doc.status != "Confirmed":
		return
	site = _site_name(doc.subdomain)
	doc.db_set({"status": "Provisioning", "site_name": site, "error": None})
	frappe.db.commit()
	try:
		# Nova tentativa depois de uma falha: o site pode já existir (falhou depois de criado)
		if not _site_ready(site):
			if _site_exists(site):
				raise RuntimeError(f"{site} exists but is incomplete: remove it (bench drop-site) before retrying")
			_create_site(site)
		# Links dos e-mails (boas-vindas, senha) com o endereço da empresa, também fora de requisição
		_bench(["--site", site, "set-config", "host_name", f"https://{site}"])
		_bench(["--site", site, "execute", "cred_hub.site_setup.apply_from_env"], env=_site_secrets())
		_bench(
			[
				"--site",
				site,
				"execute",
				"cred_hub.signup.create_owner",
				"--kwargs",
				json.dumps({"email": doc.email, "full_name": doc.full_name}),
			]
		)
	except Exception as e:
		frappe.db.rollback()
		doc.db_set({"status": "Failed", "error": str(e)[-4000:]})
		frappe.db.commit()
		frappe.log_error(title=f"Cred Hub signup failed: {site}")
		_alert_team(doc)
		return
	doc.db_set({"status": "Active", "active_on": now_datetime()})
	frappe.db.commit()


def _alert_team(doc):
	"""Avisa a equipe CredTech (System Managers do site central) que um cadastro falhou."""
	recipients = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ("not in", ("Administrator", "Guest"))},
		pluck="name",
	)
	recipients = [user for user in recipients if "System Manager" in frappe.get_roles(user)]
	if not recipients:
		return
	frappe.sendmail(
		recipients=recipients,
		subject=f"Cred Hub: cadastro de {doc.company_name} falhou",
		message=(
			f"<p>O cadastro de <b>{frappe.utils.escape_html(doc.company_name)}</b> ({doc.site_name}, "
			f"{frappe.utils.escape_html(doc.email)}) falhou.</p>"
			f"<p>Veja o erro em <a href=\"{get_url(f'/app/cred-hub-signup/{doc.name}')}\">Cred Hub Signup</a> "
			"e tente de novo com retry_provision.</p>"
		),
	)


@frappe.whitelist(methods=["POST"])
def retry_provision(signup: str):
	"""Tenta de novo um cadastro que falhou (System Manager do site central). Retoma de onde parou."""
	_require_central_site()
	frappe.only_for("System Manager")
	doc = frappe.get_doc("Cred Hub Signup", signup)
	if doc.status != "Failed":
		frappe.throw(_("Only failed signups can be retried."))
	doc.db_set({"status": "Confirmed", "error": None})
	frappe.enqueue(
		"cred_hub.signup.provision",
		queue=PROVISION_QUEUE,
		timeout=1800,
		job_id=f"credhub-signup-{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		signup=doc.name,
	)
	return {"status": "Confirmed"}


def create_owner(email, full_name):
	"""Roda no site novo: o responsável vira o administrador e recebe o e-mail para criar a senha (ou entra
	pelo Google/Microsoft/GitHub com o mesmo e-mail). Pode repetir (nova tentativa do cadastro)."""
	roles = ("System Manager", "Sales Manager", "Sales User")
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
		user.enabled = 1
		user.add_roles(*roles)
	else:
		first, _sep, last = (full_name or "").strip().partition(" ")
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first or email,
				"last_name": last,
				"language": "pt-BR",
				"send_welcome_email": 1,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return user.name
