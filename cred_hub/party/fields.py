"""Custom field of the party role on CRM Organization (credtech#367)."""

from cred_hub.party.roles import FIELD, ROLES

ORGANIZATION_FIELDS = [
	{
		"fieldname": FIELD,
		"fieldtype": "Select",
		"label": "Party role",
		"options": "\n" + "\n".join(ROLES),
		"insert_after": "organization_name",
		"in_standard_filter": 1,
		"in_list_view": 1,
		"description": "Cedente sells receivables; sacado (debtor) pays them. Filled automatically by collection "
		"imports and the ERP integration; the system only adds roles, never removes them.",
	}
]


def party_custom_fields() -> dict:
	return {"CRM Organization": ORGANIZATION_FIELDS}
