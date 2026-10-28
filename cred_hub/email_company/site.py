"""Leitura leve do site da empresa para achar o CNPJ (credtech#186, Fase 3 do épico #183). Sem Frappe.

Até 3 páginas (a inicial e os links de privacidade e de termos do mesmo site), sem executar JavaScript, com as
proteções da Fase 1 (`safe_http`: só endereço público, prazo e tamanho máximos). Fontes, na ordem:
1. JSON-LD (`legalName` + `taxID`/`vatID`) que o próprio site publica;
2. CNPJs escritos no texto (rodapé, política de privacidade), com dígitos conferidos (módulo 11).

Com vários CNPJs válidos, a escolha prefere a matriz (filial 0001), o CNPJ escrito perto de "controladora",
"sede", "matriz" ou "razão social" e o que mais se repete.
"""

import html
import json
import re
from urllib.parse import urljoin, urlsplit

import requests

from cred_hub.cnpj.mapping import is_valid_cnpj, only_digits
from cred_hub.email_company import safe_http
from cred_hub.email_company.domain import url_domain

MAX_PAGES = 3
PAGE_BYTES = 256 * 1024
CNPJ_PATTERN = re.compile(r"(?<!\d)(\d{2}\.?\d{3}\.?\d{3}\s*/?\s*\d{4}\s*-?\s*\d{2})(?!\d)")
JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
POLICY_WORDS = re.compile(r"privacidade|privacy|termos|terms|lgpd|pol[ií]tica", re.I)
ANCHOR_WORDS = re.compile(r"controladora|sede|matriz|raz[aã]o\s+social|cnpj", re.I)
ANCHOR_WINDOW = 120


def _plain(page_html: str) -> str:
	text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page_html, flags=re.I | re.S)
	text = re.sub(r"<[^>]+>", " ", text)
	return re.sub(r"\s+", " ", html.unescape(text))


def _fetch(url: str):
	try:
		page = safe_http.fetch(url, max_bytes=PAGE_BYTES)
	except (safe_http.BlockedAddress, OSError, ValueError, requests.RequestException):
		return None
	return page if page.status < 400 else None


def policy_links(page_html: str, base_url: str) -> list[str]:
	"""Links de privacidade e termos que ficam no mesmo site."""
	host = url_domain(base_url)
	found = []
	for href, label in LINK.findall(page_html):
		if not (POLICY_WORDS.search(href) or POLICY_WORDS.search(_plain(label))):
			continue
		url = urljoin(base_url, html.unescape(href.strip()))
		if urlsplit(url).scheme in ("http", "https") and url_domain(url) == host and url not in found:
			found.append(url)
	return found


def json_ld_companies(page_html: str) -> list[dict]:
	"""Organizações declaradas em JSON-LD com CNPJ válido: [{"cnpj", "legal_name"}]."""
	companies = []

	def walk(node):
		if isinstance(node, list):
			for item in node:
				walk(item)
		elif isinstance(node, dict):
			tax = only_digits(node.get("taxID") or node.get("vatID") or "")
			if len(tax) == 14 and is_valid_cnpj(tax):
				companies.append({"cnpj": tax, "legal_name": node.get("legalName") or node.get("name") or ""})
			for value in node.values():
				if isinstance(value, dict | list):
					walk(value)

	for block in JSON_LD.findall(page_html):
		try:
			walk(json.loads(html.unescape(block.strip())))
		except ValueError:
			continue
	return companies


def text_cnpjs(text: str) -> list[dict]:
	"""CNPJs válidos escritos no texto, cada um com a contagem e se aparece perto de uma palavra-chave."""
	seen = {}
	for match in CNPJ_PATTERN.finditer(text):
		cnpj = only_digits(match.group(1))
		if len(cnpj) != 14 or not is_valid_cnpj(cnpj):
			continue
		near = text[max(0, match.start() - ANCHOR_WINDOW) : match.end() + ANCHOR_WINDOW]
		entry = seen.setdefault(cnpj, {"cnpj": cnpj, "count": 0, "anchored": False})
		entry["count"] += 1
		entry["anchored"] = entry["anchored"] or bool(ANCHOR_WORDS.search(near))
	return list(seen.values())


def best_cnpj(candidates: list[dict]) -> str | None:
	"""Matriz (0001) primeiro, depois perto de "controladora/sede/matriz/razão social", depois o que mais aparece."""
	if not candidates:
		return None
	ranked = sorted(
		candidates,
		key=lambda c: (c["cnpj"][8:12] == "0001", c.get("anchored", False), c.get("count", 0)),
		reverse=True,
	)
	return ranked[0]["cnpj"]


def find_cnpj(domain: str) -> dict | None:
	"""Lê até 3 páginas do site e devolve {"cnpj", "source": "site", "site_detail", "url"} ou None."""
	home = None
	for url in (f"https://{domain}/", f"https://www.{domain}/"):
		home = _fetch(url)
		if home:
			break
	if not home:
		return None
	pages = [home]
	for url in policy_links(home.text, home.url)[: MAX_PAGES - 1]:
		page = _fetch(url)
		if page:
			pages.append(page)

	for page in pages:
		companies = json_ld_companies(page.text)
		if companies:
			return {"cnpj": companies[0]["cnpj"], "source": "site", "site_detail": "json-ld", "url": page.url}

	merged = {}
	for page in pages:
		for entry in text_cnpjs(_plain(page.text)):
			total = merged.setdefault(entry["cnpj"], {"cnpj": entry["cnpj"], "count": 0, "anchored": False})
			total["count"] += entry["count"]
			total["anchored"] = total["anchored"] or entry["anchored"]
	cnpj = best_cnpj(list(merged.values()))
	if not cnpj:
		return None
	url = next((p.url for p in pages if cnpj in only_digits(_plain(p.text))), home.url)
	return {"cnpj": cnpj, "source": "site", "site_detail": "texto", "url": url}
