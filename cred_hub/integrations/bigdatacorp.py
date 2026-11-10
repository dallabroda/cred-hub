"""Cliente e adaptador da BigDataCorp para Localização & Recuperação de Crédito (Substituto 100% do Assertiva Localize).

Projetado como substituto drop-in de `cred_hub/integrations/assertiva.py` para o módulo `localize.py` no HubTech CRM,
e também compatível com execução standalone (sem Frappe) para o Portal Independente do Bureau:
- Expõe a mesma interface pública (`BigDataCorpClient`, `BigDataCorpError`, `partners`, `phones`, `emails`,
  `addresses`, `protocol_of`), permitindo virar a chave em `localize.py` sem alterar o fluxo do CRM nem a
  integração com a CENPROT.
- Grande vantagem econômica sobre o Assertiva Localize:
  Na chamada de CNPJ (`POST /empresas`), os datasets `relationships`, `phones_extended`, `related_people_phones`,
  `emails_extended`, `related_people_emails`, `addresses_extended` e `related_people_addresses` já devolvem
  todos os sócios (CPF completo desmascarado) + todos os telefones, e-mails e endereços (com CEP de 8 dígitos
  para a CENPROT) de TODOS os sócios de uma só vez por um custo fixo (~R$ 0,40), sem cobrar uma consulta extra
  por cada sócio pessoa física.
"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

from cred_hub.cnpj.mapping import only_digits

try:
	import phonenumbers
except ImportError:  # pragma: no cover
	phonenumbers = None

try:
	from frappe import _
	from frappe.utils import cint, flt, validate_email_address
except ImportError:  # Fallback standalone (Portal Independente / Pytest sem Frappe)
	def _(msg: str) -> str:
		return msg

	def cint(val: Any) -> int:
		try:
			return int(float(val or 0))
		except (ValueError, TypeError):
			return 0

	def flt(val: Any) -> float:
		try:
			return float(val or 0.0)
		except (ValueError, TypeError):
			return 0.0

	def validate_email_address(val: str) -> str:
		val = (val or "").strip()
		return val if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val) else ""


DEFAULT_BASE_URL = "https://plataforma.bigdatacorp.com.br"
EMPRESAS_URL = f"{DEFAULT_BASE_URL}/empresas"
PESSOAS_URL = f"{DEFAULT_BASE_URL}/pessoas"
MARKETPLACE_URL = f"{DEFAULT_BASE_URL}/marketplace"
ONDEMAND_URL = f"{DEFAULT_BASE_URL}/ondemand"
TIMEOUT = 30
ATTEMPTS = 3

STREET_TYPES = {
	"R": "RUA",
	"AV": "AVENIDA",
	"AL": "ALAMEDA",
	"TV": "TRAVESSA",
	"PC": "PRACA",
	"ROD": "RODOVIA",
	"EST": "ESTRADA",
}

# Modo Estrito CENPROT (Padrão Cobrança): Só CPF desmascarado + Endereço/CEP de todos os sócios (COGS R$ 0,10 fixos por CNPJ)
CENPROT_CNPJ_DATASETS = "relationships,related_people_addresses"

# Modo Reaproveitamento CENPROT (quando o CRM já tem os CPFs dos sócios da Análise de Crédito): Só Endereço/CEP (COGS R$ 0,06 fixos)
CENPROT_ONLY_ADDRESSES_DATASETS = "related_people_addresses"

# Modo Completo Localize (CENPROT + Telefones/WhatsApp + E-mails da empresa e sócios — COGS R$ 0,40 fixos por CNPJ)
LOCALIZE_CNPJ_FULL_CONTACTS_DATASETS = (
	"relationships,"
	"phones_extended,"
	"related_people_phones,"
	"emails_extended,"
	"related_people_emails,"
	"addresses_extended,"
	"related_people_addresses"
)

# Padrão utilizado pelo módulo de cobrança/CENPROT (R$ 0,10 fixos por CNPJ)
LOCALIZE_CNPJ_DATASETS = CENPROT_CNPJ_DATASETS

# Datasets de Localização PF para CENPROT (COGS R$ 0,06 por CPF para endereço, ou R$ 0,22 c/ telefones e e-mails)
CENPROT_CPF_DATASETS = "addresses_extended"
LOCALIZE_CPF_FULL_CONTACTS_DATASETS = (
	"basic_data,"
	"phones_extended,"
	"emails_extended,"
	"addresses_extended"
)
LOCALIZE_CPF_DATASETS = CENPROT_CPF_DATASETS

RE_RELATED_TYPE = re.compile(r"RELATED\s*-\s*(\d{11})\s*-\s*([A-Z_]+)\s*-\s*(.+)", re.I)


def e164(ddd: Any, number: Any) -> str | None:
	"""Formata DDD + número para o padrão E.164 brasileiro (+55...)."""
	digits = only_digits(f"{ddd or ''}{number or ''}")
	if len(digits) > 11 and digits.startswith("55"):
		digits = digits[2:]
	if phonenumbers is not None:
		try:
			parsed = phonenumbers.parse(digits, "BR")
		except phonenumbers.NumberParseException:
			return None
		if not phonenumbers.is_valid_number(parsed):
			return None
		return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
	if len(digits) in (10, 11):
		return f"+55{digits}"
	return None


class BigDataCorpError(Exception):
	"""Erro de comunicação ou autenticação na API da BigDataCorp."""

	def __init__(self, message: str, http_status: int | None = None):
		super().__init__(message)
		self.http_status = http_status


def parse_credentials(access_token: str, token_id: str = "") -> tuple[str, str]:
	"""Aceita AccessToken e TokenId separados ou combinados no formato 'AccessToken:TokenId' / 'AccessToken|TokenId'."""
	raw_token = (access_token or "").strip()
	raw_id = (token_id or "").strip()
	if not raw_id and (":" in raw_token or "|" in raw_token):
		sep = ":" if ":" in raw_token else "|"
		parts = raw_token.split(sep, 1)
		raw_token = parts[0].strip()
		raw_id = parts[1].strip()
	return raw_token, raw_id


def _message_for(status: int) -> str:
	if status == 400:
		return _("BigDataCorp refused the query parameters (HTTP 400).")
	if status in (401, 403):
		return _("BigDataCorp refused the AccessToken or TokenId, or the account has no balance (HTTP {0}).").format(status)
	if status == 429:
		return _("BigDataCorp rate limit exceeded (HTTP 429).")
	return _("BigDataCorp refused the query (HTTP {0}).").format(status)


class BigDataCorpClient:
	"""Cliente HTTP para a Plataforma BigDataCorp (/empresas, /pessoas, /marketplace, /ondemand)."""

	def __init__(
		self,
		access_token: str,
		token_id: str = "",
		base_url: str = DEFAULT_BASE_URL,
		session=None,
		sleep=time.sleep,
	):
		self.access_token, self.token_id = parse_credentials(access_token, token_id)
		self.base_url = base_url.rstrip("/")
		self.session = session or requests.Session()
		self.sleep = sleep

	def _headers(self) -> dict[str, str]:
		if not self.access_token:
			raise BigDataCorpError(_("Fill the BigDataCorp AccessToken in the settings."))
		headers = {
			"AccessToken": self.access_token,
			"Accept": "application/json",
			"Content-Type": "application/json",
		}
		if self.token_id:
			headers["TokenId"] = self.token_id
		return headers

	def post(self, endpoint: str, datasets: str, q: str, limit: int = 1) -> tuple[int, dict[str, Any] | None]:
		"""Executa POST na BigDataCorp com retry automático para falhas transitórias (429 e 5xx)."""
		url = f"{self.base_url}/{endpoint.lstrip('/')}"
		headers = self._headers()
		body = {
			"Datasets": datasets,
			"q": q,
			"Limit": limit,
		}
		last_status = None
		for attempt in range(ATTEMPTS):
			try:
				response = self.session.post(url, json=body, headers=headers, timeout=TIMEOUT)
			except requests.RequestException:
				last_status = None
			else:
				last_status = response.status_code
				if last_status == 200:
					try:
						payload = response.json()
					except ValueError as e:
						raise BigDataCorpError(_("BigDataCorp answered with an invalid JSON response."), 200) from e
					if not isinstance(payload, dict):
						raise BigDataCorpError(_("BigDataCorp answered with an invalid response."), 200)
					results = payload.get("Result")
					if isinstance(results, list) and len(results) == 0:
						return 404, None
					return 200, payload
				if last_status == 404:
					return 404, None
				if last_status != 429 and last_status < 500:
					raise BigDataCorpError(_message_for(last_status), last_status)
			if attempt + 1 < ATTEMPTS:
				self.sleep(2 * (attempt + 1))
		raise BigDataCorpError(_("BigDataCorp is unavailable right now. Try again in a few minutes."), last_status)

	def localize_cnpj(
		self,
		cnpj: str,
		purpose: int = 2,
		include_contacts: bool = False,
		already_has_partner_cpfs: bool = False,
	) -> tuple[int, dict[str, Any] | None]:
		"""Consulta CNPJ para alimentar o CENPROT (R$ 0,10 fixos por padrão: CPF desmascarado + Endereços/CEP dos sócios;
		cai para R$ 0,06 se o CRM já tiver os CPFs dos sócios da análise de crédito; ou R$ 0,40 se `include_contacts=True`)."""
		digits = only_digits(cnpj)
		if include_contacts:
			datasets = LOCALIZE_CNPJ_FULL_CONTACTS_DATASETS
		elif already_has_partner_cpfs:
			datasets = CENPROT_ONLY_ADDRESSES_DATASETS
		else:
			datasets = CENPROT_CNPJ_DATASETS
		return self.post("empresas", datasets, f"doc{{{digits}}}")

	def localize_cpf(
		self,
		cpf: str,
		purpose: int = 2,
		include_contacts: bool = False,
	) -> tuple[int, dict[str, Any] | None]:
		"""Consulta CPF individual para alimentar o CENPROT (R$ 0,06 só endereço, ou R$ 0,22 com telefones/e-mails)."""
		digits = only_digits(cpf)
		datasets = LOCALIZE_CPF_FULL_CONTACTS_DATASETS if include_contacts else CENPROT_CPF_DATASETS
		return self.post("pessoas", datasets, f"doc{{{digits}}}")


# Extratores compatíveis 1:1 com `cred_hub/integrations/assertiva.py` ------------------------------------------


def first_entity(payload: dict[str, Any] | None) -> dict[str, Any]:
	"""Retorna o primeiro objeto dentro de `Result` do payload da BigDataCorp."""
	if not isinstance(payload, dict):
		return {}
	results = payload.get("Result")
	if isinstance(results, list) and results and isinstance(results[0], dict):
		return results[0]
	return payload


def protocol_of(payload: dict[str, Any] | None) -> str:
	"""Extrai o `QueryId` da BigDataCorp para auditoria LGPD no DocType `Cred Hub Bureau Query`."""
	if not isinstance(payload, dict):
		return ""
	return str(payload.get("QueryId") or payload.get("protocolo") or "").strip()


def _is_active_relationship(rel: dict[str, Any]) -> bool:
	end_date = str(rel.get("RelationshipEndDate") or "").strip()
	if not end_date or end_date.startswith("9999") or end_date.startswith("0001"):
		return True
	return False


def partners(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
	"""Extrai os sócios PF do quadro societário (`Relationships`) com CPF de 11 dígitos desmascarado."""
	entity = first_entity(payload)
	rel_root = entity.get("Relationships") if isinstance(entity.get("Relationships"), dict) else {}
	rel_list = rel_root.get("Relationships") if isinstance(rel_root.get("Relationships"), list) else []

	found: dict[str, dict[str, Any]] = {}
	other_companies_by_cpf: dict[str, list[dict[str, str]]] = {}

	for item in rel_list:
		if not isinstance(item, dict):
			continue
		tax_type = str(item.get("RelatedEntityTaxIdType") or "").upper()
		doc = only_digits(item.get("RelatedEntityTaxIdNumber"))
		name = str(item.get("RelatedEntityName") or "").strip()
		rel_type = str(item.get("RelationshipType") or "").upper()
		rel_name = str(item.get("RelationshipName") or "").strip()

		if tax_type == "CPF" and len(doc) == 11 and name and "*" not in doc:
			if rel_type not in ("QSA", "OWNERSHIP", "REPRESENTANTELEGAL", "ADMIN", ""):
				continue
			if not _is_active_relationship(item):
				continue
			if doc not in found:
				found[doc] = {
					"cpf": doc,
					"name": name,
					"qualification": rel_name or rel_type or "Sócio",
					"share": flt(item.get("OwnershipPercentage")) if item.get("OwnershipPercentage") not in (None, "") else None,
					"other_companies": [],
					"inline_person_payload": extract_partner_inline_payload(payload, doc),
				}
		elif tax_type == "CNPJ" and len(doc) == 14 and name:
			other_companies_by_cpf.setdefault("_company_links", []).append(
				{"cnpj": doc, "name": name, "relationship": rel_name or rel_type}
			)

	for partner in found.values():
		if "_company_links" in other_companies_by_cpf:
			partner["other_companies"] = other_companies_by_cpf["_company_links"]

	return list(found.values())


def _extract_cpf_from_related_type(type_str: str) -> str:
	match = RE_RELATED_TYPE.search(str(type_str or ""))
	return match.group(1) if match else ""


def extract_partner_inline_payload(payload: dict[str, Any] | None, partner_cpf: str) -> dict[str, Any]:
	"""Monta um payload individual para o CPF do sócio a partir de `RelatedPeople*` já retornados no CNPJ."""
	cpf_digits = only_digits(partner_cpf)
	entity = first_entity(payload)
	rel_phones = [
		p for p in (entity.get("RelatedPeoplePhones") or [])
		if isinstance(p, dict) and _extract_cpf_from_related_type(p.get("Type", "")) == cpf_digits
	]
	rel_emails = [
		e for e in (entity.get("RelatedPeopleEmails") or [])
		if isinstance(e, dict) and _extract_cpf_from_related_type(e.get("Type", "")) == cpf_digits
	]
	rel_addrs = [
		a for a in (entity.get("RelatedPeopleAddresses") or [])
		if isinstance(a, dict) and _extract_cpf_from_related_type(a.get("Type", "")) == cpf_digits
	]
	return {
		"QueryId": protocol_of(payload),
		"Result": [
			{
				"ExtendedPhones": {"Phones": rel_phones},
				"ExtendedEmails": {"Emails": rel_emails},
				"ExtendedAddresses": {"Addresses": rel_addrs},
			}
		],
	}


def _score_phone(item: dict[str, Any]) -> int:
	"""Converte os indicadores de validação e passagens da BigDataCorp num score de 0 a 100."""
	score = 50
	val_status = str(item.get("ValidationStatus") or "").upper()
	if val_status == "FOUND IN ABRT":
		score += 30
	elif val_status == "NUMBER IN RANGE LIST":
		score += 10
	elif "INVALID" in val_status or "NOT FOUND" in val_status:
		score -= 30

	if item.get("IsMain"):
		score += 10
	if item.get("IsRecent"):
		score += 10
	if item.get("IsActive") is False:
		score -= 25

	passages = cint(item.get("PhoneTotalPassages") or item.get("PhoneEntityTotalPassages") or 0)
	if passages >= 5:
		score += 5
	return max(min(score, 100), 0)


def phones(payload: dict[str, Any] | None, partner_cpf: str | None = None) -> list[dict[str, Any]]:
	"""Extrai e ordena telefones no formato exato esperado pelo `localize.py`."""
	if partner_cpf:
		payload = extract_partner_inline_payload(payload, partner_cpf)
	entity = first_entity(payload)
	ext_phones = entity.get("ExtendedPhones") if isinstance(entity.get("ExtendedPhones"), dict) else {}
	raw_list = list(ext_phones.get("Phones") or [])
	if not partner_cpf and isinstance(entity.get("RelatedPeoplePhones"), list):
		raw_list.extend(entity.get("RelatedPeoplePhones") or [])

	found: dict[str, dict[str, Any]] = {}
	for item in raw_list:
		if not isinstance(item, dict):
			continue
		raw_num = str(item.get("Number") or "")
		if "*" in raw_num:
			continue
		number = e164(item.get("AreaCode"), raw_num)
		if not number:
			continue
		kind = str(item.get("Type") or "").upper()
		mobile = "MOBILE" in kind or "MOV" in kind or "CEL" in kind
		if not kind and phonenumbers is not None:
			mobile = phonenumbers.number_type(phonenumbers.parse(number)) == phonenumbers.PhoneNumberType.MOBILE

		score = _score_phone(item)
		is_active = item.get("IsActive") is not False
		has_whatsapp = bool(mobile and is_active and score >= 70)
		dnd = bool(item.get("IsInDoNotCallList"))

		phone_entry = {
			"phone": number,
			"mobile": mobile,
			"whatsapp": has_whatsapp,
			"score": score,
			"carrier": str(item.get("CurrentCarrier") or "").strip()[:40],
			"dnd": dnd,
		}
		current = found.get(number)
		if not current or (phone_entry["whatsapp"], phone_entry["score"]) > (current["whatsapp"], current["score"]):
			found[number] = phone_entry

	return sorted(found.values(), key=lambda p: (not p["mobile"], not p["whatsapp"], -p["score"]))


def emails(payload: dict[str, Any] | None, partner_cpf: str | None = None) -> list[str]:
	"""Extrai e-mails validados e ordenados por prioridade/recência."""
	if partner_cpf:
		payload = extract_partner_inline_payload(payload, partner_cpf)
	entity = first_entity(payload)
	ext_emails = entity.get("ExtendedEmails") if isinstance(entity.get("ExtendedEmails"), dict) else {}
	raw_list = list(ext_emails.get("Emails") or [])
	if not partner_cpf and isinstance(entity.get("RelatedPeopleEmails"), list):
		raw_list.extend(entity.get("RelatedPeopleEmails") or [])

	scored: dict[str, int] = {}
	for item in raw_list:
		if not isinstance(item, dict):
			continue
		addr = str(item.get("EmailAddress") or "").strip().lower()
		if not addr or "*" in addr or not validate_email_address(addr):
			continue
		priority = cint(item.get("Priority") or 10)
		score = max(100 - (priority - 1) * 10, 10)
		if item.get("IsMain"):
			score += 15
		if item.get("IsRecent"):
			score += 10
		scored[addr] = max(score, scored.get(addr, 0))

	return sorted(scored, key=lambda a: -scored[a])


def addresses(payload: dict[str, Any] | None, partner_cpf: str | None = None) -> list[dict[str, Any]]:
	"""Extrai endereços completos com logradouro e CEP de 8 dígitos (compatível com a CENPROT)."""
	if partner_cpf:
		payload = extract_partner_inline_payload(payload, partner_cpf)
	entity = first_entity(payload)
	ext_addrs = entity.get("ExtendedAddresses") if isinstance(entity.get("ExtendedAddresses"), dict) else {}
	raw_list = list(ext_addrs.get("Addresses") or [])
	if not partner_cpf and isinstance(entity.get("RelatedPeopleAddresses"), list):
		raw_list.extend(entity.get("RelatedPeopleAddresses") or [])

	found: list[dict[str, Any]] = []
	for item in raw_list:
		if not isinstance(item, dict):
			continue
		street = str(item.get("AddressMain") or "").strip()
		typology = str(item.get("Typology") or "").strip().upper()
		title = str(item.get("Title") or "").strip()
		if title and not street.upper().startswith(title.upper()):
			street = f"{title} {street}".strip()
		if street and typology:
			type_full = STREET_TYPES.get(typology, typology)
			if not street.upper().startswith((f"{type_full} ", f"{typology} ")):
				street = f"{type_full} {street}"

		zip_code = only_digits(item.get("ZipCode"))
		if not street or "*" in street or len(zip_code) != 8:
			continue

		priority = cint(item.get("Priority") or 999)
		score = 90 if item.get("IsMain") else max(85 - (priority - 1) * 10, 20)
		if item.get("IsRecent"):
			score = min(score + 10, 100)

		found.append(
			{
				"street": street,
				"number": str(item.get("Number") or "").strip(),
				"complement": str(item.get("Complement") or "").strip(),
				"neighborhood": str(item.get("Neighborhood") or "").strip(),
				"city": str(item.get("City") or "").strip(),
				"state": str(item.get("State") or "").strip().upper()[:2],
				"zip": zip_code,
				"score": score,
				"ranking": priority,
			}
		)
	return sorted(found, key=lambda a: (-a["score"], a["ranking"]))
