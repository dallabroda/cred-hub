"""Sistema de Webhooks de Saída do HubTech em Tempo Real (credtech#288).

Dispara eventos HTTP assíncronos (deal.created, deal.stage_changed, deal.won, deal.lost, deal.deleted,
contact.created, contact.updated) com validação de integridade via HMAC-SHA256 (X-HubTech-Signature).
"""

import hashlib
import hmac
import secrets
import time
import requests

import frappe
from frappe.utils import now_datetime

WEBHOOK_DOCTYPE = "Cred Hub Webhook"
LOG_DOCTYPE = "Cred Hub Webhook Log"

EVENT_FIELD_MAP = {
	"deal.created": "deal_created",
	"deal.stage_changed": "deal_stage_changed",
	"deal.won": "deal_won",
	"deal.lost": "deal_lost",
	"deal.deleted": "deal_deleted",
	"contact.created": "contact_created",
	"contact.updated": "contact_updated",
}


def on_deal_insert(doc, method=None):
	"""Disparado após criar um novo card (deal.created)."""
	if frappe.flags.in_test or not frappe.db.exists("DocType", WEBHOOK_DOCTYPE):
		pass
	_trigger_event("deal.created", _deal_payload(doc), pipeline=doc.pipeline, reference_doctype="CRM Deal", reference_name=doc.name)


def on_deal_update(doc, method=None):
	"""Disparado na atualização de um card (deal.stage_changed, deal.won, deal.lost)."""
	if not doc.has_value_changed("status"):
		return

	payload = _deal_payload(doc)
	_trigger_event("deal.stage_changed", payload, pipeline=doc.pipeline, reference_doctype="CRM Deal", reference_name=doc.name)

	stage_type = frappe.db.get_value("CRM Deal Status", doc.status, "type")
	if stage_type == "Won":
		_trigger_event("deal.won", payload, pipeline=doc.pipeline, reference_doctype="CRM Deal", reference_name=doc.name)
	elif stage_type == "Lost":
		_trigger_event("deal.lost", payload, pipeline=doc.pipeline, reference_doctype="CRM Deal", reference_name=doc.name)


def on_deal_trash(doc, method=None):
	"""Disparado antes de excluir um card (deal.deleted)."""
	_trigger_event("deal.deleted", _deal_payload(doc), pipeline=doc.pipeline, reference_doctype="CRM Deal", reference_name=doc.name)


def on_contact_insert(doc, method=None):
	"""Disparado após criar um contato (contact.created)."""
	_trigger_event("contact.created", _contact_payload(doc), pipeline=None, reference_doctype="Contact", reference_name=doc.name)


def on_contact_update(doc, method=None):
	"""Disparado após alterar um contato (contact.updated)."""
	_trigger_event("contact.updated", _contact_payload(doc), pipeline=None, reference_doctype="Contact", reference_name=doc.name)


def _deal_payload(doc) -> dict:
	return {
		"name": doc.name,
		"pipeline": doc.pipeline,
		"status": doc.status,
		"organization": getattr(doc, "organization", None),
		"organization_name": getattr(doc, "organization_name", None),
		"annual_revenue": getattr(doc, "annual_revenue", None),
		"deal_owner": getattr(doc, "deal_owner", None),
		"creation": str(doc.creation) if hasattr(doc, "creation") else None,
		"modified": str(doc.modified) if hasattr(doc, "modified") else None,
	}


def _contact_payload(doc) -> dict:
	return {
		"name": doc.name,
		"first_name": getattr(doc, "first_name", None),
		"last_name": getattr(doc, "last_name", None),
		"email_id": getattr(doc, "email_id", None),
		"mobile_no": getattr(doc, "mobile_no", None),
		"phone": getattr(doc, "phone", None),
		"company_name": getattr(doc, "company_name", None),
		"creation": str(doc.creation) if hasattr(doc, "creation") else None,
		"modified": str(doc.modified) if hasattr(doc, "modified") else None,
	}


def _trigger_event(event_name: str, payload_data: dict, pipeline: str | None, reference_doctype: str, reference_name: str):
	if not frappe.db.exists("DocType", WEBHOOK_DOCTYPE):
		return

	field = EVENT_FIELD_MAP.get(event_name)
	if not field:
		return

	filters = {"status": "Active", field: 1}
	webhooks = frappe.get_all(WEBHOOK_DOCTYPE, filters=filters, fields=["name", "webhook_url"])

	for wh in webhooks:
		# Filtro de funil: se o webhook listar funis autorizados, só dispara para eles
		wh_pipelines = frappe.get_all("Cred Hub Webhook Pipeline", {"parent": wh.name, "parenttype": WEBHOOK_DOCTYPE}, pluck="pipeline")
		if wh_pipelines and pipeline and pipeline not in wh_pipelines:
			continue

		# Disparo em fila assíncrona (não atrasa a thread do usuário nem da API)
		frappe.enqueue(
			"cred_hub.webhooks.send_webhook",
			queue="short",
			webhook_name=wh.name,
			event_name=event_name,
			payload_data=payload_data,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			enqueue_after_commit=True,
		)


def send_webhook(webhook_name: str, event_name: str, payload_data: dict, reference_doctype: str = "", reference_name: str = ""):
	"""Executado pelo worker em segundo plano para envio HTTP com assinatura HMAC."""
	if not frappe.db.exists(WEBHOOK_DOCTYPE, webhook_name):
		return

	doc = frappe.get_doc(WEBHOOK_DOCTYPE, webhook_name)
	if doc.status != "Active":
		return

	delivery_id = f"wh_{secrets.token_hex(12)}"
	body = {
		"event": event_name,
		"delivery_id": delivery_id,
		"timestamp": now_datetime().isoformat(),
		"data": payload_data,
	}
	payload_json = frappe.as_json(body)

	secret = doc.get_password("secret") or ""
	signature = "sha256=" + hmac.new(secret.encode(), payload_json.encode(), hashlib.sha256).hexdigest()

	headers = {
		"Content-Type": "application/json",
		"User-Agent": "HubTech-Webhooks/1.0",
		"X-HubTech-Signature": signature,
		"X-HubTech-Event": event_name,
		"X-HubTech-Delivery": delivery_id,
	}

	start_time = time.time()
	status = "Failed"
	status_code = None
	response_text = ""

	try:
		resp = requests.post(doc.webhook_url, data=payload_json, headers=headers, timeout=10)
		status_code = resp.status_code
		response_text = resp.text[:2000]
		status = "Success" if resp.ok else "Failed"
	except Exception as exc:
		response_text = f"Connection error: {str(exc)[:1500]}"
		status_code = 0
		status = "Failed"
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
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="Cred Hub Webhook: falha ao gravar log de entrega")
