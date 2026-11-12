"""QI Tech connector (ADR 0007 §10, credtech#375), from the public IaaS documentation (cred_hub/erp/qitech.py).

What the QI Tech API offers and what it does not (docs read 11/11/2026):

- ping: GET /authentication_test ({"success": "Congrats!"}), any profile host.
- cedente registry: GET /assignor_registry/assignor_registries (page/limit, filters document_number, name...).
- register cedente: POST /assignor_registry/assignor_registry. QI answers 201 with `assignor_registry_key` and status
  `pending_registry`: the cedente is created but only operates after QI's analysis (documents + analysis trigger,
  done in QI Tech) and a signed assignment contract.
- NOT offered by the REST API, so not declared:
  * overdue títulos: the per-título portfolio exists only as the daily `assets_wallet_composition` CSV delivered by
    SFTP/e-mail; REST lists assets only per assignment batch, with pipeline statuses (no open/overdue/paid);
  * settled títulos: no settlement listing by date (only one settlement by its id, payment batches without their
    settlements, the `settlement.settlement_status_change` webhook and a daily conciliation CSV);
  * sacado registry (sacados exist only inside each asset as `borrower`);
  * named lists.
  The collection sync therefore stays off for QI Tech (the shared code reads `capabilities`), until a file/webhook
  entry (ADR 0007 §1, door B) brings those títulos.
"""

from .. import formats
from ..qitech import DEFAULT_ALGORITHM, QiTechClient
from .base import ALREADY, CREATED, SENT, Capabilities, Connector, Credential, RegisterOutcome, register

QITECH = "QI Tech"


def _(text: str) -> str:
	try:
		import frappe

		return frappe._(text)
	except Exception:  # noqa: BLE001 - pure use (tests) has no translation
		return text


def normalize_pem(text: str) -> str:
	"""A PEM pasted in a one-line password box loses its line breaks: rebuild them (64 characters per line)."""
	text = (text or "").strip().replace("\\n", "\n")
	if "\n" in text or "-----BEGIN" not in text:
		return text
	head_end = text.index("-----", text.index("-----BEGIN") + 10) + 5
	foot_start = text.index("-----END")
	body = "".join(text[head_end:foot_start].split())
	lines = [body[i:i + 64] for i in range(0, len(body), 64)]
	return "\n".join([text[:head_end], *lines, text[foot_start:]])


def _flat(row: dict) -> dict:
	"""One level of nesting as "parent.child" columns, so the de-para can pick them by name."""
	out = {}
	for key, value in (row or {}).items():
		if isinstance(value, dict):
			for sub, inner in value.items():
				out[f"{key}.{sub}"] = inner
		else:
			out[key] = value
	return out


def _rule(erp_field, source="", transform="", default=None, required=False, label=""):
	return {"erp_field": erp_field, "source": source, "transform": transform, "default_value": default,
		"required": int(required), "label": label}


# POST /assignor_registry/assignor_registry (homologacao_cedente/cadastro). Required by QI: name, document_number
# (with punctuation), person_type, email, annual_revenues (integer >= 1), is_in_national_financial_system, address
# (street, number of 1-4 characters, neighborhood, city, uf, postal_code "XXXXX-XXX", country ISO alpha-3) and, for a
# company, related_parties with at least one representative (is_representative, with e-mail).
DEFAULT_RULES = [
	_rule("name", "organization.legal_name|organization.organization_name", "trim", required=True, label="Legal name"),
	_rule("document_number", "organization.cnpj", "document_formatted", required=True, label="CPF/CNPJ"),
	_rule("email", "organization.registry_email|contact.email_id", "lower", required=True, label="E-mail"),
	_rule("annual_revenues", "organization.annual_revenue", "integer", required=True, label="Annual revenue (R$)"),
	_rule("address.street", "address.address_line1", "street", required=True, label="Street"),
	_rule("address.number", "organization.address_number|address.address_line1", "street_number", required=True,
		label="Number (up to 4 characters)"),
	_rule("address.complement", "organization.address_complement", "trim", label="Complement"),
	_rule("address.neighborhood", "organization.neighborhood", "trim", required=True, label="District"),
	_rule("address.city", "address.city", "trim", required=True, label="City"),
	_rule("address.uf", "address.state", "upper", required=True, label="State (UF)"),
	_rule("address.postal_code", "address.pincode", "cep_formatted", required=True, label="ZIP code"),
	_rule("phone.area_code", "organization.registry_phone|contact.mobile_no", "phone_ddd", label="Phone area code"),
	_rule("phone.number", "organization.registry_phone|contact.mobile_no", "phone_number", label="Phone"),
	_rule("related_parties[0].name", "contact.full_name", "trim", label="Representative name"),
	_rule("related_parties[0].document_number", "contact.cpf", "document_formatted", label="Representative CPF"),
	_rule("related_parties[0].email", "contact.email_id", "lower", label="Representative e-mail"),
	_rule("related_parties[0].related_party_type", "", "", "partner", label="Representative role (QI Tech value)"),
	_rule("related_parties[0].is_representative", "", "bool", "1", label="Is the representative"),
]
EXTRA_FIELDS = [
	_rule("is_in_national_financial_system", "", "bool", "0", label="Part of the national financial system"),
	_rule("address.country", "", "upper", "BRA", label="Country (ISO alpha-3)"),
	_rule("related_parties[0].direct_beneficiary", "", "bool", "", label="Representative is a direct beneficiary"),
	_rule("minimum_required_signers", "", "integer", "", label="Minimum required signers"),
]


@register
class QiTechConnector(Connector):
	key = QITECH
	label = "QI Tech"
	credentials = (
		Credential("qitech_base_url", "QI Tech API URL", description="Host of your profile, e.g. "
			"https://assignor-api.qidtvm.com.br (sandbox: https://assignor-api.sandbox.qidtvm.com.br)"),
		Credential("qitech_api_key", "QI Tech API key", secret=True),
		Credential("qitech_private_key", "QI Tech private key (PEM)", secret=True),
		Credential("qitech_algorithm", "QI Tech signing algorithm", required=False),
	)
	capabilities = Capabilities(register_cedente=True, cedente_registry=True)

	@classmethod
	def make_client(cls, values: dict, **kwargs):
		return QiTechClient(
			values.get("qitech_base_url"),
			values.get("qitech_api_key"),
			normalize_pem(values.get("qitech_private_key")),
			values.get("qitech_algorithm") or DEFAULT_ALGORITHM,
			**kwargs,
		)

	@classmethod
	def catalog(cls) -> list[dict]:
		return [
			{"erp_field": r["erp_field"], "label": r["label"], "kind": "data", "transform": r["transform"],
			 "required": "yes" if r["required"] else ""}
			for r in DEFAULT_RULES + EXTRA_FIELDS
		]

	@classmethod
	def default_rules(cls) -> list[dict]:
		return [dict(r) for r in DEFAULT_RULES]

	@classmethod
	def documented_columns(cls, kind: str) -> list[str]:
		from .base import CEDENTE

		if kind != CEDENTE:
			return []
		return ["assignor_registry_key", "status", "name", "document_number", "person_type", "email",
			"annual_revenues", "address.street", "address.number", "address.neighborhood", "address.city",
			"address.uf", "address.postal_code", "organization_level"]

	@classmethod
	def fixed_columns(cls, kind: str) -> dict:
		from .base import CEDENTE

		return {"document_number": "document", "name": "name"} if kind == CEDENTE else {}

	def finalize_cedente(self, payload: dict):
		"""QI Tech values the de-para does not choose: person type from the document, country and the flag of the
		national financial system with their defaults, and a company's representative only when it was filled."""
		payload = dict(payload)
		document = formats.digits(payload.get("document_number"))
		payload["person_type"] = "natural_person" if len(document) == 11 else "legal_person"
		payload.setdefault("is_in_national_financial_system", False)
		if isinstance(payload.get("address"), dict):
			payload["address"].setdefault("country", "BRA")
		parties = [p for p in payload.get("related_parties") or [] if p and p.get("name")]
		if parties:
			payload["related_parties"] = parties
		else:
			payload.pop("related_parties", None)
		if not (payload.get("phone") or {}).get("number"):
			payload.pop("phone", None)
		return payload

	# Operations -------------------------------------------------------------------------------------------------
	def ping(self) -> dict:
		return self.client.ping()

	def cedentes(self):
		for row in self.client.assignors():
			flat = _flat(row)
			flat["CPF/CNPJ"] = formats.digits(row.get("document_number"))
			flat["Nome"] = row.get("name") or ""
			yield flat

	def register_cedente(self, context) -> RegisterOutcome:
		document = formats.digits(context.organization.get("cnpj"))
		return self.send_registration(self.finalize_cedente(context.result.payload), document)

	def send_registration(self, payload: dict, document: str) -> RegisterOutcome:
		"""Check by document first (never two registrations of the same company), then register once."""
		existing = self.client.find_assignor(formats.document_formatted(document))
		if existing and formats.digits(existing.get("document_number")) == document:
			return RegisterOutcome(ALREADY, existing.get("assignor_registry_key"), _(
				"The cedente was already registered in QI Tech (status {0}). Nothing was sent."
			).format(existing.get("status") or "-"))
		answer = self.client.register_assignor(payload)
		key = answer.get("assignor_registry_key")
		if not key:
			return RegisterOutcome(SENT, None, _(
				"Cedente registration sent to QI Tech without a registration key in the answer: confirm it in QI Tech."
			))
		return RegisterOutcome(CREATED, key, _(
			"Cedente registered in QI Tech (status {0}). It operates only after QI Tech's analysis: send the documents "
			"and start the analysis in QI Tech, then sign the assignment contract."
		).format(answer.get("status") or "-"), {"qitech_status": answer.get("status")})
