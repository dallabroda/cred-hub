"""Domínio da empresa a partir do e-mail do lead (credtech#183). Sem Frappe."""

from urllib.parse import urlsplit

from cred_hub.company.discover import registered_domain

# E-mails pessoais e de provedores: o domínio não diz nada sobre a empresa do lead
PERSONAL_DOMAINS = frozenset(
	{
		"gmail.com", "googlemail.com", "outlook.com", "outlook.com.br", "hotmail.com", "hotmail.com.br",
		"live.com", "live.com.br", "msn.com", "yahoo.com", "yahoo.com.br", "ymail.com", "rocketmail.com",
		"icloud.com", "me.com", "mac.com", "aol.com", "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br",
		"globo.com", "globomail.com", "r7.com", "zipmail.com.br", "oi.com.br", "click21.com.br", "superig.com.br",
		"protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
		"tutanota.com", "hey.com", "fastmail.com", "qq.com", "163.com", "126.com", "naver.com",
		"example.com", "example.org", "example.net", "test.com",
	}
)


def email_domain(email: str | None) -> str | None:
	"""Domínio registrável do e-mail (`ana@vendas.acme.com.br` -> `acme.com.br`), ou None se for pessoal ou inválido."""
	email = (email or "").strip().lower()
	local, sep, host = email.rpartition("@")
	if not sep or not local or not host:
		return None
	host = host.strip(".")
	try:
		host = host.encode("idna").decode("ascii")
	except UnicodeError:
		return None
	if "." not in host or any(not part for part in host.split(".")):
		return None
	domain = registered_domain(host)
	if domain in PERSONAL_DOMAINS or host in PERSONAL_DOMAINS:
		return None
	return domain


def url_domain(url: str | None) -> str | None:
	"""Domínio registrável de um site (`https://www.acme.com.br/contato` -> `acme.com.br`)."""
	url = (url or "").strip()
	if not url:
		return None
	host = urlsplit(url if "//" in url else f"//{url}").hostname
	return registered_domain(host) if host and "." in host else None
