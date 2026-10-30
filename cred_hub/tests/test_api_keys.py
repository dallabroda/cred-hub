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
			billing.pause_over_limit("downgrade")
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

	def test_call_arguments_survive_authentication(self):
		# set_user zera o form_dict: sem o cuidado, get_stage_rules(pipeline, stage) chegava sem argumentos
		token = self.create()["token"]
		request = MagicMock(path="/api/method/crm.api.pipeline.get_stage_rules", method="GET")
		request.headers = Headers({"Authorization": f"Bearer {token}"})
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		frappe.local.form_dict = frappe._dict(pipeline=self.pipeline, stage="Novo")
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
		):
			frappe.local.request_ip = "203.0.113.9"
			api_keys.authenticate()
		self.assertEqual(dict(frappe.local.form_dict), {"pipeline": self.pipeline, "stage": "Novo"})

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
		self.call(token, "/api/method/crm.api.forms.get_form_schema")
		self.call(token, "/api/method/crm.api.forms.get_card_blocks")
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

	# ---------- Enterprise API Capabilities (credtech#288) ----------

	def test_delete_requires_write_and_allow_delete(self):
		# Sem allow_delete: barrado mesmo com cards_write
		token_no_delete = self.create(scopes=("cards_read", "cards_write"))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token_no_delete, "/api/resource/CRM Deal/CARD-1", "DELETE")

		# Sem cards_write: barrado mesmo com allow_delete
		token_no_write = self.create(scopes=("cards_read", "allow_delete"))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token_no_write, "/api/resource/CRM Deal/CARD-1", "DELETE")

		# Com ambos: permitido e marcado como escrita
		token_allowed = self.create(scopes=("cards_read", "cards_write", "allow_delete"))["token"]
		self.call(token_allowed, "/api/resource/CRM Deal/CARD-1", "DELETE")
		self.assertTrue(frappe.local.credhub_api_key["write"])

		# Outros recursos permitidos para DELETE com o escopo de escrita correspondente
		for resource, scope in (
			("Contact", "contacts_write"),
			("CRM Organization", "organizations_write"),
			("File", "files_write"),
			("FCRM Note", "notes_write"),
			("CRM Task", "tasks_write"),
		):
			with self.subTest(resource=resource):
				t = self.create(scopes=(scope, "allow_delete"))["token"]
				self.call(t, f"/api/resource/{resource}/NAME-1", "DELETE")
				self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_delete_blocked_on_protected_doctypes(self):
		all_scopes = list(api_keys.SCOPES.keys())
		token = self.create(scopes=all_scopes)["token"]
		for protected in ("CRM Pipeline", "CRM Deal Status", "Cred Hub API Key", "User", "CRM Lead"):
			with self.subTest(doctype=protected), self.assertRaises(frappe.PermissionError):
				self.call(token, f"/api/resource/{protected}/X", "DELETE")

	def test_files_download_and_upload(self):
		token_read = self.create(scopes=("files_read",))["token"]
		self.call(token_read, "/private/files/relatorio.pdf", "GET")
		self.call(token_read, "/files/avatar.png", "HEAD")
		self.call(token_read, "/api/resource/File")
		self.call(token_read, "/api/method/frappe.client.get_list", form={"doctype": "File"})

		# Download não aceita POST
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/private/files/relatorio.pdf", "POST")

		# Upload exige files_write
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/api/method/upload_file", "POST")
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/api/resource/File", "POST")

		token_write = self.create(scopes=("files_read", "files_write"))["token"]
		self.call(token_write, "/api/method/upload_file", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])
		self.call(token_write, "/api/resource/File", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_comments_and_notes(self):
		token_read = self.create(scopes=("comments_read", "notes_read"))["token"]
		self.call(token_read, "/api/method/cred_hub.timeline.get_comments", "GET", form={"reference_doctype": "CRM Deal", "reference_name": "CARD-1"})
		self.call(token_read, "/api/resource/FCRM Note")
		self.call(token_read, "/api/method/frappe.client.get_list", form={"doctype": "FCRM Note"})

		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/api/method/frappe.desk.form.utils.add_comment", "POST")
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/api/resource/FCRM Note", "POST")

		token_write = self.create(scopes=("comments_write", "notes_write"))["token"]
		self.call(token_write, "/api/method/frappe.desk.form.utils.add_comment", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])
		self.call(token_write, "/api/resource/FCRM Note", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_tasks_resource(self):
		token_read = self.create(scopes=("tasks_read",))["token"]
		self.call(token_read, "/api/resource/CRM Task")
		self.call(token_read, "/api/method/frappe.client.get_list", form={"doctype": "CRM Task"})
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read, "/api/resource/CRM Task", "POST")

		token_write = self.create(scopes=("tasks_read", "tasks_write"))["token"]
		self.call(token_write, "/api/resource/CRM Task", "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])

	def test_sync_deleted_records(self):
		token = self.create(scopes=("sync_deletes",))["token"]
		self.call(token, "/api/method/cred_hub.sync.get_deleted_records", "GET", form={"doctype": "CRM Deal"})

		token_no_sync = self.create(scopes=("cards_read",))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token_no_sync, "/api/method/cred_hub.sync.get_deleted_records", "GET", form={"doctype": "CRM Deal"})

	def test_sync_deleted_records_isolates_by_pipeline_and_fails_closed(self):
		"""Chave só vê exclusões de CRM Deal dos próprios funis; sem o snapshot do funil, o registro
		fica de fora (nunca aparece "por garantia")."""
		from cred_hub import sync

		for name, pipeline in (
			("GHOST-IN-SCOPE", self.pipeline),
			("GHOST-OUT-OF-SCOPE", self.other),
		):
			frappe.get_doc(
				{
					"doctype": "Deleted Document",
					"deleted_doctype": "CRM Deal",
					"deleted_name": name,
					"data": frappe.as_json({"pipeline": pipeline}),
				}
			).insert(ignore_permissions=True)
		for name, data in (("GHOST-NO-SNAPSHOT", None), ("GHOST-BAD-SNAPSHOT", "{not json")):
			frappe.get_doc(
				{"doctype": "Deleted Document", "deleted_doctype": "CRM Deal", "deleted_name": name, "data": data}
			).insert(ignore_permissions=True)

		token = self.create(scopes=("cards_read", "sync_deletes"))["token"]
		self.call(token, "/api/method/cred_hub.sync.get_deleted_records", "GET", form={"doctype": "CRM Deal"})
		seen = {r["deleted_name"] for r in sync.get_deleted_records("CRM Deal")}
		self.assertIn("GHOST-IN-SCOPE", seen)
		self.assertNotIn("GHOST-OUT-OF-SCOPE", seen)
		self.assertNotIn("GHOST-NO-SNAPSHOT", seen)
		self.assertNotIn("GHOST-BAD-SNAPSHOT", seen)
		self.assertNotIn("owner", sync.get_deleted_records("CRM Deal")[0])

	def test_sync_deleted_records_does_not_stall_behind_other_pipelines(self):
		"""Mais exclusões de outros funis do que o limite não escondem as do funil da chave."""
		from cred_hub import sync

		for i in range(3):
			frappe.get_doc(
				{
					"doctype": "Deleted Document",
					"deleted_doctype": "CRM Deal",
					"deleted_name": f"GHOST-OTHER-{i}",
					"data": frappe.as_json({"pipeline": self.other}),
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": "CRM Deal",
				"deleted_name": "GHOST-MINE",
				"data": frappe.as_json({"pipeline": self.pipeline}),
			}
		).insert(ignore_permissions=True)
		seen = [r["deleted_name"] for r in sync.deleted_since("CRM Deal", None, [self.pipeline], limit=2)]
		self.assertIn("GHOST-MINE", seen)
		self.assertFalse([name for name in seen if name.startswith("GHOST-OTHER")])

	def test_sync_deleted_notes_follow_the_card_pipeline(self):
		from cred_hub import sync

		mine = frappe.get_doc(
			{"doctype": "CRM Deal", "pipeline": self.pipeline, "organization_name": "Sync Org"}
		).insert(ignore_permissions=True)
		other = frappe.get_doc(
			{"doctype": "CRM Deal", "pipeline": self.other, "organization_name": "Sync Org 2"}
		).insert(ignore_permissions=True)
		for name, card in (("NOTE-MINE", mine.name), ("NOTE-OTHER", other.name), ("NOTE-GONE", "CRM-DEAL-GONE")):
			frappe.get_doc(
				{
					"doctype": "Deleted Document",
					"deleted_doctype": "FCRM Note",
					"deleted_name": name,
					"data": frappe.as_json({"reference_doctype": "CRM Deal", "reference_docname": card}),
				}
			).insert(ignore_permissions=True)
		seen = {r["deleted_name"] for r in sync.deleted_since("FCRM Note", None, [self.pipeline])}
		self.assertIn("NOTE-MINE", seen)
		self.assertNotIn("NOTE-OTHER", seen)
		self.assertNotIn("NOTE-GONE", seen)

	def test_card_contacts_route(self):
		token = self.create(scopes=("cards_read",))["token"]
		self.call(token, "/api/method/crm.api.card_contacts.get_card_contacts", "GET", form={"deal": "CARD-1"})

		token_no_cards = self.create(scopes=("contacts_read",))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token_no_cards, "/api/method/crm.api.card_contacts.get_card_contacts", "GET", form={"deal": "CARD-1"})

	def test_custom_rate_limit(self):
		created = self.create(scopes=("cards_read",), custom_rate_limit=5)
		self.assertEqual(created["custom_rate_limit"], 5)
		token = created["token"]
		for _ in range(5):
			self.call(token, "/api/resource/CRM Deal")
		with self.assertRaises(frappe.RateLimitExceededError):
			self.call(token, "/api/resource/CRM Deal")

	def test_update_custom_rate_limit(self):
		created = self.create(scopes=("cards_read",))
		self.assertIsNone(created["custom_rate_limit"])
		updated = api_keys.update_api_key(created["name"], custom_rate_limit=800)
		self.assertEqual(updated["custom_rate_limit"], 800)
		self.assertEqual(frappe.db.get_value(api_keys.KEY, created["name"], "custom_rate_limit"), 800)

	def test_custom_rate_limit_has_a_ceiling(self):
		with self.assertRaises(frappe.ValidationError):
			self.create(scopes=("cards_read",), custom_rate_limit=api_keys.MAX_CUSTOM_RATE_LIMIT + 1)
		created = self.create(scopes=("cards_read",))
		with self.assertRaises(frappe.ValidationError):
			api_keys.update_api_key(created["name"], custom_rate_limit=100000)
	def test_sync_deleted_notes_with_deleted_parent_and_orphan_isolation(self):
		"""Notas excluídas exigem vínculo com card do funil (vivo ou excluído); órfãs nunca vazam."""
		from cred_hub import sync

		# Card excluído no funil da chave
		frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": "CRM Deal",
				"deleted_name": "CARD-ALREADY-DELETED",
				"data": frappe.as_json({"pipeline": self.pipeline}),
			}
		).insert(ignore_permissions=True)

		# Nota pertencente a esse card já excluído
		frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": "FCRM Note",
				"deleted_name": "NOTE-FROM-DELETED-CARD",
				"data": frappe.as_json({"reference_doctype": "CRM Deal", "reference_docname": "CARD-ALREADY-DELETED"}),
			}
		).insert(ignore_permissions=True)

		# Nota órfã (sem card)
		frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": "FCRM Note",
				"deleted_name": "NOTE-ORPHAN",
				"data": frappe.as_json({"reference_doctype": None, "reference_docname": None}),
			}
		).insert(ignore_permissions=True)

		# Nota vinculada a lead
		frappe.get_doc(
			{
				"doctype": "Deleted Document",
				"deleted_doctype": "FCRM Note",
				"deleted_name": "NOTE-LEAD",
				"data": frappe.as_json({"reference_doctype": "CRM Lead", "reference_docname": "LEAD-1"}),
			}
		).insert(ignore_permissions=True)

		seen = {r["deleted_name"] for r in sync.deleted_since("FCRM Note", None, [self.pipeline])}
		self.assertIn("NOTE-FROM-DELETED-CARD", seen)
		self.assertNotIn("NOTE-ORPHAN", seen)
		self.assertNotIn("NOTE-LEAD", seen)

	def test_legacy_hash_upgrade(self):
		"""Chave gravada com hash SHA-256 antigo autentica e atualiza para HMAC com chave de site."""
		import hashlib
		created = self.create()
		_key_id, secret = api_keys._split_token(created["token"])
		legacy_sha256 = hashlib.sha256(secret.encode()).hexdigest()
		frappe.db.set_value(api_keys.KEY, created["name"], "secret_hash", legacy_sha256)

		# Autentica normalmente
		self.call(created["token"], "/api/resource/CRM Deal")

		# Confere que o hash foi atualizado no banco
		upgraded_hash = frappe.db.get_value(api_keys.KEY, created["name"], "secret_hash")
		self.assertEqual(upgraded_hash, api_keys._hash(secret))
		self.assertNotEqual(upgraded_hash, legacy_sha256)

	def test_bulk_upsert_cards(self):
		"""Operação em lote para cards com relatório individual de erros e savepoints."""
		from cred_hub import bulk

		token = self.create(scopes=("cards_read", "cards_write"))["token"]
		records = [
			{"pipeline": self.pipeline, "organization_name": "Batch Org 1"},
			{"pipeline": self.pipeline, "organization_name": "Batch Org 2"},
		]
		self.call(token, "/api/method/cred_hub.bulk.bulk_upsert", "POST", form={"doctype": "CRM Deal", "records": frappe.as_json(records)})
		res = bulk.bulk_upsert("CRM Deal", records)
		self.assertEqual(res["total"], 2)
		self.assertEqual(res["success"], 2)
		self.assertEqual(res["failed"], 0)
		self.assertTrue(frappe.db.exists("CRM Deal", res["results"][0]["name"]))
		self.assertTrue(frappe.db.exists("CRM Deal", res["results"][1]["name"]))

	def test_bulk_upsert_isolates_failures_with_savepoints(self):
		"""Um erro no meio do lote não cancela os outros registros do lote."""
		from cred_hub import bulk

		token = self.create(scopes=("cards_read", "cards_write"))["token"]
		records = [
			{"pipeline": self.pipeline, "organization_name": "Valid Card"},
			{"pipeline": "INVALID-PIPELINE", "organization_name": "Invalid Card"},
		]
		res = bulk.bulk_upsert("CRM Deal", records)
		self.assertEqual(res["total"], 2)
		self.assertEqual(res["success"], 1)
		self.assertEqual(res["failed"], 1)
		self.assertEqual(res["results"][0]["status"], "success")
		self.assertEqual(res["results"][1]["status"], "error")

	def test_bulk_upsert_permission_enforced(self):
		"""Chave sem permissão de escrita recebe PermissionError no lote."""
		from cred_hub import bulk

		token_read_only = self.create(scopes=("cards_read",))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token_read_only, "/api/method/cred_hub.bulk.bulk_upsert", "POST", form={"doctype": "CRM Deal", "records": "[]"})

	def test_webhooks_signature_and_delivery(self):
		"""Webhooks assinam o payload com HMAC-SHA256 (X-HubTech-Signature)."""
		from cred_hub import webhooks

		wh = frappe.get_doc(
			{
				"doctype": "Cred Hub Webhook",
				"label": "Test Webhook",
				"webhook_url": "https://example.com/webhook",
				"secret": "test_secret_key_123",
				"status": "Active",
				"deal_created": 1,
				"pipelines": [{"pipeline": self.pipeline}],
			}
		).insert(ignore_permissions=True)

		with patch("requests.post") as mock_post:
			mock_post.return_value.ok = True
			mock_post.return_value.status_code = 200
			mock_post.return_value.text = "OK"

			webhooks.send_webhook(wh.name, "deal.created", {"name": "CARD-TEST", "pipeline": self.pipeline})

			self.assertTrue(mock_post.called)
			_args, kwargs = mock_post.call_args
			headers = kwargs["headers"]
			self.assertIn("X-HubTech-Signature", headers)
			self.assertTrue(headers["X-HubTech-Signature"].startswith("sha256="))
			self.assertEqual(headers["X-HubTech-Event"], "deal.created")
			self.assertIn("X-HubTech-Delivery", headers)
