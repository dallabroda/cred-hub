"""WBA connector (credtech#367 -> contract of ADR 0007).

WBA registers cedentes and reads receivables + the accounting ledger. Its collection sync (overdue + settlements
from the ledger, cred_hub/erp/wba_ledger.py) is not wired yet, so those capabilities stay off until it is.
"""

from .. import WBA
from ..wba import WbaClient
from .base import Capabilities, Connector, Credential, RegisterOutcome, register


@register
class WbaConnector(Connector):
	key = WBA
	label = "WBA"
	credentials = (
		Credential("wba_base_url", "WBA API URL", required=False),
		Credential("wba_tenant_id", "WBA Tenant ID"),
		Credential("wba_username", "WBA API User"),
		Credential("wba_password", "WBA API Password", secret=True),
	)
	capabilities = Capabilities(register_cedente=True)

	@classmethod
	def make_client(cls, values: dict, **kwargs):
		return WbaClient(
			values.get("wba_tenant_id"),
			values.get("wba_username"),
			values.get("wba_password"),
			base_url=values.get("wba_base_url"),
			**kwargs,
		)

	@classmethod
	def catalog(cls) -> list[dict]:
		from ..catalog import CATALOG

		return CATALOG[WBA]

	@classmethod
	def default_rules(cls) -> list[dict]:
		from ..mapper import DEFAULT_RULES

		return DEFAULT_RULES[WBA]

	def finalize_cedente(self, payload: dict):
		from ..mapper import finalize

		return finalize(WBA, payload)

	def ping(self) -> dict:
		return self.client.ping()

	def register_cedente(self, context) -> RegisterOutcome:
		from ..cedente import _send_wba
		from .smart import _outcome

		return _outcome(_send_wba(self.client, context.organization, context.result))
