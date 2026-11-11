"""Smart Securities connector (credtech#367 -> contract of ADR 0007)."""

from .. import SMART
from ..smart import OPEN, OPEN_RANGE_START, PAID, SmartClient, normalize_title
from .base import CEDENTE, SACADO, TITLE, Capabilities, Connector, Credential, Pace, RegisterOutcome, register

# Asking the same Smart report again within 10 minutes restarts its cooldown: never reopen it sooner
REPORT_COOLDOWN = 11 * 60


@register
class SmartConnector(Connector):
	key = SMART
	label = "Smart Securities"
	credentials = (
		Credential("smart_base_url", "Smart API URL", required=False),
		Credential("smart_client_id", "Smart Client ID"),
		Credential("smart_client_secret", "Smart Client Secret", secret=True),
	)
	capabilities = Capabilities(
		register_cedente=True,
		overdue_titles=True,
		settled_titles=True,
		sacado_registry=True,
		cedente_registry=True,
		lists=True,
	)
	pace = Pace(report_cooldown=REPORT_COOLDOWN)

	@classmethod
	def make_client(cls, values: dict, **kwargs):
		return SmartClient(
			values.get("smart_client_id"),
			values.get("smart_client_secret"),
			base_url=values.get("smart_base_url"),
			**kwargs,
		)

	@classmethod
	def catalog(cls) -> list[dict]:
		from ..catalog import SMART_FIELDS

		return SMART_FIELDS

	@classmethod
	def default_rules(cls) -> list[dict]:
		from ..mapper import DEFAULT_RULES

		return DEFAULT_RULES[SMART]

	@classmethod
	def documented_columns(cls, kind: str) -> list[str]:
		from .. import import_map

		return {
			TITLE: import_map.SMART_TITLE_COLUMNS,
			SACADO: import_map.SMART_SACADO_COLUMNS,
			CEDENTE: import_map.SMART_CEDENTE_COLUMNS,
		}.get(kind, [])

	@classmethod
	def default_import_rules(cls, kind: str) -> list[tuple[str, str]]:
		from .. import import_map

		return {TITLE: import_map.DEFAULT_TITLE_MAP.get(SMART), SACADO: import_map.DEFAULT_SACADO_MAP.get(SMART)}.get(kind) or []

	@classmethod
	def fixed_columns(cls, kind: str) -> dict:
		from .. import import_map

		return {
			TITLE: import_map.SMART_TITLE_FIXED,
			SACADO: import_map.SMART_SACADO_FIXED,
			CEDENTE: import_map.SMART_CEDENTE_FIXED,
		}.get(kind, {})

	def finalize_cedente(self, payload: dict):
		from ..mapper import finalize

		return finalize(SMART, payload)

	def ping(self) -> dict:
		return self.client.ping()

	def overdue_titles(self, until):
		for row in self.client.titles(OPEN, vencimentoIni=OPEN_RANGE_START, vencimentoFim=until):
			yield normalize_title(row)

	def settled_titles(self, since, until):
		for row in self.client.titles(PAID, quitacaoIni=since, quitacaoFim=until):
			yield normalize_title(row)

	def sacados(self):
		return self.client.sacados()

	def cedentes(self):
		return self.client.cedentes()

	def lists(self, resource: str) -> list[str]:
		return [row.get("descricao") for row in self.client.lookup(resource) if row.get("descricao")]

	def register_cedente(self, context) -> RegisterOutcome:
		from ..cedente import _send_smart

		answer = _send_smart(self, context.settings, context.deal, context.organization, context.result)
		return RegisterOutcome.from_dict(answer)
