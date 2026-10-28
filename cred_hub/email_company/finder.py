"""Descobrir a empresa (CNPJ) pelo domínio do e-mail do lead (épico credtech#183).

Cascata, que para no primeiro resultado:
1. (credtech#184) domínio do e-mail sem os pessoais; domínio final pelo redirecionamento do site; Organização que já
   existe na empresa com o site nesse domínio.
2. (credtech#185) RDAP do Registro.br (só .br): CNPJ do titular pessoa jurídica; dígitos conferidos; Organização com
   esse CNPJ, se já existir; senão confirmação na consulta de CNPJ do cred_hub e confiança alta/média/baixa.
3. (credtech#186) Leitura leve do site (até 3 páginas: JSON-LD e CNPJ escrito no texto), também para domínios
   fora do .br, conferida na Receita como o RDAP.
O resultado é aplicado no card (Fase 4, credtech#187: cred_hub.email_company.apply).

Roda em fila depois que o lead entra: nunca atrasa nem derruba a entrada do lead.
"""

import frappe
import requests
from frappe.utils import now_datetime

from cred_hub.cnpj import client as cnpj_client
from cred_hub.cnpj.mapping import is_valid_cnpj
from cred_hub.email_company import rdap, safe_http, score, site
from cred_hub.email_company.domain import email_domain, url_domain

CACHE_DAYS = 30
# Site fora do ar ou recusado: tenta de novo no dia seguinte, não daqui a 30 dias
FAILURE_CACHE_HOURS = 24
CACHE_PREFIX = "credhub_email_company:"


def enabled() -> bool:
	# Interruptor em Configurações › Origens, ligado por padrão (credtech#187)
	from cred_hub.email_company import apply

	return apply.enabled()


def on_lead_insert(doc, method=None):
	"""doc_event CRM Lead.after_insert: site, Google Ads, Meta, webhook e lead criado à mão."""
	if not enabled() or frappe.flags.in_import or not email_domain(doc.get("email")):
		return
	frappe.enqueue(
		"cred_hub.email_company.finder.run",
		queue="default",
		job_id=f"cred_hub_email_company::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		lead=doc.name,
	)


def run(lead: str) -> dict | None:
	"""Job: descobre a empresa do lead. Erros ficam no log, nunca no lead."""
	try:
		from cred_hub.email_company import apply

		values = frappe.db.get_value("CRM Lead", lead, ["email", "organization"], as_dict=True)
		if not values:
			return None
		result = discover(values.email, company_name=values.organization)
		apply.apply(lead, result)
		return result
	except Exception:
		frappe.log_error(title=f"Cred Hub: descobrir empresa do lead {lead}")
		return None


def discover(email: str, company_name: str | None = None) -> dict | None:
	"""Cascata que para no primeiro resultado confiável. None quando o e-mail é pessoal ou inválido."""
	domain = email_domain(email)
	if not domain:
		return None
	site = site_info(domain)
	domains = {domain, site.get("final_domain")} - {None}
	result = {"domain": domain, "final_domain": site.get("final_domain"), "company_name": company_name}
	organization = find_organization(domains)
	if organization:
		return {**result, "organization": organization, "confidence": score.HIGH, "source": "organization"}
	found = from_rdap(domain, site.get("final_domain"), company_name) or from_site(
		domain, site.get("final_domain"), company_name
	)
	if found:
		return {**result, **found}
	return {**result, "organization": None, "confidence": None, "source": None}


def from_site(domain: str, final_domain: str | None, company_name: str | None) -> dict | None:
	"""CNPJ publicado no site da empresa (credtech#186), conferido na Receita como o do RDAP."""
	site_domain = final_domain or domain
	found = site_cnpj(site_domain)
	if not found:
		return None
	cnpj = found["cnpj"]
	result = {"cnpj": cnpj, "source": "site", "site_url": found.get("url"), "site_detail": found.get("site_detail")}
	organization = find_organization(set(), cnpj)
	if organization:
		return {**result, "organization": organization, "confidence": score.HIGH}
	company = receita(cnpj)
	if not company:
		return None
	return {**result, "organization": None, "company": company, "confidence": score.confidence(company, site_domain, company_name)}


def site_cnpj(domain: str) -> dict | None:
	"""CNPJ do site, com cache de 30 dias; sem CNPJ ou fora do ar, tenta de novo no dia seguinte."""
	key = f"{CACHE_PREFIX}site_cnpj:{domain}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached or None
	found = site.find_cnpj(domain)
	frappe.cache.set_value(key, found or {}, expires_in_sec=(CACHE_DAYS * 86400) if found else FAILURE_CACHE_HOURS * 3600)
	return found


def from_rdap(domain: str, final_domain: str | None, company_name: str | None) -> dict | None:
	"""CNPJ do titular do domínio .br, conferido na Receita. O domínio final do site vem primeiro."""
	for candidate in dict.fromkeys(d for d in (final_domain, domain) if rdap.is_br(d)):
		holder = rdap_holder(candidate)
		if not holder or not is_valid_cnpj(holder["cnpj"]):
			continue
		cnpj = holder["cnpj"]
		found = {"cnpj": cnpj, "source": "rdap", "rdap_domain": candidate}
		organization = find_organization(set(), cnpj)
		if organization:
			return {**found, "organization": organization, "confidence": score.HIGH}
		company = receita(cnpj)
		if not company:
			continue
		return {
			**found,
			"organization": None,
			"company": company,
			"confidence": score.confidence(company, candidate, company_name),
		}
	return None


def rdap_holder(domain: str) -> dict | None:
	"""Titular do domínio, com cache de 30 dias (também quando não há titular PJ). Fora do ar: sem cache."""
	key = f"{CACHE_PREFIX}rdap:{domain}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached or None
	try:
		holder = rdap.lookup(domain)
	except rdap.RdapUnavailable:
		return None
	frappe.cache.set_value(key, holder or {}, expires_in_sec=CACHE_DAYS * 86400)
	return holder


def receita(cnpj: str) -> dict | None:
	"""Razão social, fantasia e situação pela consulta de CNPJ que o cred_hub já usa (cache de 30 dias).
	Só o necessário para a confiança: sócios e demais dados ficam para o enriquecimento da Organização."""
	key = f"{CACHE_PREFIX}cnpj:{cnpj}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached or None
	try:
		data = cnpj_client.fetch(cnpj)
	except cnpj_client.CnpjNotFound:
		data = None
	except cnpj_client.CnpjUnavailable:
		return None
	company = (
		{k: data.get(k) or "" for k in ("legal_name", "trade_name", "registration_status", "city", "state")}
		if data
		else {}
	)
	frappe.cache.set_value(key, company, expires_in_sec=CACHE_DAYS * 86400)
	return company or None


def site_info(domain: str) -> dict:
	"""Domínio final do site da empresa (ex.: acme.com -> acme.com.br), com cache de 30 dias por domínio."""
	key = CACHE_PREFIX + domain
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached
	info = {"domain": domain, "final_domain": None, "checked_on": str(now_datetime())}
	for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
		try:
			page = safe_http.fetch(url, max_bytes=64 * 1024)
		except (safe_http.BlockedAddress, OSError, ValueError, requests.RequestException) as e:
			# Recusado pela proteção ou fora do ar: tenta o próximo endereço
			info.setdefault("errors", []).append(f"{url}: {type(e).__name__}: {e}"[:200])
			continue
		if page.status < 400:
			info["final_domain"] = url_domain(page.url)
			break
	expires = CACHE_DAYS * 86400 if info["final_domain"] else FAILURE_CACHE_HOURS * 3600
	frappe.cache.set_value(key, info, expires_in_sec=expires)
	return info


def find_organization(domains: set[str], cnpj: str | None = None) -> str | None:
	"""Organização da empresa com o mesmo CNPJ ou com o site num destes domínios (sem duplicar)."""
	if cnpj and frappe.get_meta("CRM Organization").has_field("cnpj"):
		from cred_hub.cnpj.mapping import format_cnpj

		name = frappe.db.get_value("CRM Organization", {"cnpj": format_cnpj(cnpj)}, "name")
		if name:
			return name
	for domain in sorted(domains):
		rows = frappe.get_all(
			"CRM Organization",
			filters={"website": ("like", f"%{domain}%")},
			fields=["name", "website"],
			order_by="creation asc",
			limit=20,
		)
		for row in rows:
			if url_domain(row.website) == domain:
				return row.name
	return None
