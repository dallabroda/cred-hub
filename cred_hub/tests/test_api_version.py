"""API version header, pinning and deprecation headers (credtech#375, ADR 0007 section 11).
Run: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_api_version`."""

from datetime import date
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import Headers

from cred_hub import api_keys, api_version


class TestApiVersion(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline({"pipeline_name": "API Versao", "stages": [{"deal_status": "Novo"}]})["name"]
		frappe.local.response_headers = Headers()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()

	def create(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key("ERP", ["cards_read", "cards_write", "pipelines_read"], [self.pipeline])["token"]

	def call(self, token, path="/api/resource/CRM Deal", method="GET", headers=None):
		request = MagicMock(
			path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}", **(headers or {})}), files={}
		)
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		frappe.local.response_headers = Headers()
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, {}, clear=True),
		):
			frappe.local.request_ip = "203.0.113.9"
			api_keys.authenticate()
		return frappe.local.response_headers

	def test_every_key_call_answers_the_version(self):
		headers = self.call(self.create())
		self.assertEqual(headers.get(api_version.HEADER), "1")
		self.assertEqual(frappe.local.credhub_api_key["api_version"], "1")
		self.assertIsNone(headers.get("Deprecation"))

	def test_version_header_also_on_refused_calls(self):
		with self.assertRaises(frappe.AuthenticationError):
			self.call("hub_000000000000_wrong")
		self.assertEqual(frappe.local.response_headers.get(api_version.HEADER), "1")

	def test_pinning_version_one_works(self):
		headers = self.call(self.create(), headers={api_version.HEADER: "1"})
		self.assertEqual(headers.get(api_version.HEADER), "1")

	def test_unknown_version_is_refused_with_400_after_auth(self):
		token = self.create()
		for value in ("2", "0", "v1", "1.0", "x" * 300):
			with self.assertRaises(api_version.UnsupportedApiVersion) as ctx:
				self.call(token, headers={api_version.HEADER: value})
			self.assertEqual(ctx.exception.http_status_code, 400)
			# the key was verified first: the error is logged and wrapped like any other key error
			self.assertTrue(frappe.local.credhub_api_key)

	def test_deprecated_route_gets_deprecation_sunset_and_link(self):
		path = "/api/resource/CRM Deal"
		entry = {"GET " + path: {"deprecated_on": "2026-01-01", "sunset_on": "2099-12-31"}}
		with patch.dict(api_version.DEPRECATED_ROUTES, entry, clear=True):
			headers = self.call(self.create(), path=path)
		self.assertEqual(headers.get("Deprecation"), "@1767225600")
		self.assertEqual(headers.get("Sunset"), "Thu, 31 Dec 2099 00:00:00 GMT")
		self.assertIn('rel="deprecation"', headers.get("Link"))
		self.assertIn("/api-docs", headers.get("Link"))

	def test_deprecation_headers_only_inside_the_window(self):
		today = date(2026, 11, 11)
		self.assertEqual(api_version.deprecation_headers("2026-12-01", "2027-03-01", today), {})
		self.assertEqual(api_version.deprecation_headers("2026-01-01", "2026-06-01", today), {})
		inside = api_version.deprecation_headers("2026-11-01", "2027-02-01", today)
		self.assertEqual(set(inside), {"Deprecation", "Sunset", "Link"})
		self.assertNotIn("Sunset", api_version.deprecation_headers("2026-11-01", None, today))

	def test_old_user_keys_announce_their_sunset(self):
		frappe.set_user("Administrator")
		frappe.local.response_headers = Headers()
		frappe.set_user("Guest")
		frappe.local.session.user = "seller-legacy@example.com"
		with patch.dict(frappe.conf, {"credhub_legacy_token_until": "2099-12-31"}):
			api_keys._check_legacy_token()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.local.response_headers.get("Sunset"), "Thu, 31 Dec 2099 00:00:00 GMT")
		self.assertTrue(frappe.local.response_headers.get("Deprecation").startswith("@"))

	def test_openapi_states_the_version(self):
		from cred_hub.api_schema import build_openapi_spec

		info = build_openapi_spec()["info"]
		self.assertEqual(info["x-hubtech-api-version"], "1")
		self.assertIn("X-HubTech-API-Version", info["description"])

	def test_policy_constants(self):
		self.assertEqual(api_version.SUPPORTED, ("1",))
		self.assertGreaterEqual(api_version.MIN_NOTICE_DAYS, 90)
