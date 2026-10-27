"""Chaves da API do HubTech (credtech#175): só admin cria, usuário técnico não ocupa vaga, escopos por rota.
Roda no bench: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_api_keys`."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import Headers

from cred_hub import api_keys, billing

SELLER = "crm.user1@example.com"


class TestApiKeys(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{"pipeline_name": "API Liberado", "stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}]}
		)["name"]
		self.other = save_pipeline(
			{"pipeline_name": "API Fechado", "stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}]}
		)["name"]

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()

	def create(self, scopes=("cards_read", "cards_write", "pipelines_read"), **kw):
		return api_keys.create_api_key("ERP", list(scopes), [self.pipeline], **kw)

	def call(self, token, path, method="GET", form=None, ip="203.0.113.9"):
		"""Roda o auth_hook como numa chamada real e devolve o usuário da chamada."""
		request = MagicMock(path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}))
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, form or {}, clear=True),
		):
			frappe.local.request_ip = ip
			api_keys.authenticate()
		return frappe.session.user

	# ---------- Criar e o usuário técnico ----------

	def test_token_shown_once_and_only_hash_stored(self):
		created = self.create()
		self.assertTrue(created["token"].startswith(f"hub_{frappe.db.get_value(api_keys.KEY, created['name'], 'key_id')}_"))
		doc = frappe.get_doc(api_keys.KEY, created["name"])
		secret = created["token"].split("_", 2)[2]
		self.assertNotIn(secret, frappe.as_json(doc.as_dict()))
		self.assertEqual(doc.secret_hint, secret[-4:])
		self.assertNotIn("token", api_keys.get_api_keys()["keys"][0])

	def test_technical_user_takes_no_seat_and_owns_only_its_pipelines(self):
		before = set(billing.active_users())
		user = self.create()
		technical = frappe.db.get_value(api_keys.KEY, user["name"], "technical_user")
		self.assertEqual(set(billing.active_users()), before)
		owners = frappe.get_all("CRM Pipeline Member", {"user": technical}, ["parent", "role"])
		self.assertEqual([(o.parent, o.role) for o in owners], [(self.pipeline, "Owner")])
		self.assertNotIn("System Manager", frappe.get_roles(technical))

	def test_technical_user_ignores_plan_limit(self):
		frappe.flags.credhub_enforce_user_limit = True
		self.addCleanup(setattr, frappe.flags, "credhub_enforce_user_limit", False)
		with patch.object(billing, "user_limit", return_value=0):
			self.create()
			with self.assertRaises(billing.UserLimitError):
				frappe.get_doc({"doctype": "User", "email": "pessoa@example.com", "first_name": "Pessoa"}).insert()

	def test_only_admin_manages_keys(self):
		frappe.set_user(SELLER)
		with self.assertRaises(frappe.PermissionError):
			self.create()
		with self.assertRaises(frappe.PermissionError):
			api_keys.get_api_keys()

	def test_cards_need_a_pipeline(self):
		with self.assertRaises(frappe.ValidationError):
			api_keys.create_api_key("Sem funil", ["cards_read"], [])

	# ---------- Autenticação ----------

	def test_valid_token_runs_as_technical_user(self):
		created = self.create()
		technical = frappe.db.get_value(api_keys.KEY, created["name"], "technical_user")
		self.assertEqual(self.call(created["token"], "/api/resource/CRM Deal"), technical)

	def test_wrong_secret_revoked_and_expired_keys_fail(self):
		created = self.create()
		with self.assertRaises(frappe.AuthenticationError):
			self.call(created["token"][:-2] + "xx", "/api/resource/CRM Deal")
		with self.assertRaises(frappe.AuthenticationError):
			self.call("hub_naoexiste_segredo", "/api/resource/CRM Deal")
		frappe.set_user("Administrator")
		api_keys.revoke_api_key(created["name"])
		with self.assertRaises(frappe.AuthenticationError):
			self.call(created["token"], "/api/resource/CRM Deal")

		expired = self.create(expires_on="2020-01-01")
		with self.assertRaises(frappe.AuthenticationError):
			self.call(expired["token"], "/api/resource/CRM Deal")

	def test_revoke_disables_user_and_leaves_pipelines(self):
		created = self.create()
		technical = frappe.db.get_value(api_keys.KEY, created["name"], "technical_user")
		api_keys.revoke_api_key(created["name"])
		self.assertFalse(frappe.db.get_value("User", technical, "enabled"))
		self.assertFalse(frappe.db.exists("CRM Pipeline Member", {"user": technical}))

	def test_rotate_invalidates_old_secret(self):
		created = self.create()
		rotated = api_keys.rotate_api_key(created["name"])
		with self.assertRaises(frappe.AuthenticationError):
			self.call(created["token"], "/api/resource/CRM Deal")
		self.call(rotated["token"], "/api/resource/CRM Deal")

	def test_allowed_ips(self):
		created = self.create(allowed_ips="198.51.100.0/24")
		self.call(created["token"], "/api/resource/CRM Deal", ip="198.51.100.7")
		with self.assertRaises(frappe.AuthenticationError):
			self.call(created["token"], "/api/resource/CRM Deal", ip="203.0.113.9")

	def test_other_schemes_untouched(self):
		request = MagicMock(path="/api/resource/CRM Deal", method="GET", headers=Headers({"Authorization": "token a:b"}))
		frappe.set_user("Guest")
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
		):
			api_keys.authenticate()
		self.assertEqual(frappe.session.user, "Guest")

	# ---------- Escopos ----------

	def test_scopes_by_route(self):
		token = self.create(scopes=("cards_read",))["token"]
		self.call(token, "/api/resource/CRM Deal")
		self.call(token, "/api/method/crm.api.pipeline.get_stage_rules")
		self.call(token, "/api/method/frappe.client.get_list", form={"doctype": "CRM Deal"})
		for path, method, form in (
			("/api/resource/CRM Deal", "POST", None),
			("/api/resource/CRM Deal/X", "DELETE", None),
			("/api/resource/Contact", "GET", None),
			("/api/resource/User", "GET", None),
			("/api/resource/Cred Hub API Key", "GET", None),
			("/api/method/crm.api.pipeline.move_card", "POST", None),
			("/api/method/cred_hub.api_keys.create_api_key", "POST", None),
			("/api/method/cred_hub.billing.get_plan", "GET", None),
			("/api/method/frappe.client.get_list", "GET", {"doctype": "User"}),
			("/api/v2/document/CRM Deal/X/method/run", "POST", None),
			("/app/crm-deal", "GET", None),
		):
			with self.subTest(path=path, method=method), self.assertRaises(frappe.PermissionError):
				self.call(token, path, method, form)

	def test_write_scope_allows_create_and_move(self):
		token = self.create(scopes=("cards_read", "cards_write"))["token"]
		self.call(token, "/api/resource/CRM Deal", "POST")
		self.call(token, "/api/method/crm.api.pipeline.move_card", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_fields_admin_save_pipeline_only_fields_of_own_pipeline(self):
		token = self.create(scopes=("cards_read", "fields_admin"))["token"]
		path = "/api/method/crm.api.pipeline.save_pipeline"
		self.call(token, path, "POST", {"doc": frappe.as_json({"name": self.pipeline, "fields": []})})
		for doc in (
			{"name": self.other, "fields": []},
			{"pipeline_name": "Novo funil"},
			{"name": self.pipeline, "members": [{"user": SELLER, "role": "Owner"}]},
		):
			with self.subTest(doc=doc), self.assertRaises(frappe.PermissionError):
				self.call(token, path, "POST", {"doc": frappe.as_json(doc)})

	def test_fields_admin_off_by_default(self):
		token = self.create()["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token, "/api/method/crm.api.pipeline.create_field", "POST")

	def test_rate_limit(self):
		token = self.create()["token"]
		with patch.object(api_keys, "CALLS_PER_MINUTE", 2):
			self.call(token, "/api/resource/CRM Deal")
			self.call(token, "/api/resource/CRM Deal")
			with self.assertRaises(frappe.RateLimitExceededError):
				self.call(token, "/api/resource/CRM Deal")

	def test_key_cannot_manage_keys(self):
		self.call(self.create()["token"], "/api/resource/CRM Deal")
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			api_keys.get_api_keys()

	# ---------- Log ----------

	def test_writes_logged_reads_only_touch_last_use(self):
		created = self.create()
		token = created["token"]
		self.call(token, "/api/resource/CRM Deal")
		with patch.object(frappe, "request", MagicMock(path="/api/resource/CRM Deal", method="GET")):
			api_keys.after_request(MagicMock(status_code=200))
		self.assertFalse(frappe.db.count(api_keys.LOG, {"api_key": created["name"]}))
		self.assertTrue(frappe.db.get_value(api_keys.KEY, created["name"], "last_used_on"))

		self.call(token, "/api/resource/CRM Deal", "POST")
		with patch.object(frappe, "request", MagicMock(path="/api/resource/CRM Deal", method="POST")):
			api_keys.after_request(MagicMock(status_code=200))
		log = frappe.get_all(api_keys.LOG, {"api_key": created["name"]}, ["route", "reference_doctype", "status_code"])
		self.assertEqual(len(log), 1)
		self.assertEqual((log[0].route, log[0].reference_doctype, log[0].status_code), ("POST /api/resource/CRM Deal", "CRM Deal", 200))
