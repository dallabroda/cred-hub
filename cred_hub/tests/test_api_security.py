"""Endurecimento da API por chave (auditoria 11/11/2026, credtech#375): campos do sistema, linhas de tabela, busca por
documento, Pasta Digital, arquivos de outros funis, upload, recusas registradas, sem traceback e consultas pagas.
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_api_security`."""

import io
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import FileStorage, Headers

from cred_hub import api_guard, api_keys, bulk

DEAL = "CRM Deal"


class TestApiSecurity(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline({"pipeline_name": "Seg Liberado", "stages": [{"deal_status": "Novo"}]})["name"]
		self.other = save_pipeline({"pipeline_name": "Seg Fechado", "stages": [{"deal_status": "Novo"}]})["name"]
		suffix = frappe.generate_hash(length=6)
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": f"Seg Org {suffix}", "cnpj": "11.222.333/0001-81"}
		).insert(ignore_permissions=True)
		self.contact = frappe.get_doc({"doctype": "Contact", "first_name": "Bia", "last_name": suffix}).insert(
			ignore_permissions=True
		)
		self.deal = frappe.get_doc(
			{
				"doctype": DEAL,
				"pipeline": self.pipeline,
				"organization": self.org.name,
				"contacts": [{"contact": self.contact.name}],
			}
		).insert(ignore_permissions=True)
		self.hidden = frappe.get_doc(
			{
				"doctype": DEAL,
				"pipeline": self.other,
				"organization": self.org.name,
				"contacts": [{"contact": self.contact.name}],
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(DEAL, self.deal.name, "credit_verdict", "Warning")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.local.flags.disable_traceback = False
		frappe.db.rollback()

	def create(self, scopes):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		return api_keys.create_api_key("Seg", list(scopes), [self.pipeline])

	def call(self, token, path, method="GET", form=None, files=None):
		request = MagicMock(
			path=path, method=method, headers=Headers({"Authorization": f"Bearer {token}"}), files=files or {}
		)
		frappe.set_user("Guest")
		frappe.local.credhub_api_key = None
		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", lambda name, default=None: request.headers.get(name, default)),
			patch.dict(frappe.local.form_dict, form or {}, clear=True),
		):
			frappe.local.request_ip = "203.0.113.9"
			api_keys.authenticate()
		return frappe.session.user

	# ---------- Despacho para outro método ----------

	def test_run_method_cannot_dispatch_a_document_method(self):
		token = self.create(("cards_read",))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(token, f"/api/resource/{DEAL}/{self.deal.name}", form={"run_method": "save"})

	# ---------- Campos do sistema e linhas de tabela (M6) ----------

	def test_system_filled_fields_refused_on_key_writes(self):
		token = self.create(("cards_read", "cards_write", "contacts_write"))["token"]
		path = f"/api/resource/{DEAL}/{self.deal.name}"
		current = frappe.db.get_value(DEAL, self.deal.name, "credit_verdict")
		self.call(token, path, "PUT", {"credit_verdict": current, "next_step": "ok"})  # devolver o que leu passa
		for data in ({"credit_verdict": "outro"}, {"erp_run_id": "x"}, {"owner": "Administrator"}, {"docstatus": 1}):
			with self.subTest(data=data), self.assertRaises(api_guard.WriteRefused):
				self.call(token, path, "PUT", data)
		with self.assertRaises(api_guard.WriteRefused):
			self.call(token, f"/api/resource/{DEAL}", "POST", {"data": frappe.as_json({"pipeline": self.pipeline, "erp_run_id": "x"})})
		# canais do contato são entrada legítima de integração
		self.call(token, "/api/resource/Contact", "POST", {"first_name": "Caio", "email_id": "caio@example.com"})

	def test_child_rows_cannot_carry_internals_or_rows_of_another_record(self):
		token = self.create(("cards_read", "cards_write"))["token"]
		path = f"/api/resource/{DEAL}/{self.deal.name}"
		own = self.deal.contacts[0].name
		foreign = frappe.get_doc(DEAL, self.hidden.name).contacts[0].name
		self.call(token, path, "PUT", {"contacts": [{"name": own, "contact": self.contact.name}]})
		for rows in (
			[{"name": foreign, "contact": self.contact.name}],
			[{"doctype": "User", "contact": self.contact.name}],
			[{"parent": self.hidden.name, "contact": self.contact.name}],
		):
			with self.subTest(rows=rows), self.assertRaises(api_guard.WriteRefused):
				self.call(token, path, "PUT", {"contacts": rows})

		self.call(token, "/api/method/cred_hub.bulk.bulk_upsert", "POST", {"doctype": DEAL, "records": "[]"})
		result = bulk.bulk_upsert(
			DEAL,
			[
				{"name": self.deal.name, "contacts": [{"name": foreign, "contact": self.contact.name}]},
				{"name": self.deal.name, "credit_report": "x"},
				{"name": self.deal.name, "next_step": "lote"},
			],
		)
		self.assertEqual([r["status"] for r in result["results"]], ["error", "error", "success"], result)
		self.assertEqual(frappe.get_doc(DEAL, self.hidden.name).contacts[0].name, foreign)

	# ---------- Busca por documento e Pasta Digital (M8, M9) ----------

	def test_lookup_returns_only_blocks_of_the_key_scopes(self):
		self.call(self.create(("cards_read",))["token"], "/api/method/cred_hub.api_keys.lookup_by_document")
		result = api_keys.lookup_by_document("11222333000181")
		self.assertIsNone(result["organization"])
		self.assertIsNone(result["digital_folder_summary"])
		self.assertEqual({c["name"] for c in result["cards"]}, {self.deal.name})  # só os cards dos funis da chave
		self.assertNotIn("credit_verdict", result["cards"][0])
		self.call(self.create(("organizations_read", "cards_read"))["token"], "/api/method/cred_hub.api_keys.lookup_by_document")
		result = api_keys.lookup_by_document("11222333000181")
		self.assertEqual(result["organization"]["name"], self.org.name)
		self.assertNotIn("credit_verdict", result["organization"])
		self.assertEqual({c["name"] for c in result["cards"]}, {self.deal.name})  # o card do outro funil não vem
		self.assertIsNone(result["digital_folder_summary"])

	def test_credit_onboarding_needs_credit_read(self):
		path = "/api/method/cred_hub.credit_check.api.get_credit_onboarding"
		for scopes in (("cards_read",), ("organizations_read",)):
			with self.subTest(scopes=scopes), self.assertRaises(frappe.PermissionError):
				self.call(self.create(scopes)["token"], path)
		self.call(self.create(("credit_read",))["token"], path)

	# ---------- Arquivos (M10, M11, upload) ----------

	def file_on(self, deal):
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"seg-{deal}.txt",
				"content": b"conteudo",
				"attached_to_doctype": DEAL,
				"attached_to_name": deal,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)

	def test_files_follow_the_card_pipeline(self):
		mine, other = self.file_on(self.deal.name), self.file_on(self.hidden.name)
		self.call(self.create(("files_read",))["token"], "/api/resource/File")
		names = set(frappe.get_list("File", filters={"name": ("in", [mine.name, other.name])}, pluck="name"))
		self.assertEqual(names, {mine.name})
		self.assertFalse(frappe.has_permission("File", "read", doc=frappe.get_doc("File", other.name)))
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		self.assertEqual(
			set(frappe.get_list("File", filters={"name": ("in", [mine.name, other.name])}, pluck="name")),
			{mine.name, other.name},
		)

	def test_upload_file_rules_for_keys(self):
		token = self.create(("files_write", "cards_read"))["token"]

		def upload(name="contrato.pdf", **form):
			files = {"file": FileStorage(io.BytesIO(b"x"), filename=name)}
			return self.call(token, "/api/method/upload_file", "POST", form, files=files)

		upload(doctype=DEAL, docname=self.deal.name)
		upload()  # sem anexo: arquivo privado avulso
		for name in ("pagina.html", "imagem.svg", "script.js", "sem_extensao"):
			with self.subTest(name=name), self.assertRaises(api_guard.WriteRefused):
				upload(name)
		for form in (
			{"file_url": "/private/files/outro.pdf"},
			{"doctype": "User", "docname": "Administrator"},
			{"doctype": DEAL},
			{"doctype": DEAL, "docname": self.hidden.name},
		):
			with self.subTest(form=form), self.assertRaises(frappe.PermissionError):
				upload(**form)

	# ---------- Recusas registradas, rate limit e sem traceback (M13, M14) ----------

	def test_refused_calls_are_logged_and_rate_limited(self):
		created = self.create(("cards_read",))
		with self.assertRaises(frappe.PermissionError):
			self.call(created["token"], "/api/resource/Contact")
		log = frappe.get_last_doc(api_keys.LOG, filters={"api_key": created["name"]})
		self.assertEqual(log.status_code, 403)
		self.assertTrue(log.request_id.startswith("req_"))
		wrong = created["token"][:-4] + "zzzz"
		with self.assertRaises(frappe.AuthenticationError):
			self.call(wrong, "/api/resource/CRM Deal")
		self.assertEqual(frappe.get_last_doc(api_keys.LOG, filters={"api_key": created["name"]}).status_code, 401)
		with patch.object(api_keys, "CALLS_PER_MINUTE", 3), self.assertRaises(frappe.RateLimitExceededError):
			for _ in range(5):
				try:
					self.call(created["token"], "/api/resource/Contact")
				except frappe.RateLimitExceededError:
					raise
				except frappe.PermissionError:
					pass

	def test_keys_never_get_tracebacks(self):
		frappe.local.flags.disable_traceback = False
		self.call(self.create(("cards_read",))["token"], "/api/resource/CRM Deal")
		self.assertTrue(frappe.local.flags.disable_traceback)

	# ---------- Consultas pagas (L6) ----------

	def test_paid_queries_need_their_own_scope_and_post(self):
		path = "/api/method/cred_hub.credit_check.api.trigger_check"
		with self.assertRaises(frappe.PermissionError):
			self.call(self.create(("credit_read", "credit_write"))["token"], path, "POST")
		paid = self.create(("credit_paid_queries",))["token"]
		with self.assertRaises(frappe.PermissionError):
			self.call(paid, path, "GET")
		self.call(paid, path, "POST")
		self.assertTrue(frappe.local.credhub_api_key["write"])
