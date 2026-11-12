"""Tests for OpenAPI 3.0.3 converter, Postman Collection v2.1 generator, and Webhooks spec (credtech#375)."""

import unittest
from cred_hub.api_schema import _webhook_specs, to_openapi_3_0, to_postman_collection


class TestApiOpenApi30AndPostman(unittest.TestCase):
	def test_webhook_specs_covers_all_ten_events(self):
		webhooks = _webhook_specs()
		expected_events = {
			"deal.created",
			"deal.stage_changed",
			"deal.won",
			"deal.lost",
			"deal.deleted",
			"contact.created",
			"contact.updated",
			"checklist.item_rejected",
			"checklist.item_attached",
			"credit.limit_decided",
		}
		self.assertEqual(set(webhooks.keys()), expected_events)
		for event_name, op in webhooks.items():
			post = op["post"]
			self.assertTrue(post.get("summary"))
			self.assertIn("Webhooks", post.get("tags", []))
			body = post["requestBody"]["content"]["application/json"]["schema"]
			self.assertEqual(body["type"], "object")
			self.assertIn("event", body["properties"])
			self.assertIn("data", body["properties"])
			self.assertIn("timestamp", body["properties"])
			self.assertIn("test", body["properties"])
			self.assertTrue(body["properties"]["test"]["description"])
			self.assertEqual(post["responses"]["200"]["description"], "Webhook received successfully")

	def test_to_openapi_3_0_converts_nullable_types_and_webhooks(self):
		spec_3_1 = {
			"openapi": "3.1.0",
			"info": {"title": "HubTech CRM API", "version": "2.0.0"},
			"webhooks": _webhook_specs(),
			"components": {
				"schemas": {
					"Card": {
						"type": "object",
						"properties": {
							"name": {"type": "string"},
							"custom_field": {"type": ["string", "null"]},
							"count": {"type": ["integer", "null"]},
							"status": {"type": "string", "enum": ["Open", "Won", None]},
						},
					}
				}
			},
			"paths": {
				"/api/resource/CRM Deal": {
					"get": {"summary": "List cards", "responses": {"200": {"description": "ok"}}}
				}
			},
		}

		spec_3_0 = to_openapi_3_0(spec_3_1)
		self.assertEqual(spec_3_0["openapi"], "3.0.3")
		self.assertNotIn("webhooks", spec_3_0)
		self.assertIn("x-webhooks", spec_3_0)
		self.assertEqual(len(spec_3_0["x-webhooks"]), 10)

		props = spec_3_0["components"]["schemas"]["Card"]["properties"]
		self.assertEqual(props["name"]["type"], "string")
		self.assertNotIn("nullable", props["name"])

		self.assertEqual(props["custom_field"]["type"], "string")
		self.assertTrue(props["custom_field"]["nullable"])

		self.assertEqual(props["count"]["type"], "integer")
		self.assertTrue(props["count"]["nullable"])

		self.assertEqual(props["status"]["enum"], ["Open", "Won"])
		self.assertTrue(props["status"]["nullable"])

	def test_to_postman_collection_generates_v21_with_variables_and_headers(self):
		spec = {
			"openapi": "3.1.0",
			"info": {"title": "HubTech CRM API", "version": "2.0.0", "description": "Test doc"},
			"servers": [{"url": "https://test.credtechdigital.com.br"}],
			"paths": {
				"/api/resource/CRM Deal": {
					"get": {
						"summary": "List cards",
						"tags": ["Cards"],
						"parameters": [{"name": "limit", "in": "query", "example": "20", "required": False}],
					},
					"post": {
						"summary": "Create card",
						"tags": ["Cards"],
					},
				},
				"/api/method/cred_hub.api_keys.lookup_by_document": {
					"get": {
						"summary": "Search 360",
						"tags": ["Search"],
						"parameters": [{"name": "document", "in": "query", "example": "11222333000181", "required": True}],
					}
				},
			},
		}

		collection = to_postman_collection(spec)
		self.assertEqual(collection["info"]["name"], "HubTech CRM API")
		self.assertEqual(collection["info"]["schema"], "https://schema.getpostman.com/json/collection/v2.1.0/collection.json")
		self.assertEqual(collection["auth"]["type"], "bearer")

		variables = {v["key"]: v["value"] for v in collection["variable"]}
		self.assertEqual(variables["baseUrl"], "https://test.credtechdigital.com.br")
		self.assertEqual(variables["hubtech_token"], "hub_id_segredo")

		folder_names = [item["name"] for item in collection["item"]]
		self.assertEqual(set(folder_names), {"Cards", "Search"})

		cards_folder = next(item for item in collection["item"] if item["name"] == "Cards")
		self.assertEqual(len(cards_folder["item"]), 2)

		get_req = next(r["request"] for r in cards_folder["item"] if r["request"]["method"] == "GET")
		self.assertEqual(get_req["url"]["raw"], "{{baseUrl}}/api/resource/CRM Deal")
		headers_get = {h["key"]: h["value"] for h in get_req["header"]}
		self.assertEqual(headers_get["Accept"], "application/json")
		self.assertEqual(headers_get["X-HubTech-API-Version"], "1")
		self.assertNotIn("Idempotency-Key", headers_get)

		post_req = next(r["request"] for r in cards_folder["item"] if r["request"]["method"] == "POST")
		headers_post = {h["key"]: h["value"] for h in post_req["header"]}
		self.assertEqual(headers_post["Idempotency-Key"], "{{$guid}}")
		self.assertEqual(headers_post["Content-Type"], "application/json")
		self.assertEqual(headers_post["X-HubTech-API-Version"], "1")


if __name__ == "__main__":
	unittest.main()
