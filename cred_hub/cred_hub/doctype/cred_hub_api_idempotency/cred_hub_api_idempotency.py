# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CredHubAPIIdempotency(Document):
	# Written only by cred_hub.api_idempotency (raw insert keyed by the key + Idempotency-Key digest, credtech#375)
	pass
