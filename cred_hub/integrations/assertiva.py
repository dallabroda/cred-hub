"""Cliente da API Localize v3 da Assertiva (credtech#290), conforme `localize/README.md` da documentação de APIs.

- Token OAuth2 de 1 hora no cache (Redis) do site, renovado sozinho no 401; o segredo nunca vai para log.
- Toda consulta exige a finalidade LGPD (`idFinalidade`): 2 = proteção ao crédito e cobrança, 1 = identidade.
- 404 é "não localizado" (resposta normal); 429 e 5xx são repetidos com espera; os outros erros viram mensagem.
- A leitura aceita a resposta da documentação (`telefones` em lista, `quadroSocietario`) e o envelope
  `cabecalho`/`resposta` com `telefones.moveis`/`telefones.fixos` e `socios`.
"""

import hashlib
import time

import frappe
import phonenumbers
import requests
from frappe import _
from frappe.utils import cint, flt, validate_email_address

from cred_hub.cnpj.mapping import only_digits

BASE_URL = "https://api.assertivasolucoes.com.br"
TOKEN_URL = f"{BASE_URL}/oauth2/v3/token"
CNPJ_URL = f"{BASE_URL}/localize/v3/cnpj"
CPF_URL = f"{BASE_URL}/localize/v3/cpf"
TIMEOUT = 30
ATTEMPTS = 3
# Rótulo das configurações -> idFinalidade
PURPOSES = {
	"Credit protection and collection": 2,
	"Identity confirmation and fraud prevention": 1,
}
_TOKEN_KEY = "cred_hub:assertiva_token"


class AssertivaError(Exception):
	def __init__(self, message: str, http_status: int | None = None):
		super().__init__(message)
		self.http_status = http_status


def _message_for(status: int) -> str:
	if status == 400:
		return _("Assertiva refused the CPF or CNPJ (HTTP 400).")
	if status == 403:
		return _("Assertiva denied the query: no balance or the module is not in your contract (HTTP 403).")
	if status == 422:
		return _("Assertiva refused the query parameters (HTTP 422).")
	return _("Assertiva refused the query (HTTP {0}).").format(status)


def forget_token():
	frappe.cache.delete_keys(_TOKEN_KEY)


class AssertivaClient:
	def __init__(self, client_id: str, client_secret: str, session=None, sleep=time.sleep):
		self.client_id = (client_id or "").strip()
		self.client_secret = client_secret or ""
		self.session = session or requests.Session()
		self.sleep = sleep

	def _cache_key(self) -> str:
		# a troca de credencial nas configurações não reaproveita o token da credencial antiga
		digest = hashlib.sha256(f"{self.client_id}:{self.client_secret}".encode()).hexdigest()[:16]
		return f"{_TOKEN_KEY}:{digest}"

	def token(self, renew: bool = False) -> str:
		if not self.client_id or not self.client_secret:
			raise AssertivaError(_("Fill the Assertiva Client ID and Client Secret in the settings."))
		key = self._cache_key()
		if not renew:
			cached = frappe.cache.get_value(key)
			if cached:
				return cached
		response = self._request_token()
		try:
			body = response.json() or {}
		except ValueError:
			body = {}
		token = body.get("access_token") or ""
		if not token:
			raise AssertivaError(_("Assertiva did not return an access token."), response.status_code)
		expires_in = cint(body.get("expires_in")) or 3600
		frappe.cache.set_value(key, token, expires_in_sec=max(expires_in - 120, 60))
		return token

	def _request_token(self):
		headers = {"Accept": "application/json"}
		try:
			# Formato da documentação: JSON com client_id e client_secret
			response = self.session.post(
				TOKEN_URL,
				json={"client_id": self.client_id, "client_secret": self.client_secret},
				headers=headers,
				timeout=TIMEOUT,
			)
			if response.status_code in (400, 401, 415):
				# Padrão OAuth2 client_credentials (Basic + formulário), aceito pelo mesmo endpoint
				response = self.session.post(
					TOKEN_URL,
					data={"grant_type": "client_credentials"},
					auth=(self.client_id, self.client_secret),
					headers=headers,
					timeout=TIMEOUT,
				)
		except requests.RequestException as e:
			raise AssertivaError(_("Could not reach Assertiva. Try again in a few minutes.")) from e
		if response.status_code != 200:
			raise AssertivaError(
				_("Assertiva refused the Client ID or Client Secret (HTTP {0}).").format(response.status_code),
				response.status_code,
			)
		return response

	def get(self, url: str, params: dict) -> tuple[int, dict | None]:
		"""(200, corpo) ou (404, None). Uma consulta paga: só repete falha de rede, 429 e 5xx."""
		token = self.token()
		renewed = False
		last = None
		attempt = 0
		while attempt < ATTEMPTS:
			try:
				response = self.session.get(
					url,
					params=params,
					headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
					timeout=TIMEOUT,
				)
			except requests.RequestException:
				last = None
			else:
				last = response.status_code
				if last == 200:
					try:
						body = response.json()
					except ValueError as e:
						raise AssertivaError(_("Assertiva answered with an invalid response."), 200) from e
					if not isinstance(body, dict):
						raise AssertivaError(_("Assertiva answered with an invalid response."), 200)
					return 200, body
				if last == 404:
					return 404, None
				if last == 401 and not renewed:
					renewed = True
					token = self.token(renew=True)
					continue
				if last == 401:
					raise AssertivaError(_("Assertiva refused the Client ID or Client Secret (HTTP 401)."), 401)
				if last != 429 and last < 500:
					raise AssertivaError(_message_for(last), last)
			attempt += 1
			if attempt < ATTEMPTS:
				self.sleep(2 * attempt)
		raise AssertivaError(_("Assertiva is unavailable right now. Try again in a few minutes."), last)

	def localize_cnpj(self, cnpj: str, purpose: int = 2):
		return self.get(CNPJ_URL, {"cnpj": only_digits(cnpj), "idFinalidade": str(purpose)})

	def localize_cpf(self, cpf: str, purpose: int = 2):
		return self.get(CPF_URL, {"cpf": only_digits(cpf), "idFinalidade": str(purpose)})


# Leitura da resposta ------------------------------------------------------------------------------------------


def body_of(payload) -> dict:
	if isinstance(payload, dict) and isinstance(payload.get("resposta"), dict):
		return payload["resposta"]
	return payload if isinstance(payload, dict) else {}


def protocol_of(payload) -> str:
	if not isinstance(payload, dict):
		return ""
	header = payload.get("cabecalho") if isinstance(payload.get("cabecalho"), dict) else {}
	return str(payload.get("protocolo") or header.get("protocolo") or "").strip()


def e164(ddd, number) -> str | None:
	digits = only_digits(f"{ddd or ''}{number or ''}")
	if len(digits) > 11 and digits.startswith("55"):
		digits = digits[2:]
	try:
		parsed = phonenumbers.parse(digits, "BR")
	except phonenumbers.NumberParseException:
		return None
	if not phonenumbers.is_valid_number(parsed):
		return None
	return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _has_whatsapp(item: dict) -> bool:
	apps = item.get("aplicativos") if isinstance(item.get("aplicativos"), dict) else {}
	return any(
		str(value).strip().lower() in ("true", "1", "s", "sim")
		for value in (item.get("temWhatsApp"), item.get("whatsApp"), item.get("whatsapp"), apps.get("whatsApp"))
		if value is not None
	)


def phones(payload) -> list[dict]:
	"""Telefones na ordem de uso: celulares com WhatsApp primeiro, depois maior assertividade (documentação 6.1)."""
	raw = body_of(payload).get("telefones") or []
	if isinstance(raw, dict):
		raw = [
			{**item, "tipo": item.get("tipo") or kind}
			for key, kind in (("moveis", "MOVEL"), ("fixos", "FIXO"))
			for item in (raw.get(key) or [])
			if isinstance(item, dict)
		]
	found = {}
	for item in raw:
		if not isinstance(item, dict):
			continue
		number = e164(item.get("ddd"), item.get("numero") or item.get("telefone"))
		if not number:
			continue
		kind = str(item.get("tipo") or "").upper()
		mobile = "MOV" in kind or "CEL" in kind
		if not kind:
			mobile = phonenumbers.number_type(phonenumbers.parse(number)) == phonenumbers.PhoneNumberType.MOBILE
		phone = {
			"phone": number,
			"mobile": mobile,
			"whatsapp": mobile and _has_whatsapp(item),
			"score": max(min(cint(item.get("scoreAssertividade") or item.get("score")), 100), 0),
			"carrier": str(item.get("operadora") or "").strip()[:40],
		}
		current = found.get(number)
		if not current or (phone["whatsapp"], phone["score"]) > (current["whatsapp"], current["score"]):
			found[number] = phone
	return sorted(found.values(), key=lambda p: (not p["mobile"], not p["whatsapp"], -p["score"]))


def emails(payload) -> list[str]:
	raw = body_of(payload).get("emails") or []
	scored = {}
	for item in raw:
		address = item.get("email") if isinstance(item, dict) else item
		address = str(address or "").strip().lower()
		if not address or not validate_email_address(address):
			continue
		score = cint(item.get("score")) if isinstance(item, dict) else 0
		scored[address] = max(score, scored.get(address, 0))
	return sorted(scored, key=lambda address: -scored[address])


def partners(payload) -> list[dict]:
	"""Sócios pessoa física do quadro societário (o contato é uma pessoa: sócio empresa fica de fora)."""
	body = body_of(payload)
	raw = body.get("quadroSocietario") or body.get("socios") or []
	found = []
	for item in raw:
		if not isinstance(item, dict):
			continue
		cpf = only_digits(item.get("cpf") or item.get("documento"))
		name = str(item.get("nome") or item.get("nomeOuRazaoSocial") or "").strip()
		if len(cpf) != 11 or not name:
			continue
		share = item.get("percentualCapital")
		found.append(
			{
				"cpf": cpf,
				"name": name,
				"qualification": str(item.get("qualificacao") or "").strip(),
				"share": flt(share) if share not in (None, "") else None,
			}
		)
	return found
