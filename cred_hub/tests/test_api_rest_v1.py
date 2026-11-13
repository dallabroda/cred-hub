"""Unit and routing tests for HubTech CRM Sovereign REST `/api/v1/*` and unified JSON error envelope."""

import json
import unittest
from unittest.mock import patch

from werkzeug.wrappers import Response

from cred_hub import api_schema, rest_v1


class TestSovereignRestV1Routing(unittest.TestCase):
	def test_is_sovereign_v1_path_distinguishes_legacy_submounts(self):
		self.assertTrue(rest_v1.is_sovereign_v1_path("/api/v1/deals"))
		self.assertTrue(rest_v1.is_sovereign_v1_path("/api/v1/deals/CRM-DEAL-001/move"))
		self.assertTrue(rest_v1.is_sovereign_v1_path("/api/v1/organizations/Acme Distribuidora S/A"))
		self.assertTrue(rest_v1.is_sovereign_v1_path("/api/v1/openapi.json"))
		self.assertFalse(rest_v1.is_sovereign_v1_path("/api/method/crm.api.pipeline.move_card"))
		self.assertFalse(rest_v1.is_sovereign_v1_path("/api/resource/CRM Deal"))
		self.assertFalse(rest_v1.is_sovereign_v1_path("/api/v1/method/ping"))
		self.assertFalse(rest_v1.is_sovereign_v1_path("/api/v1/resource/CRM Deal"))
		self.assertFalse(rest_v1.is_sovereign_v1_path("/api-docs"))

	def test_resource_slug_resolution_hides_internal_doctypes(self):
		self.assertEqual(rest_v1.resolve_doctype("deals"), "CRM Deal")
		self.assertEqual(rest_v1.resolve_doctype("cards"), "CRM Deal")
		self.assertEqual(rest_v1.resolve_doctype("organizations"), "CRM Organization")
		self.assertEqual(rest_v1.resolve_doctype("contacts"), "Contact")
		self.assertEqual(rest_v1.resolve_doctype("leads"), "CRM Lead")
		self.assertEqual(rest_v1.resolve_doctype("notes"), "FCRM Note")
		self.assertEqual(rest_v1.resolve_doctype("tasks"), "CRM Task")
		self.assertEqual(rest_v1.resolve_doctype("files"), "File")
		# Direct doctype remains backward-compatible
		self.assertEqual(rest_v1.resolve_doctype("CRM Deal"), "CRM Deal")

	def test_resolve_for_auth_maps_every_v1_domain_cleanly(self):
		cases = [
			("GET", "/api/v1/deals", ("resource", "CRM Deal", "GET", None)),
			("POST", "/api/v1/deals", ("resource", "CRM Deal", "POST", None)),
			("POST", "/api/v1/deals/upsert", ("method", "cred_hub.api_keys.upsert_record", "POST", None)),
			("PATCH", "/api/v1/deals/CRM-DEAL-0001", ("resource", "CRM Deal", "PATCH", "CRM-DEAL-0001")),
			("DELETE", "/api/v1/deals/CRM-DEAL-0001", ("resource", "CRM Deal", "DELETE", "CRM-DEAL-0001")),
			("POST", "/api/v1/deals/CRM-DEAL-0001/move", ("method", "crm.api.pipeline.move_card", "POST", "CRM-DEAL-0001")),
			("POST", "/api/v1/deals/CRM-DEAL-0001/pipeline", ("method", "crm.api.pipeline.change_pipeline", "POST", "CRM-DEAL-0001")),
			("GET", "/api/v1/deals/CRM-DEAL-0001/contacts", ("method", "crm.api.card_contacts.get_card_contacts", "GET", "CRM-DEAL-0001")),
			("GET", "/api/v1/deals/CRM-DEAL-0001/comments", ("method", "cred_hub.timeline.get_comments", "GET", "CRM-DEAL-0001")),
			("POST", "/api/v1/deals/CRM-DEAL-0001/comments", ("method", "cred_hub.timeline.add_comment", "POST", "CRM-DEAL-0001")),
			# Organization names containing slashes ("S/A") must resolve cleanly
			("GET", "/api/v1/organizations/Acme Distribuidora S/A", ("resource", "CRM Organization", "GET", "Acme Distribuidora S/A")),
			("POST", "/api/v1/organizations/Acme Distribuidora S/A/comments", ("method", "cred_hub.timeline.add_comment", "POST", "Acme Distribuidora S/A")),
			("POST", "/api/v1/files", ("method", "upload_file", "POST", None)),
			("GET", "/api/v1/files", ("resource", "File", "GET", None)),
			("GET", "/api/v1/pipelines", ("method", "crm.api.pipeline.get_pipelines", "GET", None)),
			("PUT", "/api/v1/pipelines/credito", ("method", "crm.api.pipeline.save_pipeline", "POST", "credito")),
			("POST", "/api/v1/fields", ("method", "crm.api.forms.create_field", "POST", None)),
			("PATCH", "/api/v1/fields/ch_faturamento", ("method", "crm.api.forms.update_field", "POST", "ch_faturamento")),
			("DELETE", "/api/v1/fields/ch_faturamento", ("method", "crm.api.forms.disable_field", "POST", "ch_faturamento")),
			("GET", "/api/v1/lookup", ("method", "cred_hub.api_keys.lookup_by_document", "GET", None)),
			("GET", "/api/v1/sync/records", ("method", "cred_hub.api_keys.list_records_cursor", "GET", None)),
			("GET", "/api/v1/sync/deleted", ("method", "cred_hub.sync.get_deleted_records", "GET", None)),
			("POST", "/api/v1/bulk/upsert", ("method", "cred_hub.bulk.bulk_upsert", "POST", None)),
			("POST", "/api/v1/bulk/delete", ("method", "cred_hub.bulk.bulk_delete", "POST", None)),
			("GET", "/api/v1/credit/checklist", ("method", "cred_hub.credit_check.api.get_document_checklist", "GET", None)),
			("POST", "/api/v1/credit/checklist/upload", ("method", "cred_hub.credit_check.api.upload_checklist_document", "POST", None)),
			("POST", "/api/v1/credit/checks", ("method", "cred_hub.credit_check.api.check_deal", "POST", None)),
			("POST", "/api/v1/erp/titles", ("method", "cred_hub.erp.push.upsert_titles", "POST", None)),
			("POST", "/api/v1/erp/settlements", ("method", "cred_hub.erp.push.settle_titles", "POST", None)),
			("POST", "/api/v1/erp/registrations", ("method", "cred_hub.erp.push.register_result", "POST", None)),
			("GET", "/api/v1/openapi.json", ("method", "cred_hub.api_keys.get_openapi_spec", "GET", None)),
			("GET", "/api/v1/postman.json", ("method", "cred_hub.api_keys.get_postman_collection", "GET", None)),
		]
		for verb, path, expected in cases:
			with self.subTest(verb=verb, path=path):
				self.assertEqual(rest_v1.resolve_for_auth(path, verb), expected)

	def test_unknown_path_raises_404_and_wrong_verb_raises_405(self):
		with self.assertRaises(Exception) as ctx_404:
			rest_v1.match_v1("/api/v1/does-not-exist", "GET")
		self.assertEqual(getattr(ctx_404.exception, "http_status_code", None), 404)

		with self.assertRaises(rest_v1.MethodNotAllowedError) as ctx_405:
			rest_v1.match_v1("/api/v1/lookup", "DELETE")
		self.assertEqual(ctx_405.exception.http_status_code, 405)


class TestSovereignV1ErrorEnvelope(unittest.TestCase):
	def test_417_validation_error_becomes_clean_422_without_legacy_fields(self):
		legacy_body = {
			"exc_type": "ValidationError",
			"exception": "frappe.exceptions.ValidationError: Provide a valid CPF (11 digits) or CNPJ (14 digits).",
			"exc": '["Traceback (most recent call last):\\n  File \\"frappe/app.py\\"..."]',
			"_server_messages": json.dumps([json.dumps({"message": "<b>Provide a valid CPF (11 digits) or CNPJ (14 digits).</b>"})]),
		}
		resp = Response(json.dumps(legacy_body), status=417, content_type="application/json")
		final_status = rest_v1.clean_v1_error_response(resp, "req_test1234")
		self.assertEqual(final_status, 422)
		self.assertEqual(resp.status_code, 422)
		self.assertEqual(resp.headers.get("X-Request-Id"), "req_test1234")
		self.assertEqual(resp.headers.get("X-HubTech-API-Version"), "1")
		body = json.loads(resp.get_data(as_text=True))
		self.assertEqual(set(body.keys()), {"error"})
		self.assertEqual(
			body["error"],
			{
				"code": "validation_error",
				"type": "ValidationError",
				"message": "Provide a valid CPF (11 digits) or CNPJ (14 digits).",
				"request_id": "req_test1234",
			},
		)
		self.assertNotIn("frappe", resp.get_data(as_text=True).lower())

	def test_html_error_page_from_browser_request_becomes_json_401(self):
		html_resp = Response("<html><body>Session Expired in Frappe</body></html>", status=401, content_type="text/html")
		final_status = rest_v1.clean_v1_error_response(html_resp, "req_auth401")
		self.assertEqual(final_status, 401)
		self.assertEqual(html_resp.mimetype, "application/json")
		body = json.loads(html_resp.get_data(as_text=True))
		self.assertEqual(body["error"]["code"], "authentication_error")
		self.assertEqual(body["error"]["type"], "AuthenticationError")
		self.assertEqual(body["error"]["request_id"], "req_auth401")
		self.assertNotIn("frappe", html_resp.get_data(as_text=True).lower())

	def test_internal_500_never_leaks_tracebacks_or_framework_names(self):
		crash_body = {
			"exc_type": "frappe.db.InternalError",
			"exception": "frappe.exceptions.InternalError: Table tabCRM Deal crashed",
			"exc": "Traceback...",
		}
		resp = Response(json.dumps(crash_body), status=500, content_type="application/json")
		rest_v1.clean_v1_error_response(resp, "req_500")
		body = json.loads(resp.get_data(as_text=True))
		self.assertEqual(
			body,
			{
				"error": {
					"code": "internal_error",
					"type": "InternalServerError",
					"message": "An unexpected internal error occurred.",
					"request_id": "req_500",
				}
			},
		)
		self.assertNotIn("frappe", resp.get_data(as_text=True).lower())


class TestOpenApiAndPostmanV1Alignment(unittest.TestCase):
	def test_shared_schemas_include_error_envelope_and_zero_framework_leakage(self):
		shared = api_schema._shared_schemas()
		self.assertIn("ErrorEnvelope", shared)
		err_props = shared["ErrorEnvelope"]["properties"]["error"]["properties"]
		self.assertEqual(set(err_props.keys()), {"code", "type", "message", "request_id"})
		self.assertNotIn("frappe", json.dumps(shared).lower())

	def test_postman_default_version_is_1_0_0_and_all_v1_paths_have_zero_framework_names(self):
		with patch.dict("sys.modules", {"crm.fcrm.doctype.crm_pipeline.fields": type("M", (), {"FIELD_TYPES": ("Data", "Select")})}):
			v1_paths = api_schema._v1_paths()
		self.assertTrue(len(v1_paths) >= 28)
		for path, item in v1_paths.items():
			self.assertTrue(path.startswith("/api/v1/"), path)
			self.assertNotIn("frappe", path.lower())
			if "{name}" in path:
				self.assertEqual(item["parameters"][0]["name"], "name", path)
		raw = json.dumps(v1_paths)
		self.assertNotIn("frappe", raw.lower())

		spec = {
			"openapi": "3.1.0",
			"info": {"title": "HubTech CRM API", "version": "1.0.0", "x-hubtech-api-version": "1"},
			"paths": v1_paths,
			"webhooks": api_schema._webhook_specs(),
		}
		postman = api_schema.to_postman_collection(spec)
		self.assertEqual(postman["info"]["version"], "1.0.0")
		self.assertNotIn("frappe", json.dumps(postman).lower())
		spec_30 = api_schema.to_openapi_3_0(spec)
		self.assertEqual(spec_30["openapi"], "3.0.3")
		self.assertEqual(spec_30["info"]["version"], "1.0.0")
		self.assertNotIn("frappe", json.dumps(spec_30).lower())


if __name__ == "__main__":
	unittest.main()
