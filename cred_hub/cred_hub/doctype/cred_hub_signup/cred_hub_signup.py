# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CredHubSignup(Document):
	# Created and moved through its states only by cred_hub.signup (public signup API and the provisioning job)
	pass
