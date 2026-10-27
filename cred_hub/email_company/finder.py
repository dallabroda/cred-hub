"""Descobrir a empresa (CNPJ) pelo domínio do e-mail do lead (épico credtech#183).

Fase 1 (credtech#184): saneamento do domínio, domínio final pelo redirecionamento do site, atalho por Organização que
já existe na empresa, fila assíncrona e cache. As fontes de CNPJ (RDAP e site, Fases 2 e 3) e a aplicação no card
(Fase 4) entram nesta mesma cascata; até lá o resultado só é calculado e guardado no cache.

Roda em fila depois que o lead entra: nunca atrasa nem derruba a entrada do lead.
"""

import frappe
import requests
from frappe.utils import cint, now_datetime

from cred_hub.email_company import safe_http
from cred_hub.email_company.domain import email_domain, url_domain

CACHE_DAYS = 30
CACHE_PREFIX = "credhub_email_company:"


def enabled() -> bool:
	# Interruptor provisório por site; a Fase 4 leva para Configurações (ligado por padrão)
	return bool(cint(frappe.conf.get("credhub_email_company", 1)))


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
		values = frappe.db.get_value("CRM Lead", lead, ["email", "organization"], as_dict=True)
		if not values:
			return None
		return discover(values.email, company_name=values.organization)
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
		return {**result, "organization": organization, "confidence": "high", "source": "organization"}
	return {**result, "organization": None, "confidence": None, "source": None}


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
	frappe.cache.set_value(key, info, expires_in_sec=CACHE_DAYS * 86400)
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
