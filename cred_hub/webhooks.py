"""Sistema de Webhooks de Saída do HubTech em Tempo Real (credtech#288, payload completo em credtech#375).

Dispara eventos HTTP assíncronos (deal.created, deal.stage_changed, deal.won, deal.lost, deal.deleted,
contact.created, contact.updated, checklist.item_rejected, checklist.item_attached, credit.limit_decided).

Corpo (versão 1, documentado em credtech docs/sistemas/cred-hub-api.md):
	{"event", "version": 1, "delivery_id", "attempt", "timestamp", "doctype", "data", "related"}
- `data` é o registro inteiro pela política de campos de `cred_hub.api_schema` (campos da empresa `ch_*` incluídos;
  senhas, tabelas, campos desativados e marcas internas nunca saem), ou os dados do evento (checklist, crédito);
- `related` traz organização(ões) e contatos do card, ou a organização do contato;
- cada webhook pode limitar os campos (`payload_fields`), sem nunca passar da política.

Assinaturas (mesmo segredo):
- X-HubTech-Signature: sha256=HMAC(segredo, corpo) — a de sempre, mantida para quem já integra;
- X-HubTech-Signature-V2: sha256=HMAC(segredo, X-HubTech-Timestamp + "." + corpo) — com o horário assinado,
  o receptor recusa entregas antigas (replay).
O delivery_id é o mesmo em todas as tentativas de uma entrega (idempotência no receptor).
"""

import hashlib
import hmac
import secrets
import time

import frappe
from frappe.utils import now_datetime

from cred_hub import api_schema

WEBHOOK_DOCTYPE = "Cred Hub Webhook"
LOG_DOCTYPE = "Cred Hub Webhook Log"
PAYLOAD_VERSION = 1
# Webhooks ativos por evento, para não consultar o banco em cada gravação de card ou contato
ACTIVE_CACHE_KEY = "credhub_webhooks_active"
ACTIVE_CACHE_SECONDS = 300

EVENT_FIELD_MAP = {
	"deal.created": "deal_created",
	"deal.stage_changed": "deal_stage_changed",
	"deal.won": "deal_won",
	"deal.lost": "deal_lost",
	"deal.deleted": "deal_deleted",
	"contact.created": "contact_created",
	"contact.updated": "contact_updated",
	"checklist.item_rejected": "checklist_item_rejected",
	"checklist.item_attached": "checklist_item_attached",
	"credit.limit_decided": "credit_limit_decided",
}


def on_deal_insert(doc, method=None):
	"""Disparado após criar um novo card (deal.created)."""
	_trigger_record_event("deal.created", doc, pipeline=doc.pipeline)


def on_deal_update(doc, method=None):
	"""Disparado na atualização de um card (deal.stage_changed, deal.won, deal.lost)."""
	if not doc.has_value_changed("status"):
		return
	events = ["deal.stage_changed"]
	stage_type = frappe.db.get_value("CRM Deal Status", doc.status, "type")
	if stage_type == "Won":
		events.append("deal.won")
	elif stage_type == "Lost":
		events.append("deal.lost")
	_trigger_record_event(events, doc, pipeline=doc.pipeline)


def on_deal_trash(doc, method=None):
	"""Disparado antes de excluir um card (deal.deleted)."""
	_trigger_record_event("deal.deleted", doc, pipeline=doc.pipeline)


def on_contact_insert(doc, method=None):
	"""Disparado após criar um contato (contact.created)."""
	_trigger_record_event("contact.created", doc)


def on_contact_update(doc, method=None):
	"""Disparado após alterar um contato (contact.updated)."""
	_trigger_record_event("contact.updated", doc)


def _deal_pipeline(deal_name: str | None) -> str | None:
	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		return frappe.db.get_value("CRM Deal", deal_name, "pipeline")
	return None


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
		rows = frappe.get_all(WEBHOOK_DOCTYPE, {"status": "Active"}, ["name", *set(flags.values())])
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
			}
			for row in rows
		]
	frappe.cache.set_value(ACTIVE_CACHE_KEY, result, expires_in_sec=ACTIVE_CACHE_SECONDS)
	return result


def clear_active_cache():
	frappe.cache.delete_value(ACTIVE_CACHE_KEY)


def _subscribers(event_name: str, pipeline: str | None) -> list[str]:
	"""Webhooks of the event; a webhook that lists pipelines only gets events of those pipelines."""
	return [
		wh["name"]
		for wh in _active_webhooks()
		if event_name in wh["events"] and not (wh["pipelines"] and pipeline and pipeline not in wh["pipelines"])
	]


# ---------- Disparo ----------


def _trigger_record_event(events, doc, pipeline: str | None = None):
	"""Record events: the payload (record + related, O(fields), two queries for related) is built only when some
	webhook wants one of the events."""
	events = [events] if isinstance(events, str) else events
	targets = [(event, _subscribers(event, pipeline)) for event in events]
	if not any(names for _event, names in targets):
		return
	data = api_schema.record(doc)
	related = api_schema.related(doc)
	for event, names in targets:
		_enqueue(names, event, data, doc.doctype, doc.name, related=related, doctype=doc.doctype)


def _trigger_event(event_name: str, payload_data: dict, pipeline: str | None, reference_doctype: str, reference_name: str):
	if event_name not in EVENT_FIELD_MAP:
		return
	names = _subscribers(event_name, pipeline)
	_enqueue(names, event_name, payload_data, reference_doctype, reference_name)


def _enqueue(names, event_name, payload_data, reference_doctype, reference_name, related=None, doctype=None):
	# Disparo em fila assíncrona (não atrasa a thread do usuário nem da API)
	for name in names:
		frappe.enqueue(
			"cred_hub.webhooks.send_webhook",
			queue="short",
			webhook_name=name,
			event_name=event_name,
			payload_data=payload_data,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			related=related,
			doctype=doctype,
			enqueue_after_commit=True,
		)


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
) -> dict:
	if doctype:
		payload_data, related = _select(payload_data, related, fields or [])
	body = {
		"event": event_name,
		"version": PAYLOAD_VERSION,
		"delivery_id": delivery_id,
		"attempt": attempt,
		"timestamp": now_datetime().isoformat(),
		"doctype": doctype,
		"data": payload_data,
	}
	if doctype:
		body["related"] = related or {}
	return body


def sign(secret: str, payload_json: str, timestamp: int) -> dict:
	"""Signature headers: V1 over the body (kept for current receivers) and V2 over "<timestamp>.<body>"."""
	key = (secret or "").encode()
	v1 = hmac.new(key, payload_json.encode(), hashlib.sha256).hexdigest()
	v2 = hmac.new(key, f"{timestamp}.{payload_json}".encode(), hashlib.sha256).hexdigest()
	return {
		"X-HubTech-Signature": f"sha256={v1}",
		"X-HubTech-Signature-V2": f"sha256={v2}",
		"X-HubTech-Timestamp": str(timestamp),
	}


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
):
	"""Executado pelo worker em segundo plano para envio HTTP com assinatura HMAC e retentativa automática."""
	if not frappe.db.exists(WEBHOOK_DOCTYPE, webhook_name):
		return

	doc = frappe.get_doc(WEBHOOK_DOCTYPE, webhook_name)
	if doc.status != "Active":
		return

	delivery_id = delivery_id or f"wh_{secrets.token_hex(12)}"
	body = build_body(
		event_name,
		payload_data,
		delivery_id,
		attempt,
		related=related,
		doctype=doctype,
		fields=parse_payload_fields(doc.get("payload_fields")),
	)
	payload_json = frappe.as_json(body)

	secret = doc.get_password("secret", raise_exception=False) or ""
	headers = {
		"Content-Type": "application/json",
		"User-Agent": "HubTech-Webhooks/1.0",
		"X-HubTech-Event": event_name,
		"X-HubTech-Delivery": delivery_id,
		"X-HubTech-Attempt": str(attempt),
		"X-HubTech-Webhook-Version": str(PAYLOAD_VERSION),
		**sign(secret, payload_json, int(time.time())),
	}

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
			resp = session.post(doc.webhook_url, data=payload_json, headers=headers, timeout=10, allow_redirects=False)
			status_code = resp.status_code
			response_text = resp.text[:2000]
			status = "Success" if resp.ok else "Failed"
			retryable = status_code in (408, 429, 500, 502, 503, 504)
		finally:
			session.close()
	except safe_http.BlockedAddress as exc:
		response_text = f"Security error: Destination blocked ({exc})"
		status_code = 0
		status = "Failed"
		retryable = False
	except Exception as exc:
		response_text = f"Connection error: {str(exc)[:1500]}"
		status_code = 0
		status = "Failed"
		retryable = True
	finally:
		duration_ms = int((time.time() - start_time) * 1000)

	# Atualiza o status de entrega no webhook
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
		if not getattr(frappe.flags, "in_test", False):
			frappe.db.commit()
	except Exception:
		frappe.log_error(title="Cred Hub Webhook: falha ao gravar log de entrega")

	# Auto-retry até 3 tentativas em falhas transitórias (5xx / timeout / 429), com o mesmo delivery_id
	if status == "Failed" and retryable and attempt < 3 and not getattr(frappe.flags, "in_test", False):
		frappe.enqueue(
			"cred_hub.webhooks.send_webhook",
			queue="short",
			webhook_name=webhook_name,
			event_name=event_name,
			payload_data=payload_data,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			attempt=attempt + 1,
			related=related,
			doctype=doctype,
			delivery_id=delivery_id,
		)


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
	"""Reenvia (Replay) uma entrega anterior a partir do registro em Cred Hub Webhook Log (nova entrega, novo id)."""
	frappe.only_for("System Manager")
	log_doc = frappe.get_doc(LOG_DOCTYPE, log_name)
	raw_payload = frappe.parse_json(log_doc.request_payload or "{}")
	raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
	send_webhook(
		webhook_name=log_doc.webhook,
		event_name=log_doc.event,
		payload_data=raw_payload.get("data") or {},
		reference_doctype=log_doc.reference_doctype or "",
		reference_name=log_doc.reference_name or "",
		attempt=1,
		related=raw_payload.get("related"),
		doctype=raw_payload.get("doctype"),
	)
	return {"replayed": True, "webhook": log_doc.webhook, "event": log_doc.event}
