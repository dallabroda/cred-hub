"""Sistema de Webhooks de Saída do HubTech em Tempo Real (credtech#288, payload completo e outbox em credtech#375).

Eventos: deal.created, deal.stage_changed, deal.won, deal.lost, deal.deleted, contact.created, contact.updated,
checklist.item_rejected, checklist.item_attached, credit.limit_decided.

Entrega (outbox transacional):
- o evento grava uma linha por webhook em `Cred Hub Webhook Outbox` na MESMA transação da mudança: se a gravação
  desfaz (erro, savepoint do lote), o evento some junto; a fila cheia nunca derruba a gravação do usuário ou da API;
- `dispatch_outbox` envia (cron de minuto + um disparo logo após o commit, deduplicado), com tentativas em
  1 min, 5 min, 30 min e 2 h, no máximo 50 por webhook por rodada e disjuntor depois de 10 falhas seguidas.

Corpo (versão 1, documentado em credtech docs/sistemas/cred-hub-api.md):
	{"event", "version": 1, "event_id", "delivery_id", "attempt", "replay", "test", "timestamp", "doctype", "data", "related"}
- `data` é o registro inteiro pela política de `cred_hub.api_schema` (campos `ch_*` incluídos; senhas, tabelas,
  campos desativados e marcas internas nunca saem), ou os dados do evento (checklist, crédito);
- `related` traz organização(ões) e contatos do card, ou a organização do contato;
- cada webhook pode limitar os campos (`payload_fields`), sem nunca passar da política;
- `event_id` é um por evento; `delivery_id` um por webhook e evento, o mesmo em todas as tentativas e no reenvio
  manual (que vem com `"replay": true`);
- `test` vem sempre, booleano: `true` só nas entregas a webhooks de teste (eventos do Sandbox das chaves de teste,
  que também levam o cabeçalho `X-HubTech-Test: 1`), `false` em produção (credtech#389, decisão 3-A).

Assinaturas:
- X-HubTech-Signature: sha256=HMAC(segredo, corpo) — a de sempre;
- X-HubTech-Signature-V2: sha256=HMAC(segredo, X-HubTech-Timestamp + "." + corpo); durante a troca de segredo vêm
  as duas, separadas por vírgula (a nova primeiro).
"""

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime

from cred_hub import api_schema, sandbox

WEBHOOK_DOCTYPE = "Cred Hub Webhook"
LOG_DOCTYPE = "Cred Hub Webhook Log"
OUTBOX_DOCTYPE = "Cred Hub Webhook Outbox"
PAYLOAD_VERSION = 1
# Webhooks ativos por evento, para não consultar o banco em cada gravação de card ou contato
ACTIVE_CACHE_KEY = "credhub_webhooks_active"
ACTIVE_CACHE_SECONDS = 300
# Tentativas: a primeira na hora, depois estes intervalos (em segundos); depois da última, a entrega morre
BACKOFF_SECONDS = (60, 5 * 60, 30 * 60, 2 * 60 * 60)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
PER_WEBHOOK_PER_RUN = 50
BATCH_SIZE = 200
CIRCUIT_FAILURES = 10
CIRCUIT_SECONDS = 15 * 60
RESPONSE_BYTES = 2048
LOG_DAYS = 30
OUTBOX_DAYS = 7
SECRET_OVERLAP_HOURS = 24
DISPATCH_JOB_ID = "credhub_webhook_dispatch"
# A run stops taking rows after this many seconds (the short queue kills a job at 300 s); one webhook gets at most
# WEBHOOK_SECONDS_PER_RUN of it, so a slow endpoint never holds the others back
RUN_SECONDS = 240
WEBHOOK_SECONDS_PER_RUN = 60
# A row claimed by a run that died (worker killed) goes back to Pending after this; it may then be sent twice,
# which the receiver absorbs with the delivery_id
STALE_CLAIM_MINUTES = 10

EVENT_FIELD_MAP = {
	"deal.created": "deal_created",
	"deal.stage_changed": "deal_stage_changed",
	"deal.updated": "deal_updated",
	"deal.won": "deal_won",
	"deal.lost": "deal_lost",
	"deal.deleted": "deal_deleted",
	"contact.created": "contact_created",
	"contact.updated": "contact_updated",
	"organization.created": "organization_created",
	"organization.updated": "organization_updated",
	"cedente.ready_to_register": "cedente_ready_to_register",
	"checklist.item_rejected": "checklist_item_rejected",
	"checklist.item_attached": "checklist_item_attached",
	"credit.limit_decided": "credit_limit_decided",
	"title.promise_recorded": "title_promise_recorded",
	"title.agreement_recorded": "title_agreement_recorded",
}
OUTCOME_EVENTS = {"Won": "deal.won", "Lost": "deal.lost"}


def on_deal_insert(doc, method=None):
	"""Disparado após criar um novo card (deal.created)."""
	_trigger_record_event("deal.created", doc, pipeline=doc.pipeline)


def on_deal_update(doc, method=None):
	"""Card alterado: deal.stage_changed quando a etapa muda; deal.won/deal.lost quando o status do card (outcome)
	vira Ganho/Perdido; deal.updated para outras alterações relevantes nos campos públicos do card."""
	if doc.flags.in_insert:
		return
	events = []
	if doc.has_value_changed("status"):
		events.append("deal.stage_changed")
	if doc.has_value_changed("outcome") and doc.get("outcome") in OUTCOME_EVENTS:
		events.append(OUTCOME_EVENTS[doc.outcome])
	before = doc.get_doc_before_save()
	if before is not None:
		ignore = {"status", "outcome", "closed_date"} if events else set()
		if _record_changed(before, doc, ignore=ignore):
			events.append("deal.updated")
	if events:
		_trigger_record_event(events, doc, pipeline=doc.pipeline)
	if before is not None:
		_emit_collection_title_events(before, doc)


def _emit_collection_title_events(before, doc):
	"""Detecta novas promessas de pagamento (titles) e novos acordos parcelados (installments) no card."""
	before_titles = {r.name: r for r in (before.get("titles") or []) if r.get("name")}
	for row in doc.get("titles") or []:
		new_date = row.get("promise_date")
		if not new_date:
			continue
		old = before_titles.get(row.name)
		old_date = old.get("promise_date") if old else None
		old_amt = _plain(old.get("promise_amount")) if old else ""
		if _plain(new_date) != _plain(old_date) or _plain(row.get("promise_amount")) != old_amt:
			trigger_title_promise(doc, row.name, str(new_date), float(row.get("promise_amount") or 0))

	before_inst = {r.name for r in (before.get("installments") or []) if r.get("name")}
	new_slots_by_title: dict[str, list] = {}
	for slot in doc.get("installments") or []:
		if slot.get("name") in before_inst or slot.get("paid_on") or slot.get("cancelled_on"):
			continue
		if slot.get("title"):
			new_slots_by_title.setdefault(slot.title, []).append(slot)

	if new_slots_by_title:
		titles_by_name = {r.name: r for r in (doc.get("titles") or []) if r.get("name")}
		for title_row_name, slots in new_slots_by_title.items():
			t_row = titles_by_name.get(title_row_name)
			trigger_title_agreement(
				doc,
				{
					"title_row": title_row_name,
					"title_number": t_row.get("title_number") if t_row else None,
					"erp_source": t_row.get("erp_source") if t_row else None,
					"erp_title_id": t_row.get("erp_title_id") if t_row else None,
					"installments_count": len(slots),
					"total_amount": round(sum(float(s.get("amount") or 0) for s in slots), 2),
					"installments": [
						{
							"number": s.get("number"),
							"due_date": str(s.get("due_date")) if s.get("due_date") else None,
							"amount": float(s.get("amount") or 0),
						}
						for s in slots
					],
				},
			)


def on_deal_trash(doc, method=None):
	"""Disparado antes de excluir um card (deal.deleted)."""
	_trigger_record_event("deal.deleted", doc, pipeline=doc.pipeline)


def on_contact_insert(doc, method=None):
	"""Disparado após criar um contato (contact.created)."""
	_trigger_record_event("contact.created", doc)


def on_contact_update(doc, method=None):
	"""contact.updated só quando algum campo que sai no corpo mudou (salvar sem mudar nada não dispara)."""
	if doc.flags.in_insert:
		return
	before = doc.get_doc_before_save()
	if before is None or not _record_changed(before, doc):
		return
	_trigger_record_event("contact.updated", doc)


def on_organization_insert(doc, method=None):
	"""Disparado após criar uma organização (organization.created)."""
	_trigger_record_event("organization.created", doc)


def on_organization_update(doc, method=None):
	"""organization.updated só quando algum campo que sai no corpo mudou."""
	if doc.flags.in_insert:
		return
	before = doc.get_doc_before_save()
	if before is None or not _record_changed(before, doc):
		return
	_trigger_record_event("organization.updated", doc)


def _record_changed(before, doc, ignore: set[str] | None = None) -> bool:
	ignored = {"modified", "creation"} | (ignore or set())
	for fieldname in api_schema.public_fieldnames(doc.doctype):
		if fieldname in ignored:
			continue
		if _plain(before.get(fieldname)) != _plain(doc.get(fieldname)):
			return True
	return False


def _plain(value) -> str:
	"""Compare a value read from the database with one set in memory (0/None/"" and date vs text are the same)."""
	return "" if value in (None, "", 0) else str(value)


def trigger_checklist_rejected(
	organization_name: str,
	deal_name: str | None,
	item_key: str,
	label: str,
	reason: str,
	portal_url: str,
):
	"""Dispara evento checklist.item_rejected quando um documento da Pasta Digital é recusado."""
	payload = {
		"organization": organization_name,
		"deal": deal_name,
		"item_key": item_key,
		"document_label": label,
		"rejection_reason": reason,
		"portal_url": portal_url,
	}
	_trigger_event(
		"checklist.item_rejected",
		payload,
		pipeline=_deal_pipeline(deal_name),
		reference_doctype="CRM Deal" if deal_name else "CRM Organization",
		reference_name=deal_name or organization_name,
	)


def trigger_checklist_attached(
	organization_name: str,
	deal_name: str | None,
	item_key: str,
	label: str,
	file_name: str,
	file_url: str,
	source: str,
):
	"""checklist.item_attached: a document arrived in a checklist item (screen, API or the client's external link)."""
	payload = {
		"organization": organization_name,
		"deal": deal_name,
		"item_key": item_key,
		"document_label": label,
		"status": "attached",
		"file_name": file_name,
		"file_url": file_url,
		"source": source,
	}
	_trigger_event(
		"checklist.item_attached",
		payload,
		pipeline=_deal_pipeline(deal_name),
		reference_doctype="CRM Deal" if deal_name else "CRM Organization",
		reference_name=deal_name or organization_name,
	)


def trigger_credit_limit_decided(
	organization_name: str,
	deal_name: str | None,
	committee_payload: dict,
):
	"""Dispara evento credit.limit_decided quando a Mesa de Comitê de Crédito aprova ou reprova um limite."""
	_trigger_event(
		"credit.limit_decided",
		committee_payload,
		pipeline=_deal_pipeline(deal_name),
		reference_doctype="CRM Deal" if deal_name else "CRM Organization",
		reference_name=deal_name or organization_name,
	)


def has_active_subscribers(event_name: str, pipeline: str | None = None, reference_doctype=None, reference_name=None) -> bool:
	"""Retorna True se houver pelo menos um webhook ativo inscrito no evento para o funil correspondente."""
	sandbox_event = sandbox.is_sandbox_pipeline(pipeline) or sandbox.is_sandbox_record(reference_doctype, reference_name)
	return bool(_subscribers(event_name, pipeline, reference_doctype, reference_name, sandbox_event))


def trigger_cedente_ready_to_register(deal_or_name):
	"""cedente.ready_to_register: card atingiu etapa de cadastro de cedente no ERP externo."""
	deal = frappe.get_doc("CRM Deal", deal_or_name) if isinstance(deal_or_name, str) else deal_or_name
	if not has_active_subscribers("cedente.ready_to_register", pipeline=deal.pipeline, reference_doctype="CRM Deal", reference_name=deal.name):
		return
	from cred_hub.erp.cedente import cedente_payload

	payload = cedente_payload(deal)
	_trigger_event(
		"cedente.ready_to_register",
		payload,
		pipeline=deal.pipeline,
		reference_doctype="CRM Deal",
		reference_name=deal.name,
	)


def trigger_title_promise(deal_or_name, title_row_name: str, promise_date: str, promise_amount: float):
	"""title.promise_recorded: promessa de pagamento registrada no card para um título."""
	deal = frappe.get_doc("CRM Deal", deal_or_name) if isinstance(deal_or_name, str) else deal_or_name
	if not has_active_subscribers("title.promise_recorded", pipeline=deal.pipeline, reference_doctype="CRM Deal", reference_name=deal.name):
		return
	row = next((r for r in (deal.get("titles") or []) if r.name == title_row_name), None)
	payload = {
		"deal": deal.name,
		"pipeline": deal.pipeline,
		"title_row": title_row_name,
		"title_number": row.get("title_number") if row else None,
		"erp_source": row.get("erp_source") if row else None,
		"erp_title_id": row.get("erp_title_id") if row else None,
		"promise_date": promise_date,
		"promise_amount": promise_amount,
	}
	_trigger_event(
		"title.promise_recorded",
		payload,
		pipeline=deal.pipeline,
		reference_doctype="CRM Deal",
		reference_name=deal.name,
	)


def trigger_title_agreement(deal_or_name, agreement_payload: dict):
	"""title.agreement_recorded: acordo de parcelamento/repactuação registrado."""
	deal = frappe.get_doc("CRM Deal", deal_or_name) if isinstance(deal_or_name, str) else deal_or_name
	if not has_active_subscribers("title.agreement_recorded", pipeline=deal.pipeline, reference_doctype="CRM Deal", reference_name=deal.name):
		return
	payload = {
		"deal": deal.name,
		"pipeline": deal.pipeline,
		**agreement_payload,
	}
	_trigger_event(
		"title.agreement_recorded",
		payload,
		pipeline=deal.pipeline,
		reference_doctype="CRM Deal",
		reference_name=deal.name,
	)


def _deal_pipeline(deal_name: str | None) -> str | None:
	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		return frappe.db.get_value("CRM Deal", deal_name, "pipeline")
	return None


# ---------- Quem recebe ----------


def _active_webhooks() -> list[dict]:
	"""[{name, events: [...], pipelines: [...]}] of the active webhooks: two queries, cached for a few minutes and
	dropped when a webhook changes (CredHubWebhook.on_update/on_trash)."""
	cached = frappe.cache.get_value(ACTIVE_CACHE_KEY)
	if cached is not None:
		return cached
	result = []
	if frappe.db.exists("DocType", WEBHOOK_DOCTYPE):
		meta = frappe.get_meta(WEBHOOK_DOCTYPE)
		flags = {event: field for event, field in EVENT_FIELD_MAP.items() if meta.has_field(field)}
		extra = ["is_test"] if meta.has_field("is_test") else []
		rows = frappe.get_all(WEBHOOK_DOCTYPE, {"status": "Active"}, ["name", *set(flags.values()), *extra])
		pipelines: dict[str, list] = {}
		if rows:
			for row in frappe.get_all(
				"Cred Hub Webhook Pipeline",
				{"parenttype": WEBHOOK_DOCTYPE, "parent": ("in", [r.name for r in rows])},
				["parent", "pipeline"],
			):
				pipelines.setdefault(row.parent, []).append(row.pipeline)
		result = [
			{
				"name": row.name,
				"events": [event for event, field in flags.items() if row.get(field)],
				"pipelines": pipelines.get(row.name, []),
				"is_test": bool(row.get("is_test")),
			}
			for row in rows
		]
	frappe.cache.set_value(ACTIVE_CACHE_KEY, result, expires_in_sec=ACTIVE_CACHE_SECONDS)
	return result


def clear_active_cache():
	frappe.cache.delete_value(ACTIVE_CACHE_KEY)


def _record_pipelines(reference_doctype: str | None, reference_name: str | None) -> set[str]:
	"""Pipelines an event without its own pipeline belongs to: the cards of the contact or of the organization."""
	if not reference_name:
		return set()
	if reference_doctype == "CRM Deal":
		return {p for p in [frappe.db.get_value("CRM Deal", reference_name, "pipeline")] if p}
	if reference_doctype == "Contact":
		deals = frappe.get_all("CRM Contacts", {"contact": reference_name, "parenttype": "CRM Deal"}, pluck="parent")
		filters = {"name": ("in", deals)} if deals else None
	elif reference_doctype == "CRM Organization":
		filters = {"organization": reference_name}
	else:
		return set()
	if not filters:
		return set()
	return set(frappe.get_all("CRM Deal", filters, pluck="pipeline", distinct=True)) - {None, ""}


def _subscribers(
	event_name: str, pipeline: str | None, reference_doctype=None, reference_name=None, sandbox_event=False
) -> list[str]:
	"""Webhooks of the event. A webhook that lists pipelines only gets events of those pipelines; an event without a
	pipeline (contact, organization) reaches it only when a card of the record is in one of them (audit M3).
	Events of sandbox records go only to test webhooks, and test webhooks get nothing else (credtech#375)."""
	webhooks = [
		wh
		for wh in _active_webhooks()
		if event_name in wh["events"] and bool(wh.get("is_test")) == bool(sandbox_event)
	]
	if not webhooks:
		return []
	event_pipelines = None
	names = []
	for wh in webhooks:
		if wh["pipelines"]:
			if event_pipelines is None:
				event_pipelines = {pipeline} if pipeline else _record_pipelines(reference_doctype, reference_name)
			if not event_pipelines & set(wh["pipelines"]):
				continue
		names.append(wh["name"])
	return names


# ---------- Disparo: grava na outbox ----------


def _trigger_record_event(events, doc, pipeline: str | None = None):
	"""Record events: the payload (record + related, O(fields), two queries for related) is built only when some
	webhook wants one of the events."""
	events = [events] if isinstance(events, str) else events
	sandbox_event = sandbox.is_sandbox_record(doc.doctype, doc.name, doc)
	targets = [(event, _subscribers(event, pipeline, doc.doctype, doc.name, sandbox_event)) for event in events]
	if not any(names for _event, names in targets):
		return
	data = api_schema.record(doc)
	related = api_schema.related(doc)
	for event, names in targets:
		_write_outbox(names, event, data, doc.doctype, doc.name, related=related, doctype=doc.doctype)


def _trigger_event(event_name: str, payload_data: dict, pipeline: str | None, reference_doctype: str, reference_name: str):
	if event_name not in EVENT_FIELD_MAP:
		return
	sandbox_event = sandbox.is_sandbox_pipeline(pipeline) or sandbox.is_sandbox_record(reference_doctype, reference_name)
	names = _subscribers(event_name, pipeline, reference_doctype, reference_name, sandbox_event)
	_write_outbox(names, event_name, payload_data, reference_doctype, reference_name)


def _write_outbox(
	names,
	event_name,
	payload_data,
	reference_doctype,
	reference_name,
	related=None,
	doctype=None,
	event_id=None,
	delivery_id=None,
	replay=False,
):
	"""One outbox row per webhook, in the caller's transaction (rolled back with it). Nothing is sent here."""
	if not names:
		return
	event_id = event_id or f"evt_{secrets.token_hex(12)}"
	payload = frappe.as_json({"data": payload_data, "related": related, "doctype": doctype}, indent=None)
	now = now_datetime()
	for name in names:
		frappe.get_doc(
			{
				"doctype": OUTBOX_DOCTYPE,
				"webhook": name,
				"event": event_name,
				"status": "Pending",
				"attempts": 0,
				"next_attempt_at": now,
				"event_id": event_id,
				"delivery_id": delivery_id or f"wh_{secrets.token_hex(12)}",
				"replay": int(bool(replay)),
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"payload": payload,
			}
		).insert(ignore_permissions=True)
	_kick_dispatcher()


def _kick_dispatcher():
	"""After the commit, ask a worker to send now (one job, deduplicated). Any error is swallowed: the save already
	happened and the minute cron sends anyway (audit H4: a full queue never breaks a save)."""
	if getattr(frappe.flags, "in_test", False) or frappe.flags.get("credhub_webhook_kick"):
		return
	frappe.flags.credhub_webhook_kick = True

	def kick():
		frappe.flags.credhub_webhook_kick = False
		try:
			frappe.enqueue(
				"cred_hub.webhooks.dispatch_outbox", queue="short", job_id=DISPATCH_JOB_ID, deduplicate=True
			)
		except Exception:
			pass

	frappe.db.after_commit.add(kick)
	frappe.db.after_rollback.add(lambda: frappe.flags.pop("credhub_webhook_kick", None))


# ---------- Envio ----------


def _lock(name: str, seconds: int) -> bool:
	try:
		return bool(frappe.cache.set(frappe.cache.make_key(name), 1, nx=True, ex=seconds))
	except Exception:
		return True


def _unlock(name: str):
	try:
		frappe.cache.delete(frappe.cache.make_key(name))
	except Exception:
		pass


def _circuit_key(webhook: str) -> str:
	return f"credhub_wh_circuit:{webhook}"


def _note_result(webhook: str, ok: bool):
	"""Disjuntor: 10 falhas seguidas pausam o webhook por 15 min (as entregas esperam na outbox)."""
	fails_key = f"credhub_wh_fails:{webhook}"
	if ok:
		frappe.cache.delete_value(fails_key)
		return
	fails = frappe.cache.incrby(frappe.cache.make_key(fails_key), 1)
	frappe.cache.expire(frappe.cache.make_key(fails_key), 3600)
	if fails >= CIRCUIT_FAILURES:
		frappe.cache.set_value(_circuit_key(webhook), 1, expires_in_sec=CIRCUIT_SECONDS)
		frappe.cache.delete_value(fails_key)


def _release_stale_claims():
	cutoff = add_to_date(now_datetime(), minutes=-STALE_CLAIM_MINUTES)
	frappe.db.sql(
		f"update `tab{OUTBOX_DOCTYPE}` set status='Pending', claimed_at=null where status='Sending' and claimed_at < %s",
		(cutoff,),
	)


def _claim(name: str) -> bool:
	"""Atomic: only one run moves a row from Pending to Sending (committed before the HTTP call). The claim is
	proved by reading back this run's own random token, never by driver internals: no claim, no send (fail
	closed)."""
	token = secrets.token_hex(12)
	frappe.db.sql(
		f"update `tab{OUTBOX_DOCTYPE}` set status='Sending', claimed_at=%s, claim_token=%s "
		"where name=%s and status='Pending'",
		(now_datetime(), token, name),
	)
	claimed = frappe.db.get_value(OUTBOX_DOCTYPE, name, "claim_token") == token
	if not getattr(frappe.flags, "in_test", False):
		frappe.db.commit()
	return claimed


def dispatch_outbox(limit: int = BATCH_SIZE) -> dict:
	"""Send what is due in the outbox (cron every minute and the kick after commit). Rows are claimed one by one,
	so two runs never send the same row; each run is bounded in time overall and per webhook."""
	if not frappe.db.exists("DocType", OUTBOX_DOCTYPE):
		return {"sent": 0, "failed": 0}
	locked = _lock("credhub_webhook_dispatch_lock", RUN_SECONDS + 60)
	sent = failed = 0
	started = time.monotonic()
	try:
		_release_stale_claims()
		rows = frappe.get_all(
			OUTBOX_DOCTYPE,
			{"status": "Pending", "next_attempt_at": ("<=", now_datetime())},
			["name", "webhook"],
			order_by="next_attempt_at asc, creation asc",
			limit_page_length=limit,
		)
		per_webhook: dict[str, int] = {}
		spent: dict[str, float] = {}
		for row in rows:
			if time.monotonic() - started > RUN_SECONDS:
				break
			if frappe.cache.get_value(_circuit_key(row.webhook)):
				continue
			if per_webhook.get(row.webhook, 0) >= PER_WEBHOOK_PER_RUN:
				continue
			if row.webhook in spent and spent[row.webhook] > WEBHOOK_SECONDS_PER_RUN:
				continue
			if not _claim(row.name):
				continue
			per_webhook[row.webhook] = per_webhook.get(row.webhook, 0) + 1
			t0 = time.monotonic()
			ok = deliver(row.name)
			spent[row.webhook] = spent.get(row.webhook, 0.0) + (time.monotonic() - t0)
			if ok:
				sent += 1
			else:
				failed += 1
	finally:
		if locked:
			_unlock("credhub_webhook_dispatch_lock")
	return {"sent": sent, "failed": failed}


def deliver(outbox_name: str) -> bool:
	"""Send one outbox row and schedule its next attempt or close it."""
	row = frappe.get_doc(OUTBOX_DOCTYPE, outbox_name)
	if row.status not in ("Pending", "Sending"):
		return row.status == "Delivered"
	payload = json.loads(row.payload or "{}")
	attempt = (row.attempts or 0) + 1
	result = send_webhook(
		webhook_name=row.webhook,
		event_name=row.event,
		payload_data=payload.get("data") or {},
		reference_doctype=row.reference_doctype or "",
		reference_name=row.reference_name or "",
		attempt=attempt,
		related=payload.get("related"),
		doctype=payload.get("doctype"),
		delivery_id=row.delivery_id,
		event_id=row.event_id,
		replay=bool(row.replay),
	)
	values = {
		"attempts": attempt,
		"claimed_at": None,
		"last_error": "" if result["ok"] else (result["error"] or "")[:500],
	}
	if result["ok"]:
		values["status"] = "Delivered"
	elif result["retryable"] and attempt < MAX_ATTEMPTS:
		values["status"] = "Pending"
		values["next_attempt_at"] = add_to_date(now_datetime(), seconds=BACKOFF_SECONDS[attempt - 1])
	else:
		values["status"] = "Dead"
	frappe.db.set_value(OUTBOX_DOCTYPE, row.name, values, update_modified=False)
	if result["sent"] and (result["ok"] or result["retryable"]):
		# only success and transient failures move the breaker; a final 4xx (404, 410...) is the endpoint's
		# answer, not an outage
		_note_result(row.webhook, result["ok"])
	if not getattr(frappe.flags, "in_test", False):
		frappe.db.commit()
	return result["ok"]


# ---------- Corpo e assinatura ----------


def parse_payload_fields(value: str | None) -> list[str]:
	"""Field list of a webhook: one name per line or separated by commas; empty means every field."""
	names = [part.strip() for line in (value or "").splitlines() for part in line.split(",")]
	return list(dict.fromkeys(n for n in names if n))


def _select(data: dict, related: dict | None, fields: list[str]) -> tuple[dict, dict | None]:
	"""Only the listed keys of the record and of `related` (name always goes). The record already passed the field
	policy, so a list can only narrow it."""
	if not fields:
		if not api_schema.CREDIT_FIELDS_IN_WEBHOOKS_BY_DEFAULT:
			data = {k: v for k, v in data.items() if k not in api_schema.WEBHOOK_OPT_IN_FIELDS}
		return data, related
	wanted = set(fields) | {"name"}
	data = {k: v for k, v in data.items() if k in wanted}
	if related is not None:
		related = {k: v for k, v in related.items() if k in wanted}
	return data, related


def build_body(
	event_name: str,
	payload_data: dict,
	delivery_id: str,
	attempt: int,
	related: dict | None = None,
	doctype: str | None = None,
	fields: list[str] | None = None,
	event_id: str | None = None,
	replay: bool = False,
	test: bool = False,
) -> dict:
	if doctype:
		payload_data, related = _select(payload_data, related, fields or [])
	body = {
		"event": event_name,
		"version": PAYLOAD_VERSION,
		"event_id": event_id or delivery_id,
		"delivery_id": delivery_id,
		"attempt": attempt,
		"replay": bool(replay),
		# always present: true only for test webhooks (Sandbox of the test keys), same as X-HubTech-Test (credtech#389)
		"test": bool(test),
		# UTC, with offset (audit L8)
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"doctype": doctype,
		"data": payload_data,
	}
	if doctype:
		body["related"] = related or {}
	return body


def sign(secret: str | list[str], payload_json: str, timestamp: int) -> dict:
	"""Signature headers: V1 over the body with the current secret (kept for current receivers) and V2 over
	"<timestamp>.<body>" with the current secret and, during a rotation, the previous one too."""
	secrets_ = [s for s in ([secret] if isinstance(secret, str) else secret) if s is not None]
	current = (secrets_[0] if secrets_ else "").encode()
	v1 = hmac.new(current, payload_json.encode(), hashlib.sha256).hexdigest()
	v2 = ",".join(
		"sha256=" + hmac.new(s.encode(), f"{timestamp}.{payload_json}".encode(), hashlib.sha256).hexdigest()
		for s in (secrets_ or [""])
	)
	return {
		"X-HubTech-Signature": f"sha256={v1}",
		"X-HubTech-Signature-V2": v2,
		"X-HubTech-Timestamp": str(timestamp),
	}


def _signing_secrets(doc) -> list[str]:
	secrets_ = [doc.get_password("secret", raise_exception=False) or ""]
	until = doc.get("previous_secret_until")
	if until and get_datetime(until) > now_datetime():
		previous = doc.get_password("previous_secret", raise_exception=False)
		if previous:
			secrets_.append(previous)
	return secrets_


def _read_body(resp) -> str:
	"""At most RESPONSE_BYTES of the answer (a huge or endless body never fills the worker)."""
	try:
		chunk = resp.raw.read(RESPONSE_BYTES, decode_content=True)
	except Exception:
		chunk = None
	if isinstance(chunk, bytes):
		return chunk.decode("utf-8", "replace")
	return str(getattr(resp, "text", "") or "")[:RESPONSE_BYTES]


def send_webhook(
	webhook_name: str,
	event_name: str,
	payload_data: dict,
	reference_doctype: str = "",
	reference_name: str = "",
	attempt: int = 1,
	related: dict | None = None,
	doctype: str | None = None,
	delivery_id: str | None = None,
	event_id: str | None = None,
	replay: bool = False,
) -> dict:
	"""One HTTP delivery, signed, logged in Cred Hub Webhook Log. Returns {ok, retryable, sent, error}; retries are
	the outbox's job (deliver)."""
	if not frappe.db.exists(WEBHOOK_DOCTYPE, webhook_name):
		return {"ok": False, "retryable": False, "sent": False, "error": "webhook not found"}

	doc = frappe.get_doc(WEBHOOK_DOCTYPE, webhook_name)
	if doc.status != "Active":
		return {"ok": False, "retryable": False, "sent": False, "error": "webhook disabled"}

	delivery_id = delivery_id or f"wh_{secrets.token_hex(12)}"
	body = build_body(
		event_name,
		payload_data,
		delivery_id,
		attempt,
		related=related,
		doctype=doctype,
		fields=parse_payload_fields(doc.get("payload_fields")),
		event_id=event_id,
		replay=replay,
		test=bool(doc.get("is_test")),
	)
	payload_json = frappe.as_json(body)

	headers = {
		"Content-Type": "application/json",
		"User-Agent": "HubTech-Webhooks/1.0",
		"X-HubTech-Event": event_name,
		"X-HubTech-Delivery": delivery_id,
		"X-HubTech-Attempt": str(attempt),
		"X-HubTech-Webhook-Version": str(PAYLOAD_VERSION),
		**sign(_signing_secrets(doc), payload_json, int(time.time())),
	}
	if doc.get("is_test"):
		headers[sandbox.TEST_HEADER] = "1"

	start_time = time.time()
	status = "Failed"
	status_code = None
	response_text = ""
	retryable = False

	try:
		from cred_hub.email_company import safe_http

		host = safe_http.check_url(doc.webhook_url)
		parts = safe_http.urlsplit(doc.webhook_url)
		scheme = parts.scheme.lower()
		safe_http.resolve_public(host, parts.port or safe_http.PORTS[scheme])

		session = safe_http._session()
		try:
			resp = session.post(
				doc.webhook_url, data=payload_json, headers=headers, timeout=10, allow_redirects=False, stream=True
			)
			try:
				status_code = resp.status_code
				response_text = _read_body(resp)
			finally:
				resp.close()
			status = "Success" if 200 <= (status_code or 0) < 300 else "Failed"
			retryable = status_code in (408, 425, 429, 500, 502, 503, 504)
		finally:
			session.close()
	except safe_http.BlockedAddress as exc:
		response_text = f"Security error: Destination blocked ({exc})"
		status_code = 0
		# a name that does not resolve right now is transient; a private/blocked address is final
		retryable = str(exc).startswith("dns ")
	except Exception as exc:
		response_text = f"Connection error: {str(exc)[:1500]}"
		status_code = 0
		retryable = True
	duration_ms = int((time.time() - start_time) * 1000)

	try:
		frappe.db.set_value(
			WEBHOOK_DOCTYPE,
			webhook_name,
			{
				"last_delivery_on": now_datetime(),
				"last_delivery_status": status,
				"last_delivery_code": status_code or 0,
			},
			update_modified=False,
		)
		if frappe.db.exists("DocType", LOG_DOCTYPE):
			frappe.get_doc(
				{
					"doctype": LOG_DOCTYPE,
					"webhook": webhook_name,
					"event": event_name,
					"delivery_id": delivery_id,
					"reference_doctype": reference_doctype,
					"reference_name": reference_name,
					"status": status,
					"response_code": status_code or 0,
					"duration_ms": duration_ms,
					"url": doc.webhook_url,
					"request_payload": payload_json,
					"response_body": response_text,
					"dispatched_on": now_datetime(),
				}
			).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Cred Hub Webhook: falha ao gravar log de entrega")

	return {"ok": status == "Success", "retryable": retryable, "sent": True, "error": response_text if status != "Success" else ""}


# ---------- Telas do Desk (System Manager) ----------


@frappe.whitelist(methods=["POST"])
def send_test_ping(webhook_name: str) -> dict:
	"""Envia um evento de teste (webhook.ping) imediato para validar a URL e a assinatura HMAC."""
	frappe.only_for("System Manager")
	send_webhook(
		webhook_name=webhook_name,
		event_name="webhook.ping",
		payload_data={"message": "HubTech CRM Webhook Ping Test", "webhook": webhook_name},
		reference_doctype=WEBHOOK_DOCTYPE,
		reference_name=webhook_name,
		attempt=1,
	)
	doc = frappe.get_doc(WEBHOOK_DOCTYPE, webhook_name)
	return {
		"status": doc.last_delivery_status,
		"status_code": doc.last_delivery_code,
		"last_delivery_on": doc.last_delivery_on,
	}


@frappe.whitelist(methods=["POST"])
def replay_webhook_log(log_name: str) -> dict:
	"""Reenvia uma entrega do registro: mesma delivery_id e event_id, `"replay": true`, pela outbox."""
	frappe.only_for("System Manager")
	log_doc = frappe.get_doc(LOG_DOCTYPE, log_name)
	raw_payload = frappe.parse_json(log_doc.request_payload or "{}")
	raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
	_write_outbox(
		[log_doc.webhook],
		log_doc.event,
		raw_payload.get("data") or {},
		log_doc.reference_doctype or "",
		log_doc.reference_name or "",
		related=raw_payload.get("related"),
		doctype=raw_payload.get("doctype"),
		event_id=raw_payload.get("event_id"),
		delivery_id=log_doc.delivery_id,
		replay=True,
	)
	return {"replayed": True, "webhook": log_doc.webhook, "event": log_doc.event, "delivery_id": log_doc.delivery_id}


@frappe.whitelist(methods=["POST"])
def rotate_secret(webhook_name: str) -> dict:
	"""Novo segredo, mostrado UMA vez; o antigo continua assinando (segunda assinatura V2) por 24 h para o receptor
	trocar sem perder entregas (audit L8)."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(WEBHOOK_DOCTYPE, webhook_name)
	doc.previous_secret = doc.get_password("secret", raise_exception=False)
	doc.previous_secret_until = add_to_date(now_datetime(), hours=SECRET_OVERLAP_HOURS)
	new_secret = secrets.token_hex(24)
	doc.secret = new_secret
	doc.save(ignore_permissions=True)
	return {"secret": new_secret, "previous_valid_until": doc.previous_secret_until}


# ---------- Limpeza (diária) ----------


def delete_old_records():
	"""Registro de entregas por 30 dias (traz dados pessoais do corpo) e outbox encerrada por 7 dias, em lotes."""
	for doctype, filters in (
		(LOG_DOCTYPE, {"dispatched_on": ("<", add_days(now_datetime(), -LOG_DAYS))}),
		(OUTBOX_DOCTYPE, {"status": ("in", ("Delivered", "Dead")), "modified": ("<", add_days(now_datetime(), -OUTBOX_DAYS))}),
	):
		if not frappe.db.exists("DocType", doctype):
			continue
		while True:
			names = frappe.get_all(doctype, filters, pluck="name", limit_page_length=5000)
			if not names:
				break
			frappe.db.delete(doctype, {"name": ("in", names)})
			frappe.db.commit()
			if len(names) < 5000:
				break
