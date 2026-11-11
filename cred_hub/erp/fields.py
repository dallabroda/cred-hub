"""Custom fields that the ERP collection sync writes on each título of a card (credtech#367).

CRM Deal Title is a child table of the CRM fork; the fields are added from cred_hub like the other integrations.
Values stay in English (pt-BR in the .po).
"""

PAID, REPURCHASED, EXTENDED = "Paid by debtor", "Repurchased by cedente", "Extended"
CLOSED_REASONS = (PAID, REPURCHASED, EXTENDED)

TITLE_FIELDS = [
	{
		"fieldname": "sacado",
		"fieldtype": "Link",
		"label": "Sacado",
		"options": "CRM Organization",
		"insert_after": "cedente",
		"description": "Who must pay this título (a card grouped by cedente has títulos of several sacados).",
	},
	{
		"fieldname": "sacado_contact",
		"fieldtype": "Link",
		"label": "Sacado (person)",
		"options": "Contact",
		"insert_after": "sacado",
	},
	{"fieldname": "erp_section", "fieldtype": "Section Break", "label": "ERP", "insert_after": "promise_amount", "collapsible": 1},
	{"fieldname": "erp_source", "fieldtype": "Data", "label": "ERP", "read_only": 1, "insert_after": "erp_section"},
	{
		"fieldname": "erp_title_id",
		"fieldtype": "Data",
		"label": "ERP título ID",
		"read_only": 1,
		"search_index": 1,
		"insert_after": "erp_source",
	},
	{"fieldname": "erp_operation", "fieldtype": "Data", "label": "ERP operation", "read_only": 1, "insert_after": "erp_title_id"},
	{"fieldname": "erp_situation", "fieldtype": "Data", "label": "ERP situation", "read_only": 1, "insert_after": "erp_operation"},
	{
		"fieldname": "erp_title_type",
		"fieldtype": "Data",
		"label": "Type in the ERP",
		"read_only": 1,
		"insert_after": "erp_situation",
		"description": "The ERP's own type of título (e.g. DMR, NPP). Not the protest species, which the operator picks.",
	},
	{
		"fieldname": "erp_amount",
		"fieldtype": "Currency",
		"label": "Face value in the ERP",
		"read_only": 1,
		"insert_after": "erp_situation",
		"description": "Face value the ERP reports for the título (the ERP does not report an open balance).",
	},
	{
		"fieldname": "closed_reason",
		"fieldtype": "Select",
		"label": "Closed because",
		"options": "\n" + "\n".join(CLOSED_REASONS),
		"read_only": 1,
		"insert_after": "erp_amount",
	},
	{"fieldname": "closed_on", "fieldtype": "Date", "label": "Closed on", "read_only": 1, "insert_after": "closed_reason"},
	{
		"fieldname": "erp_run_id",
		"fieldtype": "Data",
		"label": "ERP sync run",
		"read_only": 1,
		"search_index": 1,
		"insert_after": "closed_on",
		"description": "The synchronization run that created this título (to undo a whole run).",
	},
]

DEAL_FIELDS = [
	{
		"fieldname": "erp_run_id",
		"fieldtype": "Data",
		"label": "ERP sync run",
		"read_only": 1,
		"search_index": 1,
		"in_standard_filter": 1,
		"description": "The synchronization run that created this card: filter by it to undo a whole run.",
	}
]


def erp_custom_fields() -> dict:
	return {"CRM Deal Title": TITLE_FIELDS, "CRM Deal": DEAL_FIELDS}
