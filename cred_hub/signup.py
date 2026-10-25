"""Cadastro self-service de empresas (credtech#25).

Fluxo (decisões do dono, 25/10/2026): qualquer pessoa cria a própria empresa, sem aprovação, mas só depois de
confirmar o e-mail. Cada empresa ganha um site próprio, `<endereço>.credtechdigital.com.br`, com o responsável
como administrador. O cadastro nunca entra numa empresa existente (acesso a uma empresa é só por convite).

API pública (site central, `hub.credtechdigital.com.br`, com `credhub_signup: 1` no site_config):
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
PROVISION_QUEUE = "credhub_provision"
# Endereços da própria plataforma, de infraestrutura ou que confundem o cliente
RESERVED = frozenset(
	"""
	www app apps api apis chat hub bridge embed site sites mail email smtp imap pop pop3 webmail mx ns ns1 ns2
	dns ftp sftp ssh vpn cdn static assets media files img images admin administrator root sysadmin login
	logout signup cadastro conta contas account accounts auth oauth sso id portal painel dashboard status
	docs doc help ajuda suporte support blog news billing pagamento pagamentos cobranca financeiro
	credtech credtechdigital cred credhub credchat leticia test teste testes dev staging homolog demo
	autodiscover autoconfig localhost
	""".split()
)
SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$")
LEGAL_SUFFIXES = ("ltda", "me", "mei", "epp", "eireli", "sa", "ss", "slu")
OPEN_STATUSES = ("Pending Confirmation", "Confirmed", "Provisioning", "Active")


def _require_central_site():
	if not frappe.conf.get("credhub_signup"):
		frappe.throw(_("Company signup is not available here."), frappe.PermissionError)


def _domain():
	return frappe.conf.get("credhub_signup_domain") or DEFAULT_DOMAIN


def _site_name(subdomain):
	return f"{subdomain}.{_domain()}"


def _site_exists(site_name):
	return os.path.isdir(os.path.join(get_bench_path(), "sites", site_name))


def _hash(token):
	return hashlib.sha256(token.encode()).hexdigest()


def _expire_old_links():
	frappe.db.set_value(
		"Cred Hub Signup",
		{"status": "Pending Confirmation", "expires_on": ("<", now_datetime())},
		"status",
		"Expired",
		update_modified=False,
	)


def availability(subdomain, exclude=None):
	"""(disponível, motivo): motivo é "invalid", "reserved" ou "taken"."""
	subdomain = (subdomain or "").strip().lower()
	if not SUBDOMAIN_RE.match(subdomain) or "--" in subdomain:
		return False, "invalid"
	if subdomain in RESERVED:
		return False, "reserved"
	_expire_old_links()
	filters = {"subdomain": subdomain, "status": ("in", OPEN_STATUSES)}
	if exclude:
		filters["name"] = ("!=", exclude)
	if _site_exists(_site_name(subdomain)) or frappe.db.exists("Cred Hub Signup", filters):
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
@rate_limit(limit=5, seconds=60 * 60)
def request_signup(company_name: str, subdomain: str, full_name: str, email: str):
	"""Registra o pedido e manda o link de confirmação. A empresa só é criada depois do clique (dono)."""
	_require_central_site()
	company_name = (company_name or "").strip()
	full_name = (full_name or "").strip()
	email = (email or "").strip().lower()
	subdomain = (subdomain or "").strip().lower()
	if not company_name or not full_name:
		frappe.throw(_("Fill in the company name and your name."), frappe.MandatoryError)
	if not frappe.utils.validate_email_address(email):
		frappe.throw(_("Enter a valid e-mail address."), frappe.InvalidEmailAddressError)
	available, reason = availability(subdomain)
	if not available:
		frappe.throw(_reason_message(reason), frappe.ValidationError)
	recent = frappe.db.count(
		"Cred Hub Signup", {"email": email, "creation": (">", add_to_date(now_datetime(), hours=-24))}
	)
	if recent >= 3:
		frappe.throw(_("Too many signups with this e-mail today. Please try again tomorrow."), frappe.RateLimitExceededError)

	token = secrets.token_urlsafe(32)
	doc = frappe.get_doc(
		{
			"doctype": "Cred Hub Signup",
			"company_name": company_name[:140],
			"subdomain": subdomain,
			"full_name": full_name[:140],
			"email": email,
			"status": "Pending Confirmation",
			"token_hash": _hash(token),
			"expires_on": add_to_date(now_datetime(), hours=LINK_HOURS),
			"request_ip": frappe.local.request_ip,
		}
	).insert(ignore_permissions=True)
	_send_confirmation(doc, token)
	return {"ok": True}


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


def _bench(args, env=None, timeout=900):
	result = subprocess.run(
		["bench", *args],
		cwd=get_bench_path(),
		env={**os.environ, **(env or {})},
		capture_output=True,
		text=True,
		timeout=timeout,
	)
	if result.returncode != 0:
		raise RuntimeError(f"bench {args[0]} {args[1] if len(args) > 1 else ''} failed: {result.stderr[-2000:]}")
	return result.stdout


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
	doc.db_set({"status": "Provisioning", "site_name": site})
	frappe.db.commit()
	try:
		db_user = os.environ["CREDHUB_PROVISIONER_DB_USER"]
		db_password = os.environ["CREDHUB_PROVISIONER_DB_PASSWORD"]
		_bench(
			[
				"new-site",
				site,
				"--mariadb-user-host-login-scope=%",
				"--db-root-username",
				db_user,
				"--db-root-password",
				db_password,
				# Administrator não é usado no dia a dia (o responsável é o administrador); a CredTech troca a
				# senha com bench set-admin-password se precisar dar suporte
				"--admin-password",
				secrets.token_urlsafe(24),
				"--install-app",
				"crm",
				"--install-app",
				"cred_hub",
			],
			timeout=1500,
		)
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
		return
	doc.db_set({"status": "Active", "active_on": now_datetime()})
	frappe.db.commit()


def create_owner(email, full_name):
	"""Roda no site novo: o responsável vira o administrador e recebe o e-mail para criar a senha (ou entra
	pelo Google/Microsoft/GitHub com o mesmo e-mail)."""
	first, _sep, last = (full_name or "").strip().partition(" ")
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first or email,
			"last_name": last,
			"language": "pt-BR",
			"send_welcome_email": 1,
			"roles": [{"role": role} for role in ("System Manager", "Sales Manager", "Sales User")],
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return user.name
