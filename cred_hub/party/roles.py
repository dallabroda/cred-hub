"""Role of an organization in receivables financing: cedente, sacado (debtor) or both (credtech#367).

Stored in the CRM Organization custom field `party_role`. Values stay in English like the rest of the backend
(pt-BR in the .po: Cedente / Sacado / Cedente e sacado). A role is only ever added, never removed by the system:
an organization that was a sacado and now sells receivables becomes "both". The admin can still change it by hand.
"""

FIELD = "party_role"
CEDENTE = "Cedente"
DEBTOR = "Debtor"
BOTH = "Cedente and debtor"
ROLES = (CEDENTE, DEBTOR, BOTH)


def merge(current: str | None, new: str) -> str:
	"""Role after adding `new` to `current` (pure)."""
	if new not in ROLES:
		raise ValueError(f"Unknown party role: {new}")
	if not current or current == new:
		return new
	if current == BOTH or new == BOTH:
		return BOTH
	# One is cedente and the other debtor
	return BOTH


def add_role(organization: str | None, role: str) -> str | None:
	"""Adds `role` to the organization without touching anything else. Returns the resulting role."""
	if not organization:
		return None
	import frappe

	current = frappe.db.get_value("CRM Organization", organization, FIELD)
	result = merge(current, role)
	if result != current:
		frappe.db.set_value("CRM Organization", organization, FIELD, result, update_modified=False)
	return result


def has_role(value: str | None, role: str) -> bool:
	"""True when `value` covers `role` (both covers cedente and debtor)."""
	return bool(value) and (value == role or value == BOTH)


def backfill() -> dict:
	"""One-off fill for sites that already had collection cards: the debtor of each collection card becomes
	sacado; the card's second organization and every título's cedente become cedente."""
	import frappe

	from cred_hub.integrations import get_settings

	counts = {CEDENTE: 0, DEBTOR: 0}
	pipeline = get_settings().get("collection_pipeline")
	if pipeline:
		for row in frappe.get_all(
			"CRM Deal", {"pipeline": pipeline}, ["organization", "second_organization"], limit_page_length=0
		):
			if row.organization:
				add_role(row.organization, DEBTOR)
				counts[DEBTOR] += 1
			if row.second_organization and row.second_organization != row.organization:
				add_role(row.second_organization, CEDENTE)
				counts[CEDENTE] += 1
	if frappe.db.has_column("CRM Deal Title", "cedente"):
		for cedente in frappe.get_all(
			"CRM Deal Title", {"cedente": ("is", "set")}, pluck="cedente", distinct=True, limit_page_length=0
		):
			add_role(cedente, CEDENTE)
			counts[CEDENTE] += 1
	return counts
