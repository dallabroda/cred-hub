# Copyright (c) 2026, CredTech Digital and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CredHubCollectionReminders(Document):
	# Collection reminders (credtech#367): cred_hub.collection.reminders
	def validate(self):
		from cred_hub.collection import reminders

		reminders.validate_settings(self)
