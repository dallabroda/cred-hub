"""Plan features enforced by the server (credtech#398, owner option A, 12/11/2026): Credit & Collection only on the
plans that include it, AI opinions limited to the plan, the same gate on the screen, the API key and the scheduler.
Runs on the bench: `bench --site <site> run-tests --app cred_hub --module cred_hub.tests.test_plan_gating`."""

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.datastructures import Headers

from cred_hub import api_keys, billing
from cred_hub.credit_check import api as credit_api
from cred_hub.credit_check import opinion
from cred_hub.tests.test_credit_opinion import OpinionCase

C = billing.CREDIT_AND_COLLECTION

# Every whitelisted route of these modules is gated, except the reads of what already exists (nothing disappears on
# a downgrade, as in cred-hub#116) and the B2B prospecting (a sales feature). A new route must be gated or listed here.
OPEN_ROUTES = {
	"cred_hub.credit_check.api": {
		"get_report_details",
		"get_price_references",
		"get_final_bureau_options",
		"get_partners",
		"get_bureau_results",
		"get_bureau_result",
		"get_dossier_highlights",
		"get_credit_template_status",
		"list_credit_dossiers",
		"get_credit_opinion_status",
		"list_credit_opinions",
		"get_credit_policy",
		"get_dossier_package",
		"get_dossier_package_status",
		"get_checklist_template",
		"get_rating_policy",
		"get_credit_onboarding",
		"get_document_checklist",
		"get_credit_committee",
		"get_whatsapp_onboarding_reminder",
		"get_bureau_vertical_catalog",
		"public_get_portal_info",
		"public_verify_and_list",
		# turning a portal link off is always allowed (security)
		"revoke_portal_link",
		"search_bureau_b2b_leads",
	},
	"cred_hub.integrations.protest": {"collection_funnel_status", "get_status", "server_ip", "preview_row"},
	"cred_hub.integrations.localize": {"get_status"},
	"cred_hub.collection.importer": {"get_context"},
	"cred_hub.collection.reminders": {"get_reminders"},
	"cred_hub.erp.settings": {"mapping_options"},
	"cred_hub.erp.rest_templates": {"export_template"},
	"cred_hub.erp.sync": set(),
}


def set_plan(tier=None, trial_days_left=None):
	"""Plan with the trial over (or `trial_days_left` days still to go) on `tier` (None = legacy, never chose)."""
	plan = frappe.get_single(billing.PLAN)
	plan.update(
		{
			"status": "Active" if tier else "Free",
			"plan_tier": tier,
			"pending_plan_tier": None,
			"upgrade_base_tier": None,
			"upgrade_payment_id": None,
			"extra_users": 0,
			"upgrade_base_users": 0,
			"trial_ends_on": frappe.utils.add_days(frappe.utils.getdate(), trial_days_left if trial_days_left else -1),
		}
	)
	plan.save(ignore_permissions=True)


class PlanCase(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		conf = patch.dict(frappe.local.conf, {"credhub_billing": 1})
		conf.start()
		self.addCleanup(conf.stop)
		frappe.local.conf.pop("credhub_unlimited_users", None)
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.credhub_api_key = None
		frappe.db.rollback()


class TestPlanAllows(PlanCase):
	def test_each_tier_allows_or_blocks_credit_and_collection(self):
		for tier, allowed, opinions in (
			("vendas", False, 0),
			("credito_cobranca", True, 30),
			("enterprise", True, 100),
		):
			with self.subTest(tier=tier):
				set_plan(tier)
				self.assertEqual(billing.plan_allows(C), allowed)
				self.assertEqual(billing.included_opinions(), opinions)
				# the Settings screen shows exactly what the server enforces
				plan = billing.get_plan()
				self.assertEqual((plan["has_credit_and_collection"], plan["included_opinions"]), (allowed, opinions))
				if allowed:
					billing.require_feature(C)
				else:
					with self.assertRaises(billing.PlanFeatureNotIncluded) as raised:
						billing.require_feature(C)
					self.assertIsInstance(raised.exception, frappe.PermissionError)

	def test_legacy_plan_keeps_working_as_before(self):
		set_plan(None)  # grandfathered company: empty plan_tier
		self.assertEqual(billing.plan_tier_of(), "free_legacy")
		self.assertTrue(billing.plan_allows(C))
		self.assertEqual(billing.included_opinions(), 30)
		set_plan("free_legacy")
		self.assertTrue(billing.plan_allows(C))

	def test_trial_is_full_access_as_sold(self):
		set_plan("vendas", trial_days_left=5)
		self.assertTrue(billing.plan_allows(C))
		self.assertEqual(billing.included_opinions(), 30)

	def test_no_gate_without_billing_or_for_credtech_itself(self):
		set_plan("vendas")
		with patch.dict(frappe.local.conf, {"credhub_billing": 0}):
			self.assertTrue(billing.plan_allows(C))
		with patch.dict(frappe.local.conf, {"credhub_unlimited_users": 1}):
			self.assertTrue(billing.plan_allows(C))

	def test_unknown_feature_is_a_programming_error(self):
		with self.assertRaises(ValueError):
			billing.plan_allows("telepathy")

	def test_gate_follows_the_rollback_of_an_unpaid_upgrade(self):
		# Vendas -> Crédito & Cobrança "libera na hora"; the difference is not paid: back to Vendas, and so is the gate
		set_plan("credito_cobranca")
		plan = frappe.get_single(billing.PLAN)
		plan.db_set({"upgrade_base_tier": "vendas", "pending_plan_tier": "credito_cobranca"})
		self.assertTrue(billing.plan_allows(C))
		with patch.object(billing, "pause_over_limit"):
			billing._upgrade_overdue(frappe.get_single(billing.PLAN), {"invoiceUrl": "https://asaas.test/i/pay_up"})
		self.assertEqual(billing.plan_tier_of(), "vendas")
		self.assertFalse(billing.plan_allows(C))


class TestRoutesAreGated(PlanCase):
	def test_every_route_of_the_credit_modules_is_gated_or_a_listed_read(self):
		for module_name, open_routes in OPEN_ROUTES.items():
			module = importlib.import_module(module_name)
			routes = {
				fn.__name__: fn for fn in frappe.whitelisted if getattr(fn, "__module__", None) == module_name
			}
			self.assertTrue(routes, module_name)
			self.assertLessEqual(open_routes, set(routes), f"{module_name}: listed route no longer exists")
			for name, fn in routes.items():
				with self.subTest(route=f"{module_name}.{name}"):
					gated = getattr(fn, "credhub_plan_feature", None) == C
					self.assertEqual(gated, name not in open_routes)
					# the module attribute is what /api/method runs
					self.assertIs(getattr(module, name), fn)

	def test_blocked_route_on_vendas_and_allowed_on_credit_plan(self):
		from cred_hub.erp import sync

		set_plan("vendas")
		with self.assertRaises(billing.PlanFeatureNotIncluded):
			sync.sync_now()
		with self.assertRaises(billing.PlanFeatureNotIncluded):
			credit_api.save_credit_policy("Minha metodologia")
		with patch.object(credit_api.frappe, "enqueue") as enqueue:
			with self.assertRaises(billing.PlanFeatureNotIncluded):
				credit_api.trigger_check(organization_name="nao-existe")
			enqueue.assert_not_called()

		set_plan("credito_cobranca")
		# past the gate: the route runs (here it only says the ERP is off)
		self.assertFalse(sync.sync_now()["ok"])

	def test_reads_stay_open_on_vendas(self):
		set_plan("vendas")
		self.assertIn("methodology", credit_api.get_credit_policy())

	def test_blocked_message_is_translated_to_portuguese(self):
		# the .mo is compiled at deploy; here the source of the translation is checked
		set_plan("vendas")
		with self.assertRaises(billing.PlanFeatureNotIncluded) as raised:
			billing.require_feature(C)
		po = (Path(billing.__file__).parent / "locale" / "pt_BR.po").read_text(encoding="utf-8").replace("\r\n", "\n")
		self.assertIn(
			f'msgid "{raised.exception}"\nmsgstr "Seu plano não inclui Crédito e Cobrança. Um administrador pode mudar o '
			'plano em Usuários e plano."',
			po,
		)


class TestAutomationsStopWithoutThePlan(PlanCase):
	def setUp(self):
		super().setUp()
		set_plan("vendas")

	def test_credit_check_on_stage_change_does_not_run(self):
		from cred_hub.credit_check import events

		doc = MagicMock()
		doc.has_value_changed.return_value = True
		with patch.object(events, "get_credit_settings") as settings, patch.object(events.frappe, "enqueue") as enqueue:
			events.on_deal_update(doc)
		settings.assert_not_called()
		enqueue.assert_not_called()

	def test_queued_paid_check_does_not_run(self):
		from cred_hub.credit_check import enrich

		with patch.object(enrich, "get_credit_settings") as settings:
			self.assertIsNone(enrich.run_background_check(organization_name="x"))
		settings.assert_not_called()

	def test_digital_folder_stage_gate_does_not_hold_the_card(self):
		from cred_hub.credit_check import onboarding

		doc = MagicMock(organization="x", status="Comitê")
		doc.is_new.return_value = False
		doc.has_value_changed.return_value = True
		with patch.object(onboarding, "_kv_get") as kv:
			onboarding.validate_deal_stage_gate(doc)
		kv.assert_not_called()

	def test_scheduled_jobs_do_nothing(self):
		from cred_hub.collection import reminders
		from cred_hub.erp import cedente, sync
		from cred_hub.integrations import protest

		with (
			# a settings doctype that does not exist: reading it would fail the test
			patch.object(reminders, "SETTINGS", "Not A Doctype"),
			patch.object(sync, "get_settings") as erp_settings,
			patch.object(protest, "get_settings") as protest_settings,
		):
			reminders.scheduled()
			self.assertEqual(reminders.run(), {})
			sync.scheduled()
			self.assertEqual(sync.run(), {"skipped": "plan"})
			protest.sync_all()
			deal = MagicMock(flags=frappe._dict())
			deal.has_value_changed.return_value = True
			protest.on_deal_update(deal)
			cedente_deal = MagicMock()
			cedente_deal.has_value_changed.return_value = True
			with patch("cred_hub.erp.settings.get_settings") as cedente_settings:
				cedente.on_deal_update(cedente_deal)
			cedente_settings.assert_not_called()
		erp_settings.assert_not_called()
		protest_settings.assert_not_called()


class TestApiKeyPath(PlanCase):
	"""The same gate through a key (no bypass via REST): 403 with error.code PlanFeatureNotIncluded."""

	def setUp(self):
		super().setUp()
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{"pipeline_name": "API Plano", "stages": [{"deal_status": "Novo"}, {"deal_status": "Ganho", "type": "Won"}]}
		)["name"]
		self.token = api_keys.create_api_key(
			"ERP plano", ["credit_read", "credit_write", "credit_paid_queries", "files_write"], [self.pipeline]
		)["token"]

	def call(self, path, method="GET", form=None):
		request = MagicMock(
			path=path, method=method, headers=Headers({"Authorization": f"Bearer {self.token}"}), files={}
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

	def test_paid_query_and_writes_blocked_on_vendas(self):
		set_plan("vendas")
		for path, method in (
			("/api/method/cred_hub.credit_check.api.trigger_check", "POST"),
			("/api/method/cred_hub.credit_check.api.save_visit_report", "POST"),
			("/api/method/cred_hub.credit_check.api.upload_checklist_document", "POST"),
			("/api/resource/Cred Hub Checklist Item", "POST"),
			("/api/v2/document/Cred Hub Visit Report/x", "PUT"),
		):
			with self.subTest(path=path):
				with self.assertRaises(billing.PlanFeatureNotIncluded):
					self.call(path, method)
				# refused after the key is known: logged and answered with the error envelope
				self.assertTrue(frappe.local.credhub_api_key)

	def test_reads_pass_on_vendas_and_everything_on_credit_plan(self):
		set_plan("vendas")
		self.call("/api/method/cred_hub.credit_check.api.get_credit_onboarding")
		self.call("/api/resource/Cred Hub Credit Report")
		set_plan("credito_cobranca")
		user = self.call("/api/method/cred_hub.credit_check.api.trigger_check", "POST")
		self.assertNotEqual(user, "Guest")

	def test_error_envelope_carries_the_stable_code(self):
		response = MagicMock(mimetype="application/json", status_code=403)
		response.get_data.return_value = frappe.as_json(
			{"exc_type": "PlanFeatureNotIncluded", "exception": "PlanFeatureNotIncluded: Seu plano não inclui"}
		)
		api_keys._error_envelope(response, "req_1")
		body = frappe.parse_json(response.set_data.call_args.args[0])
		self.assertEqual(body["error"]["code"], "PlanFeatureNotIncluded")


class TestOpinionLimit(OpinionCase):
	"""AI opinions: up to the plan's included number per month (existing records count); over it, blocked."""

	def setUp(self):
		super().setUp()
		conf = patch.dict(frappe.local.conf, {"credhub_billing": 1})
		conf.start()
		self.addCleanup(conf.stop)
		frappe.local.conf.pop("credhub_opinions_included_monthly", None)
		frappe.local.conf.pop("credhub_unlimited_users", None)
		# the queries already exist for the organization (the dossier has something to read)
		collect = patch.object(opinion.dossier, "collect", return_value={"sections": [{"title": "x"}]})
		collect.start()
		self.addCleanup(collect.stop)

	def _done(self, count):
		for _index in range(count):
			frappe.get_doc(
				{"doctype": opinion.DOCTYPE, "organization": self.org.name, "status": "Done", "month": opinion.month_key()}
			).insert(ignore_permissions=True)

	def test_credit_plan_blocks_the_31st_opinion_of_the_month(self):
		set_plan("credito_cobranca")
		self._done(29)
		self.assertTrue(credit_api.request_credit_opinion(self.org.name)["name"])  # 30th
		status = credit_api.get_credit_opinion_status(self.org.name)
		self.assertEqual((status["included"], status["used"], status["remaining"]), (30, 30, 0))
		with self.assertRaises(billing.OpinionLimitReached) as raised:
			credit_api.request_credit_opinion(self.org.name)
		self.assertIn("30", str(raised.exception))
		# failed opinions and last month's do not count
		frappe.db.set_value(opinion.DOCTYPE, {"organization": self.org.name}, "month", "2000-01")
		self.assertTrue(credit_api.request_credit_opinion(self.org.name)["name"])

	def test_enterprise_includes_100(self):
		set_plan("enterprise")
		self._done(99)
		credit_api.request_credit_opinion(self.org.name)
		with self.assertRaises(billing.OpinionLimitReached):
			credit_api.request_credit_opinion(self.org.name)

	def test_vendas_cannot_request_any_opinion(self):
		set_plan("vendas")
		with self.assertRaises(billing.PlanFeatureNotIncluded):
			credit_api.request_credit_opinion(self.org.name)
		answer = opinion.check(self.org.name)
		self.assertTrue(answer["not_in_plan"])
		with self.assertRaises(billing.PlanFeatureNotIncluded):
			opinion.request(self.org.name)

	def test_legacy_keeps_the_30_it_always_had(self):
		set_plan(None)
		self._done(30)
		with self.assertRaises(billing.OpinionLimitReached):
			credit_api.request_credit_opinion(self.org.name)
