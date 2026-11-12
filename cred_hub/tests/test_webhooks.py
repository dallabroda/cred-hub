"""Webhooks de saída: outbox transacional, eventos sem duplicata, ganho/perda pelo status do card, filtro de funil,
tentativas com espera, disjuntor, reenvio, troca de segredo e limpeza (auditoria 11/11/2026, credtech#375).
Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_webhooks`."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime

from cred_hub import webhooks

DEAL = "CRM Deal"
OUTBOX = webhooks.OUTBOX_DOCTYPE


class TestWebhooks(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		webhooks.clear_active_cache()
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{"pipeline_name": "WH A", "stages": [{"deal_status": "Novo"}, {"deal_status": "Fechado", "type": "Won"}]}
		)["name"]
		self.other = save_pipeline({"pipeline_name": "WH B", "stages": [{"deal_status": "Novo"}]})["name"]
		self.stages = frappe.get_all("CRM Deal Status", {"pipeline": self.pipeline}, pluck="name", order_by="position asc")
		suffix = frappe.generate_hash(length=6)
		self.org = frappe.get_doc({"doctype": "CRM Organization", "organization_name": f"WH Org {suffix}"}).insert(
			ignore_permissions=True
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		webhooks.clear_active_cache()
		for key in ("credhub_wh_circuit:", "credhub_wh_fails:"):
			for name in getattr(self, "_hooks", []):
				frappe.cache.delete_value(f"{key}{name}")

	def webhook(self, **values):
		doc = frappe.get_doc(
			{
				"doctype": webhooks.WEBHOOK_DOCTYPE,
				"label": "WH",
				"webhook_url": "https://hooks.example.com/hubtech",
				"secret": "segredo",
				"status": "Active",
				**{field: 0 for field in webhooks.EVENT_FIELD_MAP.values()},
				**values,
			}
		).insert(ignore_permissions=True)
		self._hooks = [*getattr(self, "_hooks", []), doc.name]
		return doc

	def events(self, webhook=None):
		filters = {"webhook": webhook} if webhook else {}
		return [r.event for r in frappe.get_all(OUTBOX, filters, ["event"], order_by="creation asc")]

	def deal(self, pipeline=None, **values):
		return frappe.get_doc(
			{"doctype": DEAL, "pipeline": pipeline or self.pipeline, "organization": self.org.name, **values}
		).insert(ignore_permissions=True)

	def network(self, status_code=200):
		"""Dublê da rede: devolve (patches, session)."""
		from cred_hub.email_company import safe_http

		session = MagicMock()
		session.post.return_value = MagicMock(status_code=status_code, text="resposta")
		return (
			patch.object(safe_http, "resolve_public"),
			patch.object(safe_http, "_session", return_value=session),
		), session

	# ---------- Outbox (H4, L7) ----------

	def test_events_go_to_the_outbox_in_the_same_transaction(self):
		wh = self.webhook(deal_created=1)
		frappe.db.savepoint("antes")
		self.deal()
		self.assertEqual(self.events(wh.name), ["deal.created"])
		frappe.db.rollback(save_point="antes")
		self.assertEqual(self.events(wh.name), [])  # desfez a gravação, desfez o evento

	def test_full_queue_never_breaks_the_save(self):
		self.webhook(deal_created=1)
		with (
			patch.dict(frappe.flags, {"in_test": False}),
			patch.object(frappe, "enqueue", side_effect=frappe.QueueOverloaded("cheia")),
		):
			self.deal()
			frappe.db.after_commit.run()  # o disparo após o commit engole o erro
		self.assertEqual(self.events(), ["deal.created"])

	# ---------- Eventos (M1, H5, M3) ----------

	def test_no_duplicate_events_on_insert_and_no_op_save(self):
		wh = self.webhook(deal_created=1, deal_stage_changed=1, contact_created=1, contact_updated=1)
		deal = self.deal()
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Rui"}).insert(ignore_permissions=True)
		self.assertEqual(sorted(self.events(wh.name)), ["contact.created", "deal.created"])
		contact.save(ignore_permissions=True)
		self.assertEqual(self.events(wh.name).count("contact.updated"), 0)
		contact.last_name = "Lima"
		contact.save(ignore_permissions=True)
		self.assertEqual(self.events(wh.name).count("contact.updated"), 1)
		deal.status = self.stages[0]
		deal.save(ignore_permissions=True)  # mesma etapa: nada
		self.assertEqual(self.events(wh.name).count("deal.stage_changed"), 0)

	def test_won_and_lost_follow_the_card_outcome(self):
		wh = self.webhook(deal_won=1, deal_lost=1, deal_stage_changed=1)
		deal = self.deal()
		deal.outcome = "Lost"
		if not frappe.db.exists("CRM Lost Reason", "Preço WH"):
			frappe.get_doc({"doctype": "CRM Lost Reason", "lost_reason": "Preço WH"}).insert(ignore_permissions=True)
		deal.lost_reason = "Preço WH"
		deal.save(ignore_permissions=True)
		self.assertEqual(self.events(wh.name), ["deal.lost"])
		data = json.loads(frappe.get_all(OUTBOX, {"event": "deal.lost"}, ["payload"])[0].payload)["data"]
		for key in ("outcome", "lost_reason", "closed_date"):
			self.assertIn(key, data)
		self.assertEqual(data["outcome"], "Lost")
		other = self.deal()
		other.status = self.stages[1]  # etapa do tipo Ganho: o outcome vira Won
		other.save(ignore_permissions=True)
		self.assertIn("deal.won", self.events(wh.name))
		self.assertIn("deal.stage_changed", self.events(wh.name))

	def test_pipeline_filter_applies_to_contact_and_organization_events(self):
		wh = self.webhook(contact_updated=1, checklist_item_rejected=1, pipelines=[{"pipeline": self.pipeline}])
		contact = frappe.get_doc({"doctype": "Contact", "first_name": "Eva"}).insert(ignore_permissions=True)
		self.deal(pipeline=self.other, contacts=[{"contact": contact.name}])
		contact.last_name = "Fora"
		contact.save(ignore_permissions=True)
		self.assertEqual(self.events(wh.name), [])  # só tem card no outro funil
		self.deal(contacts=[{"contact": contact.name}])
		contact.last_name = "Dentro"
		contact.save(ignore_permissions=True)
		self.assertEqual(self.events(wh.name), ["contact.updated"])
		webhooks.trigger_checklist_rejected(self.org.name, None, "contrato_social", "Contrato", "x", "")
		self.assertIn("checklist.item_rejected", self.events(wh.name))  # a organização tem card no funil

	# ---------- Envio (M2, M4, L8) ----------

	def pending(self, wh):
		self.deal()
		return frappe.get_last_doc(OUTBOX, filters={"webhook": wh.name})

	def test_retries_back_off_keep_delivery_id_and_die(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		(p1, p2), session = self.network(503)
		with p1, p2:
			for attempt in range(1, webhooks.MAX_ATTEMPTS + 1):
				before = now_datetime()
				self.assertFalse(webhooks.deliver(row.name))
				row.reload()
				self.assertEqual(row.attempts, attempt)
				if attempt < webhooks.MAX_ATTEMPTS:
					self.assertEqual(row.status, "Pending")
					wait = (get_datetime(row.next_attempt_at) - before).total_seconds()
					self.assertAlmostEqual(wait, webhooks.BACKOFF_SECONDS[attempt - 1], delta=5)
		self.assertEqual(row.status, "Dead")
		ids = {c.kwargs["headers"]["X-HubTech-Delivery"] for c in session.post.call_args_list}
		self.assertEqual(ids, {row.delivery_id})
		self.assertTrue(all(c.kwargs["stream"] for c in session.post.call_args_list))

	def test_client_error_is_not_retried_and_success_is_delivered(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		(p1, p2), _session = self.network(400)
		with p1, p2:
			webhooks.deliver(row.name)
		self.assertEqual(frappe.db.get_value(OUTBOX, row.name, "status"), "Dead")
		row = self.pending(wh)
		(p1, p2), session = self.network(200)
		with p1, p2:
			self.assertTrue(webhooks.deliver(row.name))
		self.assertEqual(frappe.db.get_value(OUTBOX, row.name, "status"), "Delivered")
		body = json.loads(session.post.call_args.kwargs["data"])
		self.assertEqual((body["event_id"], body["delivery_id"], body["replay"]), (row.event_id, row.delivery_id, False))
		self.assertTrue(body["timestamp"].endswith("+00:00"))

	def test_dispatch_respects_circuit_breaker_and_per_webhook_cap(self):
		wh = self.webhook(deal_created=1)
		for _ in range(3):
			self.deal()
		(p1, p2), session = self.network(200)
		with p1, p2, patch.object(webhooks, "PER_WEBHOOK_PER_RUN", 2):
			frappe.cache.set_value(webhooks._circuit_key(wh.name), 1, expires_in_sec=60)
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 0)
			frappe.cache.delete_value(webhooks._circuit_key(wh.name))
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 2)
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 1)
		self.assertEqual(session.post.call_count, 3)

	def test_claim_is_proved_by_reading_back_its_token(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		# another run claimed it between our UPDATE and our check: we must not send
		real_get_value = frappe.db.get_value

		def stolen(doctype, name, field, *args, **kwargs):
			if doctype == OUTBOX and field == "claim_token":
				return "token-of-another-run"
			return real_get_value(doctype, name, field, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=stolen):
			self.assertFalse(webhooks._claim(row.name))
		self.assertEqual(frappe.db.get_value(OUTBOX, row.name, "status"), "Sending")  # ours stayed unsent
		fresh = self.pending(wh)
		self.assertTrue(webhooks._claim(fresh.name))
		self.assertFalse(webhooks._claim(fresh.name))  # the token of the first claim stays; a second run loses

	def test_rows_are_claimed_once_and_stale_claims_come_back(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		self.assertTrue(webhooks._claim(row.name))
		self.assertFalse(webhooks._claim(row.name))  # a second run cannot take it
		(p1, p2), session = self.network(200)
		with p1, p2:
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 0)  # in flight elsewhere: not sent again
			frappe.db.set_value(OUTBOX, row.name, "claimed_at", add_to_date(now_datetime(), minutes=-11))
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 1)  # the dead run's claim expired
		self.assertEqual(session.post.call_count, 1)
		self.assertEqual(frappe.db.get_value(OUTBOX, row.name, "status"), "Delivered")

	def test_runs_are_bounded_in_time_overall_and_per_webhook(self):
		slow, fast = self.webhook(deal_created=1), self.webhook(deal_created=1)
		for _ in range(2):
			self.deal()
		(p1, p2), session = self.network(200)
		with p1, p2, patch.object(webhooks, "RUN_SECONDS", -1):
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 0)
		with p1, p2, patch.object(webhooks, "WEBHOOK_SECONDS_PER_RUN", -1):
			self.assertEqual(webhooks.dispatch_outbox()["sent"], 2)  # one per webhook, then each budget is spent
		sent_to = [c.args[0] for c in session.post.call_args_list]
		self.assertEqual(len(sent_to), 2)
		self.assertEqual({r.webhook for r in frappe.get_all(OUTBOX, {"status": "Delivered"}, ["webhook"])}, {slow.name, fast.name})

	def test_network_errors_are_retried_and_final_4xx_do_not_trip_the_circuit(self):
		import requests

		from cred_hub.email_company import safe_http

		wh = self.webhook(deal_created=1)
		cases = (
			(patch.object(safe_http, "resolve_public", side_effect=safe_http.BlockedAddress("dns hooks.example.com: x")), None),
			(patch.object(safe_http, "resolve_public"), requests.ConnectionError("reset")),
			(patch.object(safe_http, "resolve_public"), requests.Timeout("slow")),
		)
		for resolve, error in cases:
			row = self.pending(wh)
			session = MagicMock()
			session.post.side_effect = error
			with self.subTest(error=error), resolve, patch.object(safe_http, "_session", return_value=session):
				webhooks.deliver(row.name)
				row.reload()
				self.assertEqual((row.status, row.attempts), ("Pending", 1))
		frappe.cache.delete_value(f"credhub_wh_fails:{wh.name}")
		for status in (404, 410):
			row = self.pending(wh)
			(p1, p2), _session = self.network(status)
			with p1, p2:
				webhooks.deliver(row.name)
			self.assertEqual(frappe.db.get_value(OUTBOX, row.name, "status"), "Dead")
		self.assertFalse(frappe.cache.get_value(f"credhub_wh_fails:{wh.name}"))

	def test_ten_failures_open_the_circuit(self):
		wh = self.webhook(deal_created=1)
		for _ in range(webhooks.CIRCUIT_FAILURES):
			webhooks._note_result(wh.name, False)
		self.assertTrue(frappe.cache.get_value(webhooks._circuit_key(wh.name)))

	def test_replay_keeps_ids_and_flags_replay(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		(p1, p2), _session = self.network(200)
		with p1, p2:
			webhooks.deliver(row.name)
		log = frappe.get_last_doc(webhooks.LOG_DOCTYPE, filters={"webhook": wh.name})
		webhooks.replay_webhook_log(log.name)
		replay = frappe.get_last_doc(OUTBOX, filters={"webhook": wh.name, "replay": 1})
		self.assertEqual((replay.delivery_id, replay.event_id), (row.delivery_id, row.event_id))
		(p1, p2), session = self.network(200)
		with p1, p2:
			webhooks.deliver(replay.name)
		self.assertTrue(json.loads(session.post.call_args.kwargs["data"])["replay"])

	def test_secret_rotation_signs_with_both_secrets_for_a_while(self):
		wh = self.webhook(deal_created=1)
		new_secret = webhooks.rotate_secret(wh.name)["secret"]
		row = self.pending(wh)
		(p1, p2), session = self.network(200)
		with p1, p2:
			webhooks.deliver(row.name)
		headers, raw = session.post.call_args.kwargs["headers"], session.post.call_args.kwargs["data"]
		message = f"{headers['X-HubTech-Timestamp']}.{raw}".encode()
		expected = [
			"sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest() for secret in (new_secret, "segredo")
		]
		self.assertEqual(headers["X-HubTech-Signature-V2"].split(","), expected)
		frappe.db.set_value(webhooks.WEBHOOK_DOCTYPE, wh.name, "previous_secret_until", add_to_date(now_datetime(), hours=-1))
		self.assertEqual(webhooks._signing_secrets(frappe.get_doc(webhooks.WEBHOOK_DOCTYPE, wh.name)), [new_secret])

	def test_only_https_public_addresses(self):
		for url in ("http://hooks.example.com/x", "https://localhost/x", "https://10.0.0.5/x"):
			with self.subTest(url=url), self.assertRaises(frappe.ValidationError):
				self.webhook(webhook_url=url)

	def test_response_body_is_read_up_to_the_limit(self):
		resp = MagicMock()
		resp.raw.read.return_value = b"x" * webhooks.RESPONSE_BYTES
		self.assertEqual(len(webhooks._read_body(resp)), webhooks.RESPONSE_BYTES)
		resp.raw.read.assert_called_once_with(webhooks.RESPONSE_BYTES, decode_content=True)

	# ---------- Limpeza (M5) ----------

	def test_old_logs_and_closed_outbox_are_deleted(self):
		wh = self.webhook(deal_created=1)
		row = self.pending(wh)
		(p1, p2), _session = self.network(200)
		with p1, p2:
			webhooks.deliver(row.name)
		log = frappe.get_last_doc(webhooks.LOG_DOCTYPE, filters={"webhook": wh.name})
		frappe.db.set_value(webhooks.LOG_DOCTYPE, log.name, "dispatched_on", add_days(now_datetime(), -31))
		frappe.db.set_value(OUTBOX, row.name, "modified", add_days(now_datetime(), -8), update_modified=False)
		pending = self.pending(wh)
		webhooks.delete_old_records()
		self.assertFalse(frappe.db.exists(webhooks.LOG_DOCTYPE, log.name))
		self.assertFalse(frappe.db.exists(OUTBOX, row.name))
		self.assertTrue(frappe.db.exists(OUTBOX, pending.name))
