"""Titular de um domínio .br pelo RDAP do Registro.br (credtech#185). Sem Frappe.

Verificado em 27/10/2026: para titular pessoa jurídica, a entidade `registrant` traz a razão social em
`vcardArray` (fn) e o CNPJ em `publicIds` ({type: "cnpj"}), sem máscara. Titular pessoa física vem com CPF: é
descartado. O nome do representante legal (`legalRepresentative`) é dado pessoal e não é lido.

O host é fixo (rdap.registro.br) e o domínio só entra no caminho, depois de validado.
"""

import re

import requests

from cred_hub.cnpj.mapping import only_digits
from cred_hub.email_company.safe_http import USER_AGENT

URL = "https://rdap.registro.br/domain/{domain}"
TIMEOUT = (3, 6)
DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+br$")


class RdapUnavailable(Exception):
	"""Registro.br fora do ar, limite de consultas ou resposta fora do formato: tentar de novo depois."""


def is_br(domain: str | None) -> bool:
	return bool(domain and DOMAIN_RE.match(domain))


def lookup(domain: str, session=None) -> dict | None:
	"""{"cnpj", "holder"} do titular pessoa jurídica; None se o domínio não existe, não é .br ou é de pessoa física."""
	if not is_br(domain):
		return None
	http = session or requests
	try:
		response = http.get(
			URL.format(domain=domain),
			timeout=TIMEOUT,
			headers={"Accept": "application/rdap+json", "User-Agent": USER_AGENT},
			allow_redirects=False,
		)
	except requests.RequestException as e:
		raise RdapUnavailable(type(e).__name__) from None
	if response.status_code == 404:
		return None
	if response.status_code != 200:
		raise RdapUnavailable(f"HTTP {response.status_code}")
	try:
		return parse(response.json())
	except (ValueError, TypeError, AttributeError) as e:
		raise RdapUnavailable(f"unexpected answer: {e}") from None


def parse(data: dict) -> dict | None:
	for entity in data.get("entities") or []:
		if "registrant" not in (entity.get("roles") or []):
			continue
		ids = {str(p.get("type") or "").lower(): p.get("identifier") for p in entity.get("publicIds") or []}
		cnpj = only_digits(ids.get("cnpj"))
		if len(cnpj) != 14:
			return None  # CPF ou sem documento: o domínio não diz qual é a empresa
		return {"cnpj": cnpj, "holder": _vcard_name(entity)}
	return None


def _vcard_name(entity: dict) -> str:
	vcard = entity.get("vcardArray") or []
	for item in vcard[1] if len(vcard) > 1 else []:
		if isinstance(item, list) and len(item) > 3 and item[0] == "fn":
			return str(item[3] or "").strip()
	return ""
