"""Sincronização e escala da API por chave (auditoria 11/11/2026, credtech#375): cursor no SQL, parâmetros inválidos
com 400, lote contando por registro, comentários só de registros da API, auditoria só de escritas e erros, envelope de
erro e índices. Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_api_sync`."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import Headers
from werkzeug.wrappers import Response

from cred_hub import api_keys, timeline

NOTE = "FCRM Note"


class TestApiSync(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline({"pipeline_name": "Sync", "stages": [{"deal_status": "Novo"}]})["name"]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()

	def create(self, scopes):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key("Sync", list(scopes), [self.pipeline])

	def call(self, token, path, method="GET", form=None):
		request = MagicMock(path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}))
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, form or {}, clear=True),
		):
			frappe.local.request_ip = "203.0.113.9"
			api_keys.authenticate()

	def finish(self, path, method, status=200, response=None):
		with patch.object(frappe, "request", MagicMock(path=path, method=method)):
			api_keys.after_request(response or MagicMock(status_code=status))

	# ---------- H3 ----------

	def test_cursor_walks_every_record_once_past_the_old_window(self):
		"""300 registros, 200 com o mesmo `modified`: o cursor antigo perdia o que passava de limit+200."""
		tag = frappe.generate_hash(length=8)
		same = "2026-11-01 10:00:00.000000"
		names = []
		for i in range(300):
			doc = frappe.get_doc({"doctype": NOTE, "title": f"{tag}-{i:03d}", "content": "x"})
			doc.db_insert()
			names.append(doc.name)
		frappe.db.sql(
			f"update `tab{NOTE}` set modified=%s where title like %s and title < %s", (same, f"{tag}-%", f"{tag}-200")
		)
		seen, cursor = [], None
		for _page in range(20):
			page = api_keys.list_records_cursor(NOTE, filters={"title": ["like", f"{tag}-%"]}, limit=50, cursor=cursor)
			seen += [r["name"] for r in page["data"]]
			cursor = page["next_cursor"]
			if not page["has_more"]:
				break
		self.assertEqual(len(seen), 300)
		self.assertEqual(set(seen), set(names))

	def test_invalid_parameters_answer_400(self):
		for kwargs in (
			{"updated_since": "ontem"},
			{"fields": '["name", "nao_existe"]'},
			{"fields": '{"a": 1}'},
			{"cursor": "nao-e-cursor"},
			{"filters": "{quebrado"},
		):
			with self.subTest(kwargs=kwargs), self.assertRaises(api_keys.InvalidParameter) as ctx:
				api_keys.list_records_cursor("CRM Deal", **kwargs)
			self.assertEqual(ctx.exception.http_status_code, 400)

	# ---------- L4, L5 ----------

	def test_batch_counts_one_write_per_record(self):
		token = self.create(("cards_read", "cards_write"))["token"]
		records = frappe.as_json([{"pipeline": self.pipeline}] * 50)
		form = {"doctype": "CRM Deal", "records": records}
		self.call(token, "/api/method/cred_hub.bulk.bulk_upsert", "POST", form)
		with self.assertRaises(frappe.RateLimitExceededError):
			self.call(token, "/api/method/cred_hub.bulk.bulk_upsert", "POST", form)  # 100 escritas > 60

	def test_comments_only_of_api_records_and_clamped_limit(self):
		token = self.create(("comments_read", "cards_read"))["token"]
		self.call(token, "/api/method/cred_hub.timeline.get_comments")
		with self.assertRaises(frappe.PermissionError):
			timeline.get_comments("User", "Administrator")
		deal = frappe.get_all("CRM Deal", {"pipeline": self.pipeline}, pluck="name", limit=1)
		if deal:
			self.assertIsInstance(timeline.get_comments("CRM Deal", deal[0], limit=-5), list)

	# ---------- M12, L2 ----------

	def test_reads_are_not_logged_and_last_use_is_throttled(self):
		created = self.create(("cards_read", "cards_write"))
		frappe.cache.delete_value(f"credhub_api_last_used:{created['name']}")
		self.call(created["token"], "/api/resource/CRM Deal")
		self.finish("/api/resource/CRM Deal", "GET")
		self.assertTrue(frappe.db.get_value(api_keys.KEY, created["name"], "last_used_on"))
		frappe.db.set_value(api_keys.KEY, created["name"], "last_used_on", "2026-01-01 00:00:00")
		self.call(created["token"], "/api/resource/CRM Deal")
		self.finish("/api/resource/CRM Deal", "GET")
		self.assertEqual(str(frappe.db.get_value(api_keys.KEY, created["name"], "last_used_on")), "2026-01-01 00:00:00")
		self.assertFalse(frappe.db.count(api_keys.LOG, {"api_key": created["name"]}))
		self.call(created["token"], "/api/resource/CRM Deal")
		self.finish("/api/resource/CRM Deal", "GET", status=404)  # erro de leitura é registrado
		self.assertEqual(frappe.db.count(api_keys.LOG, {"api_key": created["name"], "status_code": 404}), 1)

	def test_errors_carry_an_envelope_with_request_id(self):
		token = self.create(("cards_read",))["token"]
		self.call(token, "/api/resource/CRM Deal")
		request_id = frappe.local.credhub_api_key["request_id"]
		body = {
			"exc_type": "PermissionError",
			"_server_messages": json.dumps([json.dumps({"message": "<b>Not permitted</b>"})]),
		}
		response = Response(json.dumps(body), status=403, mimetype="application/json")
		self.finish("/api/resource/CRM Deal", "GET", response=response)
		payload = json.loads(response.get_data(as_text=True))
		self.assertEqual(
			payload["error"], {"code": "PermissionError", "message": "Not permitted", "request_id": request_id}
		)
		self.assertEqual(payload["exc_type"], "PermissionError")  # as chaves do Frappe continuam

	# ---------- M15 ----------

	def test_sync_indexes_exist(self):
		# created by after_migrate (DDL cannot run inside the test transaction)
		for doctype, columns in api_keys.SYNC_INDEXES:
			with self.subTest(doctype=doctype):
				self.assertTrue(frappe.db.has_index(f"tab{doctype}", "credhub_" + "_".join(columns)))
