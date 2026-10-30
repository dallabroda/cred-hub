"""Cliente SOAP da API CENPROT Empresas v2.2 (IEPTB), conforme `cenprot/240604 Documentacao_API_CenprotEmpresas_v22_1.pdf`
e `cenprot/README.md` da documentação de APIs (credtech#290, módulo 2).

- `Autenticar` devolve um token de sessão que vai em toda chamada seguinte; token inválido (WS_ERR_002) renova
  uma vez.
- O manual pede dados em ASCII: os textos vão sem acento, em maiúsculas e cortados no tamanho de cada campo.
- O `<token>` do EnviarTitulo fica fora do `<titulo>`: é o formato que o IEPTB aceita em produção (README).
- Valores com ponto decimal; vencimento à vista como 99/99/9999.
"""

import unicodedata
from datetime import datetime
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from frappe import _

PRODUCTION = "https://wspub.cenprotnacional.org.br/ieptb-cenprot-empresas-arquivos"
HOMOLOGATION = "https://wspub.cenprotnacional.org.br/ieptb-cenprot-empresas-arquivos-hml"
TIMEOUT = 60

# Status do campo "status" (manual, pág. 15) e os nomes da README que o manual não lista
INTEGRATOR_STATUSES = ("INEXISTENTE", "COLETADO", "GERADO", "AGENDADO", "ENVIADO")
REGISTRY_STATUSES = (
	"CONFIRMADO", "DEVOLVIDO", "CANCELADO", "PAGO", "PROTESTADO", "RETIRADO", "SUSTADO", "SUSPENSO",
	"DISTRIBUIDO", "INTIMADO",
)
KNOWN_STATUSES = frozenset(INTEGRATOR_STATUSES + REGISTRY_STATUSES)
# Operação de retirada por status (manual, pág. 16)
OPERATION_FOR_STATUS = {
	"COLETADO": "REMOCAO",
	"GERADO": "REMOCAO",
	"CONFIRMADO": "DESISTENCIA",
	"DISTRIBUIDO": "DESISTENCIA",
	"INTIMADO": "DESISTENCIA",
	"PROTESTADO": "CANCELAMENTO",
}
DUPLICATE = "WS_ERR_300"
INVALID_TOKEN = "WS_ERR_002"
AT_SIGHT = "99/99/9999"

# Tamanhos do manual (págs. 4 a 6): cedente/credor, devedor e dívida
PARTY_SIZES = {"nome": 100, "endereco": 100, "numero": 10, "complemento": 50, "bairro": 20, "municipio": 50}
DEBTOR_SIZES = {"nome": 45, "endereco": 40, "numero": 5, "complemento": 20, "bairro": 20, "municipio": 20}
TITLE_NUMBER_SIZE, OUR_NUMBER_SIZE = 11, 15


class CenprotError(Exception):
	def __init__(self, message: str, code: str = ""):
		super().__init__(message)
		self.code = code


def endpoint(environment: str) -> str:
	return PRODUCTION if environment == "Production" else HOMOLOGATION


def ascii_text(value, size: int | None = None) -> str:
	text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
	text = " ".join(text.upper().split())
	return text[:size] if size else text


def digits(value) -> str:
	return "".join(ch for ch in str(value or "") if ch.isdigit())


def document_type(document: str) -> str:
	"""1 = CPF, 2 = CNPJ."""
	return "1" if len(digits(document)) == 11 else "2"


def money(value) -> str:
	return f"{float(value or 0):.2f}"


def br_date(value) -> str:
	"""date/datetime/'YYYY-MM-DD' -> DD/MM/AAAA."""
	if not value:
		return ""
	if isinstance(value, str):
		value = datetime.strptime(value[:10], "%Y-%m-%d")
	return value.strftime("%d/%m/%Y")


def _tag(name: str, value) -> str:
	return f"<{name}>{escape(str(value if value is not None else ''))}</{name}>"


def _party(tag: str, party: dict, sizes: dict, extra: str = "") -> str:
	document = digits(party.get("documento"))
	parts = [_tag("codigo", party["codigo"])] if party.get("codigo") else []
	parts += [
		_tag("nome", ascii_text(party.get("nome"), sizes["nome"])),
		_tag("documentoTipo", document_type(document)),
		_tag("documento", document),
		_tag("endereco", ascii_text(party.get("endereco"), sizes["endereco"])),
		_tag("numero", digits(party.get("numero"))[: sizes["numero"]]),
		_tag("complemento", ascii_text(party.get("complemento"), sizes["complemento"])),
		_tag("cep", digits(party.get("cep"))[:8]),
		_tag("bairro", ascii_text(party.get("bairro"), sizes["bairro"])),
		_tag("municipio", ascii_text(party.get("municipio"), sizes["municipio"])),
		_tag("uf", ascii_text(party.get("uf"), 2)),
	]
	return f"<{tag}>{''.join(parts)}{extra}</{tag}>"


def title_xml(title: dict) -> str:
	"""Conteúdo do <titulo> do EnviarTitulo (sem o token). `title` traz cedente, devedores, divida e, opcional,
	geral (codPortador/nomePortador)."""
	parts = [_tag("alteracao", "N")]
	general = title.get("geral") or {}
	if general.get("codPortador"):
		parts.append(
			"<geral>"
			+ _tag("codPortador", ascii_text(general["codPortador"], 3))
			+ _tag("nomePortador", ascii_text(general.get("nomePortador"), 40))
			+ _tag("dataMovimento", general.get("dataMovimento") or datetime.now().strftime("%d%m%Y"))
			+ "</geral>"
		)
	parts.append(_party("cedente", title["cedente"], PARTY_SIZES))
	debtors = title["devedores"]
	for index, debtor in enumerate(debtors):
		extra = ""
		if len(debtors) > 1:
			extra += _tag("principal", "S" if index == 0 else "N")
		extra += _tag("fone", digits(debtor.get("fone"))[:11]) + _tag("email", (debtor.get("email") or "")[:60])
		parts.append(_party("devedor", debtor, DEBTOR_SIZES, extra))
	debt = title["divida"]
	debt_parts = [
		_tag("especie", ascii_text(debt["especie"], 10)),
		_tag("numero", ascii_text(debt["numero"], TITLE_NUMBER_SIZE)),
		_tag("nossoNumero", ascii_text(debt["nossoNumero"], OUR_NUMBER_SIZE)),
		_tag("valor", money(debt["valor"])),
		_tag("saldo", money(debt.get("saldo") or debt["valor"])),
	]
	if debt.get("tipoEndosso"):
		debt_parts.append(_tag("tipoEndosso", debt["tipoEndosso"]))
	debt_parts += [
		_tag("aceite", debt.get("aceite") or "N"),
		_tag("finsFalimentares", "N"),
		_tag("declaracaoPortador", debt["declaracaoPortador"]),
		_tag("emissao", debt["emissao"]),
		_tag("vencimento", debt["vencimento"]),
	]
	parts.append(f"<divida>{''.join(debt_parts)}</divida>")
	return "".join(parts)


def _local(element) -> str:
	return element.tag.rsplit("}", 1)[-1]


def _first(root, name: str) -> str:
	for element in root.iter():
		if _local(element) == name and (element.text or "").strip():
			return element.text.strip()
	return ""


def _child(element, name: str):
	return next((item for item in list(element) if _local(item) == name), None) if element is not None else None


def _child_text(element, name: str) -> str:
	found = _child(element, name)
	return (found.text or "").strip() if found is not None and found.text else ""


def result_of(root) -> dict:
	"""codigo/mensagem/status de uma resposta (a primeira <resposta>, ou o que houver no corpo)."""
	answer = next((element for element in root.iter() if _local(element) == "resposta"), None)
	scope = answer if answer is not None else root
	code = _first(scope, "codigo")
	message = _first(scope, "mensagem") or _first(root, "faultstring")
	flag = _first(scope, "status").lower()
	return {"code": code, "message": message, "ok": code.startswith("WS_SUC") and flag != "false"}


def _parse_datetime(value: str) -> datetime:
	for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
		try:
			return datetime.strptime(value.strip(), fmt)
		except ValueError:
			continue
	return datetime.min


def parse_consult(root) -> dict | None:
	"""Situação do título na resposta do ConsultarTitulo. Com vários títulos (manual v2.1), vale o de ocorrência
	mais recente. None quando não há título na resposta."""
	titles = [element for element in root.iter() if _local(element) == "titulo"]
	if not titles:
		return None

	def events_of(title):
		events = []
		for item in list(title):
			if _local(item) != "ocorrencia":
				continue
			events.append(
				{
					"at": _child_text(item, "dataHora"),
					"status": _child_text(item, "status").upper(),
					"message": _child_text(item, "mensagem"),
				}
			)
		return sorted(events, key=lambda event: _parse_datetime(event["at"]))

	title = max(titles, key=lambda t: max((_parse_datetime(e["at"]) for e in events_of(t)), default=datetime.min))
	events = events_of(title)
	answer = _child(title, "resposta")
	code = _child_text(answer, "codigo").upper()
	status = code if code in KNOWN_STATUSES else next((e["status"] for e in reversed(events) if e["status"]), "")
	registry = _child(title, "cartorio")
	message = _child_text(answer, "mensagem")
	if message.lower() == "null":
		message = ""
	return {
		"status": status,
		"message": message or (events[-1]["message"] if events and events[-1]["message"] != "null" else ""),
		"events": events,
		"protocol": _child_text(registry, "protocolo"),
		"registry": " ".join(
			filter(None, (_child_text(registry, "comarca"), _child_text(registry, "cartorio")))
		),
		"registry_date": _child_text(registry, "data"),
	}


class CenprotClient:
	def __init__(self, username: str, password: str, environment: str = "Homologation", presenter: str = "",
			session=None, token: str = ""):
		self.username = username or ""
		self.password = password or ""
		self.base = endpoint(environment)
		self.url = f"{self.base}/ProtestoInterface"
		self.namespace = f"{self.base}/services"
		self.presenter = presenter or ""
		self.session = session or requests.Session()
		self.token = token

	def _post(self, action: str, body: str):
		envelope = (
			'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
			f'<{action} xmlns="{self.namespace}">{body}</{action}>'
			"</soapenv:Body></soapenv:Envelope>"
		)
		try:
			response = self.session.post(
				self.url,
				data=envelope.encode("ascii", errors="xmlcharrefreplace"),
				headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": action},
				timeout=TIMEOUT,
			)
		except requests.RequestException as e:
			raise CenprotError(_("Could not reach CENPROT. Try again in a few minutes.")) from e
		try:
			root = ET.fromstring(response.content)
		except ET.ParseError as e:
			raise CenprotError(_("CENPROT answered with an invalid response (HTTP {0}).").format(response.status_code)) from e
		return root, response.content.decode("utf-8", errors="replace")

	def authenticate(self) -> str:
		if not self.username or not self.password:
			raise CenprotError(_("Fill the CENPROT username and password in the settings."))
		presenter = _tag("apresentante", self.presenter) if self.presenter else ""
		root, _raw = self._post(
			"Autenticar",
			'<credenciais xmlns="">'
			f"{_tag('usuario', self.username)}{_tag('senha', self.password)}{presenter}"
			"</credenciais>",
		)
		token = _first(root, "token")
		if not token:
			result = result_of(root)
			raise CenprotError(
				_("CENPROT refused the login: {0}").format(result["message"] or result["code"] or _("no message")),
				result["code"],
			)
		self.token = token
		return token

	def _call(self, action: str, build) -> tuple[dict, object, str]:
		"""Chama com o token atual; token inválido (WS_ERR_002) autentica de novo uma vez."""
		if not self.token:
			self.authenticate()
		root, raw = self._post(action, build(self.token))
		result = result_of(root)
		if result["code"] == INVALID_TOKEN:
			self.authenticate()
			root, raw = self._post(action, build(self.token))
			result = result_of(root)
		return result, root, raw

	def send_title(self, title: dict) -> dict:
		body = title_xml(title)
		result, root, raw = self._call(
			"EnviarTitulo", lambda token: f'<token xmlns="">{escape(token)}</token><titulo xmlns="">{body}</titulo>'
		)
		result["duplicate"] = result["code"] == DUPLICATE
		result["protocol"] = _first(root, "protocolo")
		result["raw"] = raw
		return result

	def consult_title(self, title: dict) -> dict:
		debt = title["divida"]
		registry = ""
		if title.get("protocolo"):
			registry = f"<cartorio>{_tag('protocolo', title['protocolo'])}</cartorio>"
		body = (
			"<devedor>"
			+ _tag("documento", digits(title["devedor"]["documento"]))
			+ _tag("nome", ascii_text(title["devedor"].get("nome"), DEBTOR_SIZES["nome"]))
			+ "</devedor><divida>"
			+ _tag("numero", ascii_text(debt["numero"], TITLE_NUMBER_SIZE))
			+ _tag("nossoNumero", ascii_text(debt["nossoNumero"], OUR_NUMBER_SIZE))
			+ _tag("especie", ascii_text(debt["especie"], 10))
			+ _tag("vencimento", debt["vencimento"])
			+ _tag("emissao", debt["emissao"])
			+ "</divida>"
			+ registry
		)
		result, root, raw = self._call(
			"ConsultarTitulo",
			lambda token: (
				f'<token xmlns="">{escape(token)}</token><completa xmlns="">S</completa>'
				f'<instrumento xmlns="">N</instrumento><anuencia xmlns="">N</anuencia><titulo xmlns="">{body}</titulo>'
			),
		)
		result["title"] = parse_consult(root)
		result["raw"] = raw
		return result

	def operate_title(self, title: dict, operation: str, justification: str = "") -> dict:
		debt = title["divida"]
		registry = ""
		if title.get("protocolo"):
			registry = f"<cartorio>{_tag('protocolo', title['protocolo'])}</cartorio>"
		body = (
			_tag("autoriza", "S")
			+ _tag("operacao", operation)
			+ _tag("justificativa", ascii_text(justification, 200))
			+ "<devedor>"
			+ _tag("documento", digits(title["devedor"]["documento"]))
			+ "</devedor><divida>"
			+ _tag("numero", ascii_text(debt["numero"], TITLE_NUMBER_SIZE))
			+ _tag("nossoNumero", ascii_text(debt["nossoNumero"], OUR_NUMBER_SIZE))
			+ _tag("vencimento", debt["vencimento"])
			+ _tag("especie", ascii_text(debt["especie"], 10))
			+ "</divida>"
			+ registry
		)
		result, _root, raw = self._call(
			"OperacaoTitulo", lambda token: f'<token xmlns="">{escape(token)}</token><titulo xmlns="">{body}</titulo>'
		)
		result["raw"] = raw
		return result
