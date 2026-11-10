"""Party role of an organization (credtech#367). Pure part runs with pytest; the bench part needs Frappe:
`python -m pytest cred_hub/tests/test_party_roles.py` / `bench --site <site> run-tests --module cred_hub.tests.test_party_roles`."""

import unittest

from cred_hub.party.roles import BOTH, CEDENTE, DEBTOR, has_role, merge


class TestMerge(unittest.TestCase):
	def test_merge(self):
		self.assertEqual(merge(None, CEDENTE), CEDENTE)
		self.assertEqual(merge("", DEBTOR), DEBTOR)
		self.assertEqual(merge(DEBTOR, DEBTOR), DEBTOR)
		self.assertEqual(merge(DEBTOR, CEDENTE), BOTH)
		self.assertEqual(merge(CEDENTE, DEBTOR), BOTH)
		self.assertEqual(merge(BOTH, CEDENTE), BOTH)
		self.assertEqual(merge(CEDENTE, BOTH), BOTH)
		with self.assertRaises(ValueError):
			merge(None, "Investor")

	def test_has_role(self):
		self.assertTrue(has_role(BOTH, CEDENTE))
		self.assertTrue(has_role(DEBTOR, DEBTOR))
		self.assertFalse(has_role(CEDENTE, DEBTOR))
		self.assertFalse(has_role(None, DEBTOR))


try:
	import frappe
	from frappe.tests import IntegrationTestCase
except ImportError:  # plain pytest without Frappe
	frappe = None

if frappe:

	class TestPartyRoleSite(IntegrationTestCase):
		def tearDown(self):
			frappe.db.rollback()

		def org(self, name):
			return frappe.get_doc({"doctype": "CRM Organization", "organization_name": name}).insert().name

		def test_field_is_in_the_side_panel_under_the_name(self):
			import json

			self.assertTrue(frappe.get_meta("CRM Organization").has_field("party_role"))
			layout = frappe.db.get_value("CRM Fields Layout", "CRM Organization-Side Panel", "layout")
			if layout:
				fields = json.loads(layout)[0]["columns"][0]["fields"]
				self.assertEqual(fields[fields.index("organization_name") + 1], "party_role")

		def test_backfill_from_collection_cards_and_titles(self):
			from cred_hub.integrations import SETTINGS, protest
			from cred_hub.party.roles import FIELD, backfill

			settings = frappe.get_single(SETTINGS)
			if not settings.collection_pipeline:
				protest.create_collection_pipeline(settings)
				settings.save()
			pipeline = frappe.get_single(SETTINGS).collection_pipeline
			debtor, cedente, other = self.org("_T Debtor 367"), self.org("_T Cedente 367"), self.org("_T Other 367")
			deal = frappe.get_doc({
				"doctype": "CRM Deal", "pipeline": pipeline, "organization": debtor, "second_organization": cedente,
			})
			deal.append("titles", {"title_number": "NF-1", "due_date": "2026-01-10", "original_amount": 100, "cedente": other})
			deal.insert(ignore_permissions=True)
			backfill()
			roles = {name: frappe.db.get_value("CRM Organization", name, FIELD) for name in (debtor, cedente, other)}
			self.assertEqual(roles, {debtor: DEBTOR, cedente: CEDENTE, other: CEDENTE})
