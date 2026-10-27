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
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
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
				frappe.get_doc(
					{"doctype": "User", "email": "pessoa@example.com", "first_name": "Pessoa", "roles": [{"role": "Sales User"}]}
				).insert()

	def technical(self, created=None):
		created = created or self.create()
		return frappe.db.get_value(api_keys.KEY, created["name"], "technical_user")

	def test_technical_user_cannot_become_regular_or_get_roles(self):
		user = frappe.get_doc("User", self.technical())
		user.credhub_api_key = ""
		with self.assertRaises(frappe.ValidationError):
			user.save()
		user.reload()
		user.append("roles", {"role": "System Manager"})
		with self.assertRaises(frappe.ValidationError):
			user.save()

	def test_technical_users_hook_lists_only_key_users(self):
		technical = self.technical()
		self.assertEqual(api_keys.technical_users(), [technical])
		self.assertIn("cred_hub.api_keys.technical_users", frappe.get_hooks("crm_hidden_users"))

	def test_regular_user_cannot_be_marked_by_hand(self):
		human = frappe.get_doc({"doctype": "User", "email": "integra@example.com", "first_name": "Integra"})
		human.credhub_api_key = "abc"
		with self.assertRaises(frappe.ValidationError):
			human.insert()

	def test_regular_user_used_for_integration_still_counts(self):
		before = len(billing.active_users())
		frappe.get_doc({"doctype": "User", "email": "integra@example.com", "first_name": "Integra", "roles": [{"role": "Sales User"}]}).insert()
		self.assertEqual(len(billing.active_users()), before + 1)

	def test_pause_over_limit_never_pauses_technical_user(self):
		technical = self.technical()
		with patch.object(billing, "user_limit", return_value=0):
			billing.pause_over_limit()
		self.assertEqual(frappe.db.get_value("User", technical, ["enabled", "credhub_paused"]), (1, 0))

	def test_plan_and_trial_reminder_ignore_technical_user(self):
		# get_plan (used_active) e _trial_reminder (excesso e destinatários) contam por active_users
		technical = self.technical()
		self.assertNotIn(technical, billing.active_users())
		self.assertNotIn(technical, billing.pending_invitations())
		# Mesmo com System Manager o técnico não seria destinatário: ele não está em active_users e não pode ter o papel
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("User", technical).add_roles("System Manager")

	def test_only_admin_manages_keys(self):
		frappe.set_user(SELLER)
		with self.assertRaises(frappe.PermissionError):
			api_keys.create_api_key("ERP", ["cards_read"], [self.pipeline])
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
		doc = {"doc": frappe.as_json({"name": self.pipeline, "fields": []})}
		with self.assertRaises(frappe.PermissionError):
			self.call(token, "/api/method/crm.api.pipeline.save_pipeline", "POST", doc)

	def test_catalog_changes_stay_admin_only(self):
		# create/update/disable_field exigem admin no crm: nem com "Campos e regras" a chave chega lá
		token = self.create(scopes=("cards_read", "fields_admin"))["token"]
		for method in ("create_field", "update_field", "disable_field"):
			with self.subTest(method=method), self.assertRaises(frappe.PermissionError):
				self.call(token, f"/api/method/crm.api.pipeline.{method}", "POST")

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

	# ---------- Chaves antigas ----------

	def legacy_call(self, user):
		"""Chamada com token antigo: o Frappe já pôs o usuário; o hook decide se ainda vale."""
		request = MagicMock(path="/api/resource/CRM Deal", method="GET", headers=Headers({"Authorization": "token a:b"}))
		frappe.set_user(user)
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
		):
			api_keys.authenticate()

	def set_legacy_until(self, value):
		frappe.local.conf.credhub_legacy_token_until = value
		self.addCleanup(lambda: frappe.local.conf.pop("credhub_legacy_token_until", None))

	def test_legacy_tokens_work_without_config_and_until_the_date(self):
		self.legacy_call(SELLER)
		self.set_legacy_until("2999-12-31")
		self.legacy_call(SELLER)

	def test_legacy_tokens_stop_after_the_date_except_administrator(self):
		self.set_legacy_until("2020-01-01")
		with self.assertRaises(frappe.AuthenticationError):
			self.legacy_call(SELLER)
		self.legacy_call("Administrator")

	def test_api_screen_warns_about_legacy_keys(self):
		frappe.db.set_value("User", SELLER, "api_key", "antiga123")
		self.set_legacy_until("2026-11-27")
		data = api_keys.get_api_keys()
		self.assertEqual(str(data["legacy_until"]), "2026-11-27")
		self.assertIn(SELLER, [u.name for u in data["legacy_users"]])

	def test_no_new_legacy_keys_after_deploy_nor_on_technical_user(self):
		from frappe.core.doctype.user.user import generate_keys

		with self.assertRaises(frappe.ValidationError):
			generate_keys(self.technical())
		generate_keys(SELLER)
		self.set_legacy_until("2999-12-31")
		with self.assertRaises(frappe.ValidationError):
			generate_keys(SELLER)
		generate_keys("Administrator")

	# ---------- Log ----------

	def test_writes_logged_reads_only_touch_last_use(self):
		# O registro engole erros para não derrubar a chamada; no teste, um erro ali tem que aparecer
		log_error = patch.object(frappe, "log_error", side_effect=AssertionError("after_request failed"))
		log_error.start()
		self.addCleanup(log_error.stop)
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
