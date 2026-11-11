"""The single contract every ERP / external system connector implements (ADR 0007, credtech#375).

The shared code (settings screen, de-para, collection sync, cedente registration) never asks which system it is
talking to: it asks the connector what the system can do (`capabilities`) and calls the contract. A new system is a
new connector registered here, never a new `if provider == ...` in the shared code.

Pure Python (no Frappe at import time), so the conformance kit runs without a site.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

# Results of register_cedente
CREATED = "created"  # the system registered it now and gave its code
SENT = "sent"  # the system accepted the request and registers it later (no code yet)
ALREADY = "already"  # it was already registered there: nothing was sent
PENDING = "pending"  # sent before and still waiting for the system: nothing was sent again
OUTCOMES = (CREATED, SENT, ALREADY, PENDING)

# The common título shape every connector yields (the collection sync reads only these keys; `raw` feeds the de-para)
TITLE_KEYS = frozenset({
	"erp", "erp_id", "operation", "number", "our_number", "kind", "sacado_document", "sacado_name",
	"cedente_document", "cedente_name", "issue_date", "original_due_date", "due_date", "amount", "situation",
	"outcome", "digitable_line", "paid_date", "paid_amount", "raw",
})
TITLE_OUTCOMES = ("open", "paid", "repurchased")

# Kinds of ERP registry rows the de-para reads (same values as import_map)
TITLE, SACADO, CEDENTE = "title", "sacado", "cedente"


@dataclass(frozen=True)
class Credential:
	"""One access field the admin fills for this system. The screen builds the form from this list."""

	fieldname: str
	label: str
	secret: bool = False
	required: bool = True
	description: str = ""


@dataclass(frozen=True)
class Capabilities:
	"""What the system offers. The shared code only uses what is declared here."""

	register_cedente: bool = False
	overdue_titles: bool = False
	settled_titles: bool = False
	sacado_registry: bool = False
	cedente_registry: bool = False
	# Named lists (managers, sectors, operators...) that cedente fields pick by name
	lists: bool = False


@dataclass(frozen=True)
class Pace:
	"""Limits of the system, respected by the shared code."""

	# Minimum seconds between two requests of the same heavy report (0 = none)
	report_cooldown: int = 0


@dataclass
class RegisterOutcome:
	status: str
	code: str | None = None
	note: str = ""
	extra: dict = field(default_factory=dict)

	def __post_init__(self):
		if self.status not in OUTCOMES:
			raise ValueError(f"unknown register outcome {self.status!r}")

	def as_dict(self) -> dict:
		return {"status": self.status, "id": self.code, "note": self.note, **self.extra}


class Connector:
	"""Base of every connector. Class attributes describe the system; an instance is bound to one ERP client.

	Subclasses set `key`, `label`, `credentials`, `capabilities` (and `pace` if any) and implement `make_client`,
	`ping` and every operation their capabilities declare. Undeclared operations raise NotImplementedError.
	"""

	key: str = ""
	label: str = ""
	credentials: tuple[Credential, ...] = ()
	capabilities: Capabilities = Capabilities()
	pace: Pace = Pace()

	def __init__(self, client):
		self.client = client

	# Building ---------------------------------------------------------------------------------------------------
	@classmethod
	def make_client(cls, values: dict, **kwargs):
		"""The system's HTTP client from the credential values ({fieldname: value}); kwargs: session, token,
		on_token (shared token cache)."""
		raise NotImplementedError

	@classmethod
	def missing_credentials(cls, values: dict) -> list[str]:
		return [c.label for c in cls.credentials if c.required and not (values.get(c.fieldname) or "").strip()]

	# Description of the system (no network) ---------------------------------------------------------------------
	@classmethod
	def catalog(cls) -> list[dict]:
		"""Fields the system accepts in the cedente registration (erp_field, label, kind, required, resource...)."""
		return []

	@classmethod
	def default_rules(cls) -> list[dict]:
		"""Suggested de-para for the cedente registration (CRM -> system)."""
		return []

	@classmethod
	def documented_columns(cls, kind: str) -> list[str]:
		"""Columns the system documents for títulos / sacados / cedentes (the de-para also learns the real ones)."""
		return []

	@classmethod
	def fixed_columns(cls, kind: str) -> dict:
		"""Columns the sync fills by itself ({column: what}), shown read-only on the de-para screen."""
		return {}

	def finalize_cedente(self, payload: dict):
		"""Last system-specific touches on the cedente payload after the de-para rules ran."""
		return payload

	# Operations (network) ---------------------------------------------------------------------------------------
	def ping(self) -> dict:
		"""Cheapest call that proves the credentials work."""
		raise NotImplementedError

	def overdue_titles(self, until) -> Iterable[dict]:
		"""Open títulos due on or before `until`, in the common título shape (raw row in `raw`)."""
		raise NotImplementedError

	def settled_titles(self, since, until) -> Iterable[dict]:
		"""Títulos settled between the two dates, in the common título shape."""
		raise NotImplementedError

	def sacados(self) -> Iterable[dict]:
		"""Raw rows of the sacado registry (read by the de-para)."""
		raise NotImplementedError

	def cedentes(self) -> Iterable[dict]:
		"""Raw rows of the cedente registry (read by the de-para)."""
		raise NotImplementedError

	def lists(self, resource: str) -> list[str]:
		"""Names in one of the system's lists (the admin picks by name, never by code)."""
		raise NotImplementedError

	def register_cedente(self, context) -> RegisterOutcome:
		"""Registers the cedente built by the de-para. `context` carries the settings, card, organization and the
		mapper result. Must never send twice the same company (check first, then send)."""
		raise NotImplementedError

	def reader(self, kind: str):
		"""The registry reader for a party kind (sacado / cedente)."""
		return {SACADO: self.sacados, CEDENTE: self.cedentes}[kind]


# Registry -------------------------------------------------------------------------------------------------------
_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
	if not cls.key or not cls.label:
		raise ValueError(f"{cls.__name__} needs a key and a label")
	if cls.key in _REGISTRY and _REGISTRY[cls.key] is not cls:
		raise ValueError(f"connector {cls.key!r} registered twice")
	_REGISTRY[cls.key] = cls
	return cls


def get(key: str | None) -> type[Connector] | None:
	return _REGISTRY.get(key or "")


def keys() -> tuple[str, ...]:
	return tuple(_REGISTRY)


def all_connectors() -> list[type[Connector]]:
	return list(_REGISTRY.values())
