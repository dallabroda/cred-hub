"""Idempotency-Key on writes made through an API key (credtech#375, ADR 0007 section 11).
Run: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_api_idempotency`."""

import io
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime
from werkzeug.datastructures import FileStorage, Headers, MultiDict
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response

from cred_hub import api_idempotency as idem
from cred_hub import api_keys

DEALS = "/api/resource/CRM Deal"


class TestApiIdempotency(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline({"pipeline_name": "API Idem", "stages": [{"deal_status": "Novo"}]})["name"]
		frappe.local.response_headers = Headers()
		self.token = self.create()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()

	def create(self, label="ERP"):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		scopes = ["cards_read", "cards_write", "pipelines_read", "files_write", "allow_delete"]
		return api_keys.create_api_key(label, scopes, [self.pipeline])["token"]

	def call(self, path=DEALS, method="POST", form=None, key=None, token=None, files=None, respond=None):
		"""authenticate as in a real call; with `respond` (status, body) also runs after_request with that answer.
		Returns the claim, or the replayed Response."""
		headers = {"Authorization": f"Bearer {token or self.token}"}
		if key is not None:
			headers[idem.HEADER] = key
		request = MagicMock(path=path, method=method, headers=Headers(headers), files=files or MultiDict())
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		frappe.local.response_headers = Headers()
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, form if form is not None else {"pipeline": self.pipeline}, clear=True),
		):
			frappe.local.request_ip = "203.0.113.9"
			try:
				api_keys.authenticate()
			except HTTPException as exc:
				api_keys.after_request(response=exc.response, request=request)
				return exc.response
			claim = frappe.local.credhub_api_key.get("idempotency")
			if respond:
				status, body = respond
				response = Response(json.dumps(body), status=status, content_type="application/json")
				api_keys.after_request(response=response, request=request)
		return claim

	def row(self, key, token=None):
		api_key = self.key_name(token or self.token)
		return frappe.db.get_value(idem.DOCTYPE, idem.row_name(api_key, key), "*", as_dict=True)

	def key_name(self, token):
		return frappe.db.get_value(api_keys.KEY, {"key_id": token.split("_")[1]}, "name")

	# ---------- Header ----------

	def test_without_header_nothing_is_stored(self):
		self.assertIsNone(self.call(respond=(200, {"data": {"name": "D1"}})))
		self.assertEqual(frappe.db.count(idem.DOCTYPE), 0)

	def test_reads_ignore_the_header(self):
		self.assertIsNone(self.call(method="GET", form={}, key="read-1"))
		self.assertIsNone(self.row("read-1"))

	def test_invalid_keys_are_refused_with_400(self):
		for value in ("", "x" * 256, "chave-é", "tab\there", "bell\x07"):
			with self.assertRaises(idem.InvalidIdempotencyKey) as ctx:
				self.call(key=value)
			self.assertEqual(ctx.exception.http_status_code, 400)
		self.call(key="k" * 255)  # the maximum is accepted

	# ---------- Replay ----------

	def test_same_key_same_request_replays_the_first_answer(self):
		claim = self.call(key="order-1", respond=(200, {"data": {"name": "CRM-DEAL-1"}}))
		self.assertTrue(claim["name"])
		row = self.row("order-1")
		self.assertEqual((row.status, row.response_status), ("Completed", 200))

		replay = self.call(key="order-1")
		self.assertIsInstance(replay, Response)
		self.assertEqual(replay.status_code, 200)
		self.assertEqual(replay.headers.get(idem.REPLAY_HEADER), "true")
		self.assertEqual(json.loads(replay.get_data()), {"data": {"name": "CRM-DEAL-1"}})
		# the replay is not stored again and leaves the first answer untouched
		self.assertEqual(self.row("order-1").response_body, row.response_body)

	def test_same_key_different_request_is_refused_with_422(self):
		self.call(key="order-2", respond=(200, {"data": {}}))
		with self.assertRaises(idem.IdempotencyKeyReused) as ctx:
			self.call(key="order-2", form={"pipeline": self.pipeline, "deal_value": 10})
		self.assertEqual(ctx.exception.http_status_code, 422)
		with self.assertRaises(idem.IdempotencyKeyReused):
			self.call(key="order-2", method="PUT", path=DEALS + "/CRM-DEAL-9")

	def test_errors_of_the_call_are_replayed_too(self):
		self.call(key="bad-1", respond=(417, {"exc_type": "MandatoryError"}))
		replay = self.call(key="bad-1")
		self.assertEqual(replay.status_code, 417)
		self.assertEqual(json.loads(replay.get_data())["exc_type"], "MandatoryError")

	def test_calls_that_did_nothing_free_the_key(self):
		for status in (500, 503, 409, 429):
			key = f"retry-{status}"
			self.call(key=key, respond=(status, {}))
			self.assertIsNone(self.row(key), status)
			self.assertTrue(self.call(key=key)["name"])  # the same key runs again

	def test_keys_are_per_api_key(self):
		other = self.create("Outro")
		self.call(key="shared", respond=(200, {"data": {"name": "A"}}))
		claim = self.call(key="shared", token=other, form={"pipeline": self.pipeline, "x": 1})
		self.assertTrue(claim["name"])

	def test_expired_key_is_free_again(self):
		self.call(key="old", respond=(200, {}))
		frappe.db.set_value(idem.DOCTYPE, self.row("old").name, "expires_at", add_to_date(now_datetime(), hours=-1))
		claim = self.call(key="old", form={"pipeline": self.pipeline, "other": "request"})
		self.assertTrue(claim["name"])
		self.assertEqual(self.row("old").status, "Processing")

	def test_large_answer_is_not_kept_but_still_replayed_by_status(self):
		big = {"data": "x" * (idem.MAX_BODY_BYTES + 10)}
		self.call(key="big", respond=(200, big))
		row = self.row("big")
		self.assertEqual((row.body_omitted, row.response_body), (1, None))
		replay = self.call(key="big")
		self.assertEqual(replay.status_code, 200)
		self.assertTrue(json.loads(replay.get_data())["idempotent_replay"]["body_omitted"])

	# ---------- Concurrency ----------

	def test_same_key_in_flight_is_refused_with_409(self):
		first = self.call(key="pay-1")  # claimed, the call is still running (no answer yet)
		self.assertTrue(first["name"])
		with self.assertRaises(idem.IdempotencyKeyInUse) as ctx:
			self.call(key="pay-1")
		self.assertEqual(ctx.exception.http_status_code, 409)
		# once the first one answers, the resend replays it
		idem.finish(first, Response('{"data": 1}', status=201, content_type="application/json"))
		self.assertEqual(self.call(key="pay-1").status_code, 201)

	def test_claim_is_the_primary_key(self):
		"""Two workers racing for one key: the database accepts exactly one claim."""
		name = idem.row_name("k1", "race")
		self.assertTrue(idem._insert(name, self.key_name(self.token), "h", "POST /x", "req_1"))
		self.assertFalse(idem._insert(name, self.key_name(self.token), "h", "POST /x", "req_2"))
		self.assertEqual(frappe.db.count(idem.DOCTYPE, {"name": name}), 1)

	# ---------- What is hashed and stored ----------

	def test_uploaded_file_bytes_are_part_of_the_request(self):
		def upload(content):
			return MultiDict({"file": FileStorage(io.BytesIO(content), filename="a.pdf")})

		form = {"is_private": 1}
		self.call(path="/api/method/upload_file", key="up-1", form=form, files=upload(b"%PDF-1"), respond=(200, {}))
		replay = self.call(path="/api/method/upload_file", key="up-1", form=form, files=upload(b"%PDF-1"))
		self.assertEqual(replay.status_code, 200)
		with self.assertRaises(idem.IdempotencyKeyReused):
			self.call(path="/api/method/upload_file", key="up-1", form=form, files=upload(b"%PDF-2"))

	def test_fingerprint_leaves_the_stream_readable(self):
		storage = FileStorage(io.BytesIO(b"abc"), filename="a.pdf")
		request = MagicMock(path="/api/method/upload_file", method="POST", files=MultiDict({"file": storage}))
		with patch.object(frappe, "request", request):
			idem.request_fingerprint()
		self.assertEqual(storage.stream.read(), b"abc")

	def test_secrets_and_the_key_itself_are_never_stored(self):
		self.call(key="client-secret-looking-key", respond=(200, {"data": {}}))
		row = self.row("client-secret-looking-key")
		stored = frappe.as_json(row)
		self.assertNotIn("client-secret-looking-key", stored)
		self.assertNotIn(self.token.split("_", 2)[2], stored)
		self.assertNotIn("Authorization", stored)

	def test_batch_writes_take_the_key(self):
		form = {"doctype": "CRM Deal", "records": json.dumps([{"pipeline": self.pipeline}])}
		claim = self.call(path="/api/method/cred_hub.bulk.bulk_upsert", key="batch-1", form=form)
		self.assertTrue(claim["name"])

	def test_delete_takes_the_key(self):
		claim = self.call(path=DEALS + "/CRM-DEAL-X", method="DELETE", form={}, key="del-1", respond=(202, {}))
		self.assertTrue(claim["name"])
		self.assertEqual(self.call(path=DEALS + "/CRM-DEAL-X", method="DELETE", form={}, key="del-1").status_code, 202)

	def test_refused_route_never_claims(self):
		with self.assertRaises(api_keys.KeyNotAllowed):
			self.call(path="/api/resource/User", key="nope")
		self.assertEqual(frappe.db.count(idem.DOCTYPE), 0)

	def test_cleanup_removes_only_expired(self):
		self.call(key="keep", respond=(200, {}))
		self.call(key="drop", respond=(200, {}))
		frappe.db.set_value(idem.DOCTYPE, self.row("drop").name, "expires_at", add_to_date(now_datetime(), hours=-25))
		idem.delete_expired()
		self.assertIsNotNone(self.row("keep"))
		self.assertIsNone(self.row("drop"))
		self.assertIn("cred_hub.api_idempotency.delete_expired", frappe.get_hooks("scheduler_events")["hourly"])

	def test_openapi_writes_accept_the_header(self):
		from cred_hub.api_schema import build_openapi_spec

		paths = build_openapi_spec()["paths"]
		post = paths["/api/resource/CRM Deal"]["post"]
		self.assertIn("Idempotency-Key", [p["name"] for p in post["parameters"]])
		self.assertIn("422", post["responses"])
		get_params = [p["name"] for p in paths["/api/resource/CRM Deal"]["get"].get("parameters", [])]
		self.assertNotIn("Idempotency-Key", get_params)
