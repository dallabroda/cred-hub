"""Pasta Digital (Checklist de Documentos), Link Externo Blindado e Relatório de Visita para Crédito PJ / Securitizadoras / FIDCs."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from html import escape
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime, today

from cred_hub.cnpj.mapping import only_digits

SETTINGS = "Cred Hub Credit Settings"
ONBOARDING_STORE = "Cred Hub Onboarding Store"
CHECKLIST_DOCTYPE = "Cred Hub Checklist Item"
VISIT_DOCTYPE = "Cred Hub Visit Report"
TOKEN_DOCTYPE = "Cred Hub Portal Token"
TEMPLATE_FIELD = "credit_checklist_template_json"
ORG_FIELD_PREFIX = "onboarding::"
TOKEN_FIELD_PREFIX = "portal_token::"
PORTAL_EXPIRY_DAYS = 7
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {
	".pdf",
	".jpg",
	".jpeg",
	".png",
	".webp",
	".xlsx",
	".xls",
	".csv",
	".doc",
	".docx",
	".zip",
}

CHECKLIST_STATUSES = ("pending", "attached", "verified", "rejected")
VISIT_STATUSES = ("not_done", "scheduled", "completed")
VISIT_TYPES = ("presencial", "video", "terceirizada")
PROPERTY_TYPES = ("proprio", "alugado", "cedido")
OPERATIONAL_STATUSES = ("otimo", "regular", "precario", "incompativel")
INVENTORY_LEVELS = ("alto", "normal", "baixo", "servicos")
VISIT_RECOMMENDATIONS = ("favoravel", "ressalvas", "desfavoravel")

DEFAULT_CHECKLIST_ITEMS: list[dict[str, Any]] = [
	{
		"key": "contrato_social",
		"label": "Contrato Social / Última Alteração Consolidada",
		"category": "Societário",
		"required": True,
		"enabled": True,
	},
	{
		"key": "doc_socios",
		"label": "RG / CNH dos Sócios e Representantes",
		"category": "Societário",
		"required": True,
		"enabled": True,
	},
	{
		"key": "comprovante_endereco",
		"label": "Comprovante de Endereço (Empresa e Sócios)",
		"category": "Cadastral",
		"required": True,
		"enabled": True,
	},
	{
		"key": "faturamento_12m",
		"label": "Relação de Faturamento dos Últimos 12 Meses",
		"category": "Financeiro",
		"required": True,
		"enabled": True,
	},
	{
		"key": "balanco_dre",
		"label": "Balanço Patrimonial e DRE / Balancete Recente",
		"category": "Contábil",
		"required": False,
		"enabled": True,
	},
	{
		"key": "endividamento_extratos",
		"label": "Posição de Endividamento Bancário / Extratos",
		"category": "Financeiro",
		"required": False,
		"enabled": True,
	},
	{
		"key": "curva_abc_sacados",
		"label": "Relação de Clientes (Sacados) / Curva ABC",
		"category": "Operacional",
		"required": False,
		"enabled": True,
	},
]


def _slugify(text: str) -> str:
	slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
	return slug[:48] or "doc_custom"


def _kv_get(doctype: str, field: str) -> str:
	"""Lê direto de tabSingles sem passar pela validação de metadados do DocType no Frappe 16."""
	rows = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s LIMIT 1",
		(doctype, field),
	)
	return str(rows[0][0] or "") if rows and rows[0] else ""


def _kv_set(doctype: str, field: str, value: str) -> None:
	"""Grava direto em tabSingles sem alterar o timestamp `modified` de Cred Hub Credit Settings."""
	exists = frappe.db.sql(
		"SELECT 1 FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s LIMIT 1",
		(doctype, field),
	)
	if exists:
		frappe.db.sql(
			"UPDATE `tabSingles` SET `value` = %s WHERE `doctype` = %s AND `field` = %s",
			(value, doctype, field),
		)
	else:
		frappe.db.sql(
			"INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, %s)",
			(doctype, field, value),
		)


def _store_get(key: str) -> str:
	val = _kv_get(ONBOARDING_STORE, key)
	if val:
		return val
	# Fallback para registros gravados anteriormente sob Cred Hub Credit Settings
	legacy = _kv_get(SETTINGS, key)
	if legacy:
		_kv_set(ONBOARDING_STORE, key, legacy)
		frappe.db.sql(
			"DELETE FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
			(SETTINGS, key),
		)
	return legacy


def _store_set(key: str, value: str) -> None:
	_kv_set(ONBOARDING_STORE, key, value)
	frappe.db.sql(
		"DELETE FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s",
		(SETTINGS, key),
	)


def _relational_tables_ready() -> bool:
	try:
		return (
			bool(frappe.db.table_exists(CHECKLIST_DOCTYPE))
			and bool(frappe.db.table_exists(VISIT_DOCTYPE))
			and bool(frappe.db.table_exists(TOKEN_DOCTYPE))
		)
	except Exception:
		return False


def _sync_state_to_relational(organization_name: str, state: dict[str, Any], deal_name: str | None = None) -> None:
	"""Sincroniza o estado da Organização para os DocTypes relacionais (Cred Hub Checklist Item, Visit Report e Portal Token)."""
	if not organization_name or not _relational_tables_ready():
		return
	if not frappe.db.exists("CRM Organization", organization_name):
		return
	try:
		# 1. Checklist Items (template + custom_items)
		template_items = _effective_template_for_org(state)
		saved_items: dict[str, Any] = state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
		existing_rows = frappe.get_all(
			CHECKLIST_DOCTYPE,
			filters={"organization": organization_name},
			fields=["name", "item_key"],
		)
		by_key = {row.item_key: row.name for row in existing_rows}
		active_keys = set()

		for tmpl in template_items:
			key = tmpl["key"]
			active_keys.add(key)
			entry = saved_items.get(key) if isinstance(saved_items.get(key), dict) else {}
			status = entry.get("status") if entry.get("status") in CHECKLIST_STATUSES else "pending"
			file_url = str(entry.get("file_url") or "")
			file_name = str(entry.get("file_name") or "")
			if status == "pending" and file_url:
				status = "attached"

			extracted_raw = entry.get("extracted_data")
			extracted_json_str = (
				json.dumps(extracted_raw, ensure_ascii=False)
				if isinstance(extracted_raw, dict) and extracted_raw
				else str(entry.get("extracted_summary_json") or "")
			)
			valid_until_val = str(entry.get("valid_until") or "").strip() or None
			values = {
				"organization": organization_name,
				"deal": deal_name or (state.get("portal") or {}).get("deal") or None,
				"item_key": key,
				"label": tmpl["label"][:140],
				"category": (tmpl.get("category") or "Geral")[:40],
				"required": cint(bool(tmpl.get("required", True))),
				"is_custom": cint(bool(tmpl.get("is_custom", False))),
				"status": status,
				"file_url": file_url[:500],
				"file_name": file_name[:255],
				"valid_until": valid_until_val,
				"extracted_summary_json": extracted_json_str,
				"notes": str(entry.get("notes") or "")[:500],
				"updated_by_user": str(entry.get("updated_by") or ""),
				"updated_on_str": str(entry.get("updated_on") or ""),
			}
			if key in by_key:
				frappe.db.set_value(CHECKLIST_DOCTYPE, by_key[key], values, update_modified=False)
			else:
				frappe.get_doc({"doctype": CHECKLIST_DOCTYPE, **values}).insert(ignore_permissions=True)

		# Remove custom items that were deleted
		for key, docname in by_key.items():
			if key not in active_keys:
				frappe.delete_doc(CHECKLIST_DOCTYPE, docname, ignore_permissions=True)

		# 2. Visit Report
		visit = state.get("visit") if isinstance(state.get("visit"), dict) else None
		if visit and visit.get("status") in VISIT_STATUSES:
			visit_values = {
				"organization": organization_name,
				"deal": deal_name or None,
				"status": visit.get("status") or "not_done",
				"visit_type": visit.get("visit_type") or "presencial",
				"visit_date": str(visit.get("visit_date") or ""),
				"visitor_name": str(visit.get("visitor_name") or "")[:140],
				"property_type": visit.get("property_type") or "alugado",
				"operational_status": visit.get("operational_status") or "otimo",
				"inventory_level": visit.get("inventory_level") or "normal",
				"employees_observed": str(visit.get("employees_observed") or "")[:40],
				"recommendation": visit.get("recommendation") or "favoravel",
				"notes": str(visit.get("notes") or "")[:4000],
				"photos_json": json.dumps(visit.get("photos") or [], ensure_ascii=False),
				"updated_by_user": str(visit.get("updated_by") or ""),
				"updated_on_str": str(visit.get("updated_on") or ""),
			}
			existing_visit = frappe.db.get_value(VISIT_DOCTYPE, {"organization": organization_name}, "name")
			if existing_visit:
				frappe.db.set_value(VISIT_DOCTYPE, existing_visit, visit_values, update_modified=False)
			elif visit.get("status") != "not_done" or visit.get("notes"):
				frappe.get_doc({"doctype": VISIT_DOCTYPE, **visit_values}).insert(ignore_permissions=True)

		# 3. Portal Token
		portal = state.get("portal") if isinstance(state.get("portal"), dict) else {}
		token = str(portal.get("token") or "").strip()
		if token:
			token_values = {
				"token": token,
				"organization": organization_name,
				"deal": portal.get("deal") or deal_name or None,
				"created_on_str": str(portal.get("created_on") or ""),
				"created_by_user": str(portal.get("created_by") or ""),
				"expires_on": str(portal.get("expires_on") or ""),
				"revoked": cint(bool(portal.get("revoked", False))),
			}
			existing_tok = frappe.db.get_value(TOKEN_DOCTYPE, {"token": token}, "name")
			if existing_tok:
				frappe.db.set_value(TOKEN_DOCTYPE, existing_tok, token_values, update_modified=False)
			else:
				frappe.get_doc({"doctype": TOKEN_DOCTYPE, **token_values}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Cred Hub Onboarding: falha ao sincronizar DocTypes relacionais")


def migrate_all_to_relational() -> int:
	"""Migra todos os estados de onboarding de tabSingles para os DocTypes relacionais (idempotente)."""
	if not _relational_tables_ready():
		return 0
	rows = frappe.db.sql(
		"SELECT `doctype`, `field`, `value` FROM `tabSingles` "
		"WHERE `doctype` IN (%s, %s) AND `field` LIKE %s",
		(ONBOARDING_STORE, SETTINGS, f"{ORG_FIELD_PREFIX}%"),
	)
	migrated = 0
	for dt, field, raw in rows:
		org_name = str(field)[len(ORG_FIELD_PREFIX) :]
		if not org_name:
			continue
		if dt == SETTINGS:
			_kv_set(ONBOARDING_STORE, field, str(raw or ""))
			frappe.db.sql("DELETE FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s", (SETTINGS, field))
		try:
			state = json.loads(raw) if raw else {}
		except ValueError:
			state = {}
		if isinstance(state, dict) and state:
			_sync_state_to_relational(org_name, state)
			migrated += 1
	return migrated


def get_checklist_template() -> dict[str, Any]:
	"""Retorna o modelo de checklist de documentos configurado para a operação (ou o padrão de securitizadora/crédito PJ)."""
	raw = _kv_get(SETTINGS, TEMPLATE_FIELD) or ""
	try:
		saved = json.loads(raw) if raw else None
	except ValueError:
		saved = None

	if not isinstance(saved, list) or not saved:
		return {
			"items": [dict(item) for item in DEFAULT_CHECKLIST_ITEMS],
			"is_default": True,
		}

	cleaned: list[dict[str, Any]] = []
	seen_keys: set[str] = set()
	for raw_item in saved:
		if not isinstance(raw_item, dict):
			continue
		label = str(raw_item.get("label") or "").strip()
		if not label:
			continue
		key = _slugify(str(raw_item.get("key") or label))
		if key in seen_keys:
			continue
		seen_keys.add(key)
		cleaned.append(
			{
				"key": key,
				"label": label[:140],
				"category": str(raw_item.get("category") or "Geral").strip()[:40],
				"required": bool(raw_item.get("required", True)),
				"enabled": bool(raw_item.get("enabled", True)),
			}
		)

	if not cleaned:
		return {
			"items": [dict(item) for item in DEFAULT_CHECKLIST_ITEMS],
			"is_default": True,
		}

	return {"items": cleaned, "is_default": False}


def save_checklist_template(items: Any) -> dict[str, Any]:
	"""Salva o modelo de checklist de documentos exigidos nas Configurações de Crédito."""
	if isinstance(items, str):
		items = frappe.parse_json(items)
	if not isinstance(items, list):
		frappe.throw(_("Invalid document checklist format."))

	cleaned: list[dict[str, Any]] = []
	seen_keys: set[str] = set()
	for raw_item in items:
		if not isinstance(raw_item, dict):
			continue
		label = str(raw_item.get("label") or "").strip()
		if not label:
			continue
		key = _slugify(str(raw_item.get("key") or label))
		if key in seen_keys:
			key = f"{key}_{len(seen_keys) + 1}"
		seen_keys.add(key)
		cleaned.append(
			{
				"key": key,
				"label": label[:140],
				"category": str(raw_item.get("category") or "Geral").strip()[:40],
				"required": bool(raw_item.get("required", True)),
				"enabled": bool(raw_item.get("enabled", True)),
			}
		)

	if not any(item["enabled"] for item in cleaned):
		frappe.throw(_("Keep at least one active document in the checklist."))

	_kv_set(SETTINGS, TEMPLATE_FIELD, json.dumps(cleaned, ensure_ascii=False))
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	return get_checklist_template()


def _org_key(organization_name: str) -> str:
	return f"{ORG_FIELD_PREFIX}{organization_name}"


def _token_key(token: str) -> str:
	return f"{TOKEN_FIELD_PREFIX}{token}"


def _load_org_state(organization_name: str) -> dict[str, Any]:
	raw = _store_get(_org_key(organization_name)) or ""
	try:
		data = json.loads(raw) if raw else {}
	except ValueError:
		data = {}
	state = data if isinstance(data, dict) else {}

	# Se existem registros relacionais mais recentes (ou editados diretamente via DocType), hidrata o state
	if organization_name and _relational_tables_ready():
		try:
			rows = frappe.get_all(
				CHECKLIST_DOCTYPE,
				filters={"organization": organization_name},
				fields=[
					"item_key",
					"label",
					"category",
					"required",
					"is_custom",
					"status",
					"file_url",
					"file_name",
					"valid_until",
					"extracted_summary_json",
					"notes",
					"updated_by_user",
					"updated_on_str",
				],
			)
			if rows:
				checklist = dict(state.get("checklist") or {})
				custom_items = list(state.get("custom_items") or [])
				custom_keys = {c.get("key") for c in custom_items if isinstance(c, dict)}
				for r in rows:
					prev_entry = checklist.get(r.item_key) if isinstance(checklist.get(r.item_key), dict) else {}
					parsed_extracted = prev_entry.get("extracted_data") or None
					if r.get("extracted_summary_json"):
						try:
							parsed_extracted = json.loads(r.extracted_summary_json)
						except Exception:
							pass
					checklist[r.item_key] = {
						"status": r.status or "pending",
						"file_url": r.file_url or "",
						"file_name": r.file_name or "",
						"valid_until": str(r.valid_until or prev_entry.get("valid_until") or ""),
						"extracted_data": parsed_extracted,
						"notes": r.notes or "",
						"updated_by": r.updated_by_user or "",
						"updated_on": r.updated_on_str or "",
					}
					if cint(r.is_custom) and r.item_key not in custom_keys:
						custom_items.append(
							{
								"key": r.item_key,
								"label": r.label,
								"category": r.category or "Específico",
								"required": bool(cint(r.required)),
							}
						)
				state["checklist"] = checklist
				state["custom_items"] = custom_items
			elif state:
				# Primeira leitura de um registro legado: sincroniza para as tabelas relacionais
				_sync_state_to_relational(organization_name, state)
		except Exception:
			pass
	return state


def _save_org_state(organization_name: str, state: dict[str, Any], deal_name: str | None = None) -> None:
	_store_set(
		_org_key(organization_name),
		json.dumps(state, ensure_ascii=False, default=str),
	)
	_sync_state_to_relational(organization_name, state, deal_name=deal_name)


def _default_visit() -> dict[str, Any]:
	return {
		"status": "not_done",
		"visit_date": "",
		"visitor_name": "",
		"visit_type": "presencial",
		"property_type": "alugado",
		"operational_status": "otimo",
		"inventory_level": "normal",
		"employees_observed": "",
		"recommendation": "favoravel",
		"notes": "",
		"photos": [],
		"updated_by": "",
		"updated_on": "",
	}


def _effective_template_for_org(state: dict[str, Any]) -> list[dict[str, Any]]:
	"""Combina os documentos globais ativos com eventuais documentos específicos adicionados neste cliente/card."""
	base_items = [dict(item, is_custom=False) for item in get_checklist_template()["items"] if item.get("enabled", True)]
	seen = {item["key"] for item in base_items}
	custom_items = state.get("custom_items") if isinstance(state.get("custom_items"), list) else []
	for custom in custom_items:
		if not isinstance(custom, dict):
			continue
		key = str(custom.get("key") or "").strip()
		label = str(custom.get("label") or "").strip()
		if not key or not label or key in seen:
			continue
		seen.add(key)
		base_items.append(
			{
				"key": key,
				"label": label,
				"category": str(custom.get("category") or "Específico"),
				"required": bool(custom.get("required", True)),
				"enabled": True,
				"is_custom": True,
			}
		)
	return base_items


def _portal_info_from_state(state: dict[str, Any]) -> dict[str, Any]:
	portal = state.get("portal") if isinstance(state.get("portal"), dict) else {}
	token = str(portal.get("token") or "").strip()
	expires_on = str(portal.get("expires_on") or "").strip()
	revoked = bool(portal.get("revoked", False))
	if not token or revoked:
		return {"active": False, "token": "", "url": "", "expires_on": ""}
	if expires_on:
		try:
			if getdate(expires_on) < getdate(today()):
				return {"active": False, "token": "", "url": "", "expires_on": expires_on, "expired": True}
		except Exception:
			pass
	path = f"/pasta-credito?token={token}"
	try:
		full_url = frappe.utils.get_url(path)
	except Exception:
		full_url = path
	return {
		"active": True,
		"token": token,
		"path": path,
		"url": full_url,
		"expires_on": expires_on,
		"created_on": str(portal.get("created_on") or ""),
	}


def get_onboarding(organization_name: str, deal_name: str | None = None) -> dict[str, Any]:
	"""Retorna o estado completo do Checklist de Documentos, Link Externo e Relatório de Visita da Organização."""
	state = _load_org_state(organization_name)
	saved_items: dict[str, Any] = state.get("checklist") or {}
	template_items = _effective_template_for_org(state)

	checklist: list[dict[str, Any]] = []
	today_date = getdate(today())
	soon_cutoff = getdate(add_days(today(), 15))
	for tmpl in template_items:
		key = tmpl["key"]
		entry = saved_items.get(key) if isinstance(saved_items.get(key), dict) else {}
		status = entry.get("status") if entry.get("status") in CHECKLIST_STATUSES else "pending"
		file_url = str(entry.get("file_url") or "")
		file_name = str(entry.get("file_name") or "")
		if status == "pending" and file_url:
			status = "attached"
		valid_until = str(entry.get("valid_until") or "").strip()
		is_expired = False
		expiring_soon = False
		if valid_until:
			try:
				v_date = getdate(valid_until)
				if v_date < today_date:
					is_expired = True
				elif v_date <= soon_cutoff:
					expiring_soon = True
			except Exception:
				valid_until = ""
		extracted_data = entry.get("extracted_data") if isinstance(entry.get("extracted_data"), dict) else None
		checklist.append(
			{
				"key": key,
				"label": tmpl["label"],
				"category": tmpl.get("category") or "Geral",
				"required": bool(tmpl.get("required", True)),
				"is_custom": bool(tmpl.get("is_custom", False)),
				"status": status,
				"file_url": file_url,
				"file_name": file_name,
				"valid_until": valid_until,
				"is_expired": is_expired,
				"expiring_soon": expiring_soon,
				"extracted_data": extracted_data,
				"notes": str(entry.get("notes") or ""),
				"updated_by": str(entry.get("updated_by") or ""),
				"updated_on": str(entry.get("updated_on") or ""),
			}
		)

	total = len(checklist)
	required_items = [item for item in checklist if item["required"]]
	required_total = len(required_items)
	verified_count = sum(1 for item in checklist if item["status"] == "verified")
	required_verified = sum(1 for item in required_items if item["status"] == "verified" and not item["is_expired"])
	attached_count = sum(1 for item in checklist if item["status"] in ("attached", "verified"))
	rejected_count = sum(1 for item in checklist if item["status"] == "rejected")
	pending_count = sum(1 for item in checklist if item["status"] == "pending")
	expired_count = sum(1 for item in checklist if item["is_expired"])
	expiring_soon_count = sum(1 for item in checklist if item["expiring_soon"])
	progress_percent = int(round((verified_count / total) * 100)) if total else 100

	saved_visit = state.get("visit") if isinstance(state.get("visit"), dict) else {}
	visit = {**_default_visit(), **saved_visit}
	if visit["status"] not in VISIT_STATUSES:
		visit["status"] = "not_done"
	if not isinstance(visit.get("photos"), list):
		visit["photos"] = []

	committee_data = None
	try:
		from cred_hub.credit_check.committee import get_committee_state

		committee_data = get_committee_state(organization_name, deal_name)
	except Exception:
		pass

	return {
		"organization": organization_name,
		"deal": deal_name,
		"checklist": checklist,
		"summary": {
			"total": total,
			"required_total": required_total,
			"verified_count": verified_count,
			"required_verified_count": required_verified,
			"attached_count": attached_count,
			"rejected_count": rejected_count,
			"pending_count": pending_count,
			"expired_count": expired_count,
			"expiring_soon_count": expiring_soon_count,
			"all_required_verified": required_verified >= required_total,
			"progress_percent": progress_percent,
		},
		"portal": _portal_info_from_state(state),
		"visit": visit,
		"committee": committee_data,
	}


def add_custom_checklist_item(
	organization_name: str,
	label: str,
	category: str = "Específico",
	required: bool = True,
	deal_name: str | None = None,
) -> dict[str, Any]:
	"""Adiciona um documento específico avulso ao checklist desta empresa/card."""
	label = (label or "").strip()[:140]
	if not label:
		frappe.throw(_("Enter the name of the document."))
	state = _load_org_state(organization_name)
	custom_items = state.get("custom_items") if isinstance(state.get("custom_items"), list) else []
	existing_keys = {item["key"] for item in _effective_template_for_org(state)}
	base_key = f"custom_{_slugify(label)}"
	key = base_key
	counter = 2
	while key in existing_keys:
		key = f"{base_key}_{counter}"
		counter += 1

	custom_items.append(
		{
			"key": key,
			"label": label,
			"category": (category or "Específico").strip()[:40],
			"required": bool(required),
		}
	)
	state["custom_items"] = custom_items
	_save_org_state(organization_name, state, deal_name=deal_name)
	return get_onboarding(organization_name, deal_name)


def remove_custom_checklist_item(
	organization_name: str,
	item_key: str,
	deal_name: str | None = None,
) -> dict[str, Any]:
	"""Remove um documento específico avulso adicionado neste cliente/card."""
	state = _load_org_state(organization_name)
	custom_items = state.get("custom_items") if isinstance(state.get("custom_items"), list) else []
	state["custom_items"] = [item for item in custom_items if isinstance(item, dict) and item.get("key") != item_key]
	if isinstance(state.get("checklist"), dict):
		state["checklist"].pop(item_key, None)
	_save_org_state(organization_name, state, deal_name=deal_name)
	return get_onboarding(organization_name, deal_name)


def _add_timeline_comment(organization_name: str, deal_name: str | None, html_text: str) -> None:
	target_doctype = "CRM Deal" if (deal_name and frappe.db.exists("CRM Deal", deal_name)) else "CRM Organization"
	target_name = deal_name if target_doctype == "CRM Deal" else organization_name
	try:
		doc = frappe.get_doc(target_doctype, target_name)
		doc.add_comment("Comment", text=html_text)
	except Exception:
		pass


def _resolve_rejection_recipient_email(organization_name: str, deal_name: str | None = None) -> str:
	"""Resolve o e-mail do cliente/contato principal do Card ou da Organização para aviso de documento recusado."""
	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		try:
			deal_doc = frappe.get_doc("CRM Deal", deal_name)
			if getattr(deal_doc, "email", None):
				return str(deal_doc.email).strip()
			for row in getattr(deal_doc, "contacts", None) or []:
				if getattr(row, "email", None):
					return str(row.email).strip()
				if getattr(row, "contact", None):
					c_email = frappe.db.get_value("Contact", row.contact, "email_id")
					if c_email:
						return str(c_email).strip()
		except Exception:
			pass
	if organization_name and frappe.db.exists("CRM Organization", organization_name):
		org_email = frappe.db.get_value("CRM Organization", organization_name, "email")
		if org_email:
			return str(org_email).strip()
		contact_email = frappe.db.get_value(
			"Contact",
			{"company_name": organization_name, "email_id": ("is", "set")},
			"email_id",
		)
		if contact_email:
			return str(contact_email).strip()
	return ""


def _notify_document_rejected(
	organization_name: str,
	deal_name: str | None,
	item_key: str,
	label: str,
	reason: str,
) -> None:
	"""Garante link ativo da Pasta Digital, envia e-mail transacional ao cliente e dispara webhook checklist.item_rejected."""
	try:
		onboarding_data = generate_portal_link(organization_name, deal_name=deal_name, regenerate=False)
		portal_url = (onboarding_data.get("portal") or {}).get("url") or ""

		# 1. Webhook para SmartChat / integrações externas
		try:
			from cred_hub.webhooks import trigger_checklist_rejected

			trigger_checklist_rejected(
				organization_name=organization_name,
				deal_name=deal_name,
				item_key=item_key,
				label=label,
				reason=reason,
				portal_url=portal_url,
			)
		except Exception:
			pass

		# 2. E-mail transacional se houver destinatário
		recipient = _resolve_rejection_recipient_email(organization_name, deal_name)
		if recipient and portal_url:
			company_title = (
				frappe.db.get_value("CRM Organization", organization_name, "organization_name")
				or organization_name
			)
			reason_html = (
				f"<p style='margin:12px 0;padding:12px;background:#fef3f2;border-left:4px solid #b42318;color:#7a271a;'>"
				f"<strong>Motivo informado pela análise:</strong> {escape(reason)}</p>"
				if reason
				else ""
			)
			subject = f"Substituição de documento solicitada: {label} — {company_title}"
			message = (
				f"<div style='font-family:Inter,Arial,sans-serif;color:#111827;line-height:1.5;max-width:600px;'>"
				f"<h2 style='margin:0 0 8px;font-size:18px;'>Documento pendente de substituição</h2>"
				f"<p>Olá! A equipe de análise revisou a Pasta Digital de <strong>{escape(company_title)}</strong> e solicitou o reenvio do seguinte documento:</p>"
				f"<p style='font-size:16px;'>📄 <strong>{escape(label)}</strong></p>"
				f"{reason_html}"
				f"<p style='margin:20px 0;'>"
				f"<a href='{escape(portal_url)}' style='background:#111827;color:#ffffff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block;'>"
				f"Acessar Caixa de Correio Blindada e Reenviar</a></p>"
				f"<p style='font-size:12px;color:#6b7280;'>Link protegido por CNPJ (LGPD).</p>"
				f"</div>"
			)
			frappe.sendmail(
				recipients=[recipient],
				subject=subject,
				message=message,
				reference_doctype="CRM Deal" if deal_name else "CRM Organization",
				reference_name=deal_name or organization_name,
				now=False,
			)
	except Exception:
		pass


def update_checklist_item(
	organization_name: str,
	item_key: str,
	status: str | None = None,
	file_url: str | None = None,
	file_name: str | None = None,
	notes: str | None = None,
	deal_name: str | None = None,
	valid_until: str | None = None,
) -> dict[str, Any]:
	"""Atualiza um item do Checklist de Documentos (anexo, vencimento se aplicável, check de conferência, rejeição ou observação)."""
	item_key = (item_key or "").strip()
	if not item_key:
		frappe.throw(_("Select a checklist document."))

	state = _load_org_state(organization_name)
	template_map = {item["key"]: item for item in _effective_template_for_org(state)}
	if item_key not in template_map:
		frappe.throw(_("Unknown checklist document: {0}").format(item_key))

	checklist_state: dict[str, Any] = state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
	current = checklist_state.get(item_key) if isinstance(checklist_state.get(item_key), dict) else {}

	new_file_url = current.get("file_url", "") if file_url is None else str(file_url or "").strip()
	new_file_name = current.get("file_name", "") if file_name is None else str(file_name or "").strip()
	new_notes = current.get("notes", "") if notes is None else str(notes or "").strip()[:500]
	new_valid_until = (
		str(current.get("valid_until") or "").strip()
		if valid_until is None
		else str(valid_until or "").strip()
	)
	if new_valid_until:
		try:
			new_valid_until = str(getdate(new_valid_until))
		except Exception:
			frappe.throw(_("Invalid document expiration date: {0}").format(new_valid_until))

	if status is not None:
		if status not in CHECKLIST_STATUSES:
			frappe.throw(_("Invalid document status: {0}").format(status))
		new_status = status
	else:
		new_status = current.get("status") or "pending"
		if new_file_url and new_status in ("pending", "rejected"):
			new_status = "attached"

	extracted_data = current.get("extracted_data")
	if new_file_url and (new_file_url != current.get("file_url") or not extracted_data):
		try:
			from cred_hub.credit_check.committee import extract_document_insights

			extracted_data = extract_document_insights(
				organization_name=organization_name,
				item_key=item_key,
				file_url=new_file_url,
				file_name=new_file_name,
			)
		except Exception:
			pass

	timestamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	checklist_state[item_key] = {
		"status": new_status,
		"file_url": new_file_url,
		"file_name": new_file_name,
		"valid_until": new_valid_until,
		"extracted_data": extracted_data,
		"notes": new_notes,
		"updated_by": frappe.session.user,
		"updated_on": timestamp,
	}
	state["checklist"] = checklist_state
	_save_org_state(organization_name, state, deal_name=deal_name)

	label = template_map[item_key]["label"]
	status_labels = {
		"pending": _("Pending"),
		"attached": _("Attached"),
		"verified": _("Verified ✓"),
		"rejected": _("Rejected / Resubmit"),
	}
	status_label = status_labels.get(new_status, new_status)
	note_suffix = f" — <em>{escape(new_notes)}</em>" if new_notes else ""
	_add_timeline_comment(
		organization_name,
		deal_name,
		f"📋 <strong>{escape(_('Credit Document Checklist'))}</strong>: {escape(label)} → <strong>{escape(status_label)}</strong>{note_suffix}",
	)

	if new_status == "rejected":
		_notify_document_rejected(
			organization_name=organization_name,
			deal_name=deal_name,
			item_key=item_key,
			label=label,
			reason=new_notes,
		)

	return get_onboarding(organization_name, deal_name)


# --- Trava de Passagem de Etapa (Stage Gate) no Funil Crédito PJ ----------------------------------------------

ANALYSIS_STAGE_NAMES = {"análise", "analise", "em análise", "em analise"}
COMMITTEE_STAGE_NAMES = {
	"comitê",
	"comite",
	"crédito concedido",
	"credito concedido",
	"aprovado",
}


def validate_deal_stage_gate(doc, method=None) -> None:
	"""Hook CRM Deal.validate: aplica as travas de governança da Pasta Digital e Dossiê ao avançar etapa."""
	if getattr(frappe.flags, "in_install", False) or getattr(frappe.flags, "in_migrate", False):
		return
	if doc.is_new() or not doc.has_value_changed("status") or not doc.status:
		return

	organization_name = getattr(doc, "organization", None)
	if not organization_name or not frappe.db.exists("CRM Organization", organization_name):
		return

	enforce_checklist = cint(_kv_get(SETTINGS, "enforce_checklist_on_stage_gate"))
	require_verified = cint(_kv_get(SETTINGS, "require_verified_checklist_on_committee"))
	enforce_dossier = cint(_kv_get(SETTINGS, "enforce_dossier_on_committee"))
	if not (enforce_checklist or require_verified or enforce_dossier):
		return

	# Verifica se o card pertence ao Funil Crédito PJ (ou se o nome da etapa é de Análise / Comitê)
	credit_pipeline = _kv_get(SETTINGS, "credit_pipeline")
	if credit_pipeline and getattr(doc, "pipeline", None) and doc.pipeline != credit_pipeline:
		return

	stage_info = (
		frappe.db.get_value("CRM Deal Status", doc.status, ["deal_status", "type"], as_dict=True) or {}
	)
	stage_label = str(stage_info.get("deal_status") or doc.status or "").strip().lower()
	stage_type = str(stage_info.get("type") or "").strip()

	is_committee_or_won = stage_label in COMMITTEE_STAGE_NAMES or stage_type == "Won"
	is_analysis_or_later = is_committee_or_won or stage_label in ANALYSIS_STAGE_NAMES
	if not is_analysis_or_later:
		return

	onboarding = get_onboarding(organization_name, doc.name)
	required_items = [item for item in onboarding["checklist"] if item.get("required")]

	if enforce_checklist and is_analysis_or_later:
		missing = [
			item["label"]
			for item in required_items
			if item.get("status") not in ("attached", "verified")
		]
		if missing:
			frappe.throw(
				_(
					"Cannot move card to {0}: the following required documents in the Digital Folder are still pending or rejected: {1}"
				).format(
					frappe.bold(stage_info.get("deal_status") or doc.status),
					frappe.bold(", ".join(missing)),
				)
			)

	if require_verified and is_committee_or_won:
		unverified = [
			item["label"] for item in required_items if item.get("status") != "verified"
		]
		if unverified:
			frappe.throw(
				_(
					"Cannot move card to {0}: all required documents in the Digital Folder must be verified (checked) by the credit team first. Pending verification: {1}"
				).format(
					frappe.bold(stage_info.get("deal_status") or doc.status),
					frappe.bold(", ".join(unverified)),
				)
			)
		expired_docs = [
			f"{item['label']} ({item.get('valid_until')})"
			for item in required_items
			if item.get("is_expired")
		]
		if expired_docs:
			frappe.throw(
				_(
					"Cannot move card to {0}: the following required documents in the Digital Folder have expired and must be renewed: {1}"
				).format(
					frappe.bold(stage_info.get("deal_status") or doc.status),
					frappe.bold(", ".join(expired_docs)),
				)
			)

	if enforce_dossier and is_committee_or_won:
		has_report = bool(getattr(doc, "credit_report", None)) or bool(
			frappe.db.get_value("CRM Organization", organization_name, "credit_report")
		)
		if not has_report and frappe.db.table_exists("Cred Hub Credit Report"):
			has_report = bool(
				frappe.db.exists("Cred Hub Credit Report", {"organization": organization_name})
			)
		if not has_report:
			frappe.throw(
				_(
					"Cannot move card to {0}: generate the Credit Dossier / Risk Report for this company before advancing to Committee."
				).format(frappe.bold(stage_info.get("deal_status") or doc.status))
			)


# --- Link Externo Blindado ("Caixa de Correio" / Sem Download) ------------------------------------------------


def generate_portal_link(
	organization_name: str,
	deal_name: str | None = None,
	regenerate: bool = False,
) -> dict[str, Any]:
	"""Gera (ou retorna o ativo) Link Externo Blindado da Pasta Digital para envio de documentos pelo cliente/contador."""
	state = _load_org_state(organization_name)
	current_info = _portal_info_from_state(state)
	if current_info["active"] and not regenerate:
		return get_onboarding(organization_name, deal_name)

	# Invalida o token anterior, se existir
	old_token = str((state.get("portal") or {}).get("token") or "").strip()
	if old_token:
		_store_set(_token_key(old_token), "")
		if _relational_tables_ready():
			old_doc = frappe.db.get_value(TOKEN_DOCTYPE, {"token": old_token}, "name")
			if old_doc:
				frappe.db.set_value(TOKEN_DOCTYPE, old_doc, "revoked", 1, update_modified=False)

	new_token = secrets.token_urlsafe(24)
	expires_on = str(add_days(today(), PORTAL_EXPIRY_DAYS))
	state["portal"] = {
		"token": new_token,
		"deal": deal_name or "",
		"created_on": now_datetime().strftime("%Y-%m-%d %H:%M"),
		"created_by": frappe.session.user,
		"expires_on": expires_on,
		"revoked": False,
	}
	_save_org_state(organization_name, state, deal_name=deal_name)
	_store_set(_token_key(new_token), organization_name)
	return get_onboarding(organization_name, deal_name)


def revoke_portal_link(organization_name: str, deal_name: str | None = None) -> dict[str, Any]:
	"""Desativa imediatamente o Link Externo da Pasta Digital."""
	state = _load_org_state(organization_name)
	old_token = str((state.get("portal") or {}).get("token") or "").strip()
	if old_token:
		_store_set(_token_key(old_token), "")
		if _relational_tables_ready():
			old_doc = frappe.db.get_value(TOKEN_DOCTYPE, {"token": old_token}, "name")
			if old_doc:
				frappe.db.set_value(TOKEN_DOCTYPE, old_doc, "revoked", 1, update_modified=False)
	state["portal"] = {"token": "", "revoked": True}
	_save_org_state(organization_name, state, deal_name=deal_name)
	return get_onboarding(organization_name, deal_name)


def _resolve_portal_token(token: str) -> tuple[str, dict[str, Any]]:
	token = (token or "").strip()
	if not token or len(token) < 16:
		frappe.throw(_("Invalid or expired link."), frappe.PermissionError)
	org_name = _store_get(_token_key(token)) or ""
	if not org_name and _relational_tables_ready():
		tok_row = frappe.db.get_value(
			TOKEN_DOCTYPE,
			{"token": token, "revoked": 0},
			["organization", "expires_on"],
			as_dict=True,
		)
		if tok_row and tok_row.get("organization"):
			org_name = tok_row.organization
	if not org_name or not frappe.db.exists("CRM Organization", org_name):
		frappe.throw(_("Invalid or expired link."), frappe.PermissionError)
	state = _load_org_state(org_name)
	info = _portal_info_from_state(state)
	if not info["active"] or info["token"] != token:
		frappe.throw(_("This document upload link has expired or been revoked."), frappe.PermissionError)
	return org_name, state


def _verify_portal_cnpj(org_name: str, provided_cnpj: str) -> None:
	org_cnpj = only_digits(frappe.db.get_value("CRM Organization", org_name, "cnpj") or "")
	if not org_cnpj:
		return
	given_digits = only_digits(provided_cnpj or "")
	if not given_digits or given_digits != org_cnpj:
		frappe.throw(_("The informed CNPJ does not match the company of this folder."), frappe.PermissionError)


def public_get_portal_info(token: str) -> dict[str, Any]:
	"""Metadados mínimos antes da confirmação de CNPJ: nunca expõe lista nem arquivos."""
	org_name, state = _resolve_portal_token(token)
	org = frappe.db.get_value(
		"CRM Organization",
		org_name,
		["organization_name", "cnpj"],
		as_dict=True,
	) or {}
	cnpj_digits = only_digits(org.get("cnpj") or "")
	cnpj_hint = ""
	if len(cnpj_digits) == 14:
		cnpj_hint = f"**.***.***/{cnpj_digits[8:12]}-{cnpj_digits[12:]}"
	return {
		"valid": True,
		"company_name": org.get("organization_name") or org_name,
		"requires_cnpj": bool(cnpj_digits),
		"cnpj_hint": cnpj_hint,
		"expires_on": (state.get("portal") or {}).get("expires_on") or "",
	}


def public_verify_and_list(token: str, cnpj: str = "") -> dict[str, Any]:
	"""Lista de documentos em modo 'Caixa de Correio Blindada' (Write-Only): nunca retorna file_url nem notas internas."""
	org_name, state = _resolve_portal_token(token)
	_verify_portal_cnpj(org_name, cnpj)
	data = get_onboarding(org_name)
	org_title = frappe.db.get_value("CRM Organization", org_name, "organization_name") or org_name

	safe_items = []
	for item in data["checklist"]:
		safe_items.append(
			{
				"key": item["key"],
				"label": item["label"],
				"category": item["category"],
				"required": item["required"],
				"status": item["status"],
				"has_file": bool(item["file_url"]),
				"valid_until": item.get("valid_until") or "",
				"is_expired": bool(item.get("is_expired")),
				"expiring_soon": bool(item.get("expiring_soon")),
				"updated_on": item["updated_on"],
				# Motivo só é exibido ao cliente quando o documento foi rejeitado e precisa ser reenviado
				"rejection_reason": item["notes"] if item["status"] == "rejected" else "",
				"locked": item["status"] == "verified" and not item.get("is_expired"),
			}
		)

	return {
		"company_name": org_title,
		"expires_on": data["portal"].get("expires_on") or "",
		"summary": data["summary"],
		"checklist": safe_items,
	}


def public_upload_document(
	token: str,
	cnpj: str,
	item_key: str,
	file_name: str,
	file_content_base64: str,
	custom_label: str = "",
	valid_until: str = "",
) -> dict[str, Any]:
	"""Recebe um documento enviado pelo cliente/contador no Link Externo Blindado e grava como privado no HubTech."""
	org_name, state = _resolve_portal_token(token)
	_verify_portal_cnpj(org_name, cnpj)

	file_name = os.path.basename((file_name or "").strip())
	if not file_name:
		frappe.throw(_("Select a file to upload."))
	ext = os.path.splitext(file_name)[1].lower()
	if ext not in ALLOWED_EXTENSIONS:
		frappe.throw(_("File type not allowed ({0}). Send PDF, image or spreadsheet.").format(ext or "unknown"))

	if not file_content_base64:
		frappe.throw(_("Empty file content."))
	if "," in file_content_base64 and file_content_base64.strip().startswith("data:"):
		file_content_base64 = file_content_base64.split(",", 1)[1]
	try:
		raw_bytes = base64.b64decode(file_content_base64)
	except Exception:
		frappe.throw(_("Could not decode the uploaded file."))
	if not raw_bytes:
		frappe.throw(_("Empty file content."))
	if len(raw_bytes) > MAX_UPLOAD_BYTES:
		frappe.throw(_("File exceeds the maximum limit of 15 MB."))

	# Se o cliente estiver enviando um documento complementar extra
	item_key = (item_key or "").strip()
	if item_key == "__custom__":
		custom_label = (custom_label or "").strip()[:140]
		if not custom_label:
			frappe.throw(_("Enter the name of the complementary document."))
		custom_items = state.get("custom_items") if isinstance(state.get("custom_items"), list) else []
		existing_keys = {item["key"] for item in _effective_template_for_org(state)}
		base_key = f"extra_{_slugify(custom_label)}"
		new_key = base_key
		idx = 2
		while new_key in existing_keys:
			new_key = f"{base_key}_{idx}"
			idx += 1
		custom_items.append(
			{
				"key": new_key,
				"label": custom_label,
				"category": "Complementar",
				"required": False,
			}
		)
		state["custom_items"] = custom_items
		_save_org_state(org_name, state)
		item_key = new_key

	template_map = {item["key"]: item for item in _effective_template_for_org(state)}
	if item_key not in template_map:
		frappe.throw(_("Unknown checklist document: {0}").format(item_key))

	checklist_state = state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
	current = checklist_state.get(item_key) if isinstance(checklist_state.get(item_key), dict) else {}
	if current.get("status") == "verified":
		frappe.throw(_("This document has already been verified and locked by the credit team."))

	deal_name = str((state.get("portal") or {}).get("deal") or "").strip() or None
	target_doctype = "CRM Deal" if (deal_name and frappe.db.exists("CRM Deal", deal_name)) else "CRM Organization"
	target_name = deal_name if target_doctype == "CRM Deal" else org_name

	from frappe.utils.file_manager import save_file

	safe_filename = f"{item_key}_{file_name}"
	file_doc = save_file(
		safe_filename,
		raw_bytes,
		target_doctype,
		target_name,
		is_private=1,
	)

	timestamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	clean_valid_until = str(valid_until or current.get("valid_until") or "").strip()
	if clean_valid_until:
		try:
			clean_valid_until = str(getdate(clean_valid_until))
		except Exception:
			clean_valid_until = ""
	extracted_data = None
	try:
		from cred_hub.credit_check.committee import extract_document_insights

		extracted_data = extract_document_insights(
			organization_name=org_name,
			item_key=item_key,
			file_url=file_doc.file_url,
			file_name=file_name,
		)
	except Exception:
		pass
	checklist_state[item_key] = {
		"status": "attached",
		"file_url": file_doc.file_url,
		"file_name": file_name,
		"valid_until": clean_valid_until,
		"extracted_data": extracted_data,
		"notes": current.get("notes") or "",
		"updated_by": "Portal do Cliente",
		"updated_on": timestamp,
	}
	state["checklist"] = checklist_state
	_save_org_state(org_name, state, deal_name=deal_name)

	label = template_map[item_key]["label"]
	_add_timeline_comment(
		org_name,
		deal_name,
		f"📥 <strong>{escape(_('External Document Portal'))}</strong>: "
		f"{escape(_('Client uploaded'))} <strong>{escape(label)}</strong> ({escape(file_name)})",
	)

	return public_verify_and_list(token, cnpj)


def save_visit_report(
	organization_name: str,
	visit_data: dict[str, Any] | str,
	deal_name: str | None = None,
) -> dict[str, Any]:
	"""Salva o Relatório de Visita Técnica / Comercial (in loco ou vídeo) da empresa."""
	if isinstance(visit_data, str):
		visit_data = frappe.parse_json(visit_data)
	if not isinstance(visit_data, dict):
		frappe.throw(_("Invalid visit report data."))

	state = _load_org_state(organization_name)
	current_visit = state.get("visit") if isinstance(state.get("visit"), dict) else _default_visit()

	status = str(visit_data.get("status") or current_visit.get("status") or "completed").strip()
	if status not in VISIT_STATUSES:
		status = "completed"

	visit_type = str(visit_data.get("visit_type") or current_visit.get("visit_type") or "presencial").strip()
	if visit_type not in VISIT_TYPES:
		visit_type = "presencial"

	property_type = str(visit_data.get("property_type") or current_visit.get("property_type") or "alugado").strip()
	if property_type not in PROPERTY_TYPES:
		property_type = "alugado"

	operational_status = str(
		visit_data.get("operational_status") or current_visit.get("operational_status") or "otimo"
	).strip()
	if operational_status not in OPERATIONAL_STATUSES:
		operational_status = "otimo"

	inventory_level = str(
		visit_data.get("inventory_level") or current_visit.get("inventory_level") or "normal"
	).strip()
	if inventory_level not in INVENTORY_LEVELS:
		inventory_level = "normal"

	recommendation = str(
		visit_data.get("recommendation") or current_visit.get("recommendation") or "favoravel"
	).strip()
	if recommendation not in VISIT_RECOMMENDATIONS:
		recommendation = "favoravel"

	raw_photos = visit_data.get("photos")
	if raw_photos is None:
		raw_photos = current_visit.get("photos") or []
	photos: list[dict[str, str]] = []
	if isinstance(raw_photos, list):
		for photo in raw_photos[:20]:
			if isinstance(photo, dict) and photo.get("file_url"):
				photos.append(
					{
						"file_url": str(photo["file_url"]).strip(),
						"file_name": str(photo.get("file_name") or "Foto da visita").strip(),
						"uploaded_on": str(photo.get("uploaded_on") or today()),
					}
				)

	timestamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	visit_date = str(visit_data.get("visit_date") or current_visit.get("visit_date") or today()).strip()
	visitor_name = str(
		visit_data.get("visitor_name") or current_visit.get("visitor_name") or frappe.session.user
	).strip()[:140]
	raw_emp = visit_data.get("employees_observed")
	if raw_emp is None:
		raw_emp = current_visit.get("employees_observed") or ""
	employees_observed = str(raw_emp).strip()[:40]
	notes = str(visit_data.get("notes") if visit_data.get("notes") is not None else current_visit.get("notes") or "").strip()[:4000]

	updated_visit = {
		"status": status,
		"visit_date": visit_date,
		"visitor_name": visitor_name,
		"visit_type": visit_type,
		"property_type": property_type,
		"operational_status": operational_status,
		"inventory_level": inventory_level,
		"employees_observed": employees_observed,
		"recommendation": recommendation,
		"notes": notes,
		"photos": photos,
		"updated_by": frappe.session.user,
		"updated_on": timestamp,
	}
	state["visit"] = updated_visit
	_save_org_state(organization_name, state, deal_name=deal_name)

	rec_labels = {
		"favoravel": _("Favorable"),
		"ressalvas": _("Favorable with reservations"),
		"desfavoravel": _("Unfavorable"),
	}
	rec_label = rec_labels.get(recommendation, recommendation)
	note_snippet = f" — <em>{escape(notes[:200])}</em>" if notes else ""
	_add_timeline_comment(
		organization_name,
		deal_name,
		f"🏢 <strong>{escape(_('Technical / Commercial Visit Report'))}</strong> ({escape(visit_date)}): "
		f"<strong>{escape(rec_label)}</strong>{note_snippet}",
	)

	return get_onboarding(organization_name, deal_name)


def for_ai(organization_name: str) -> dict[str, Any]:
	"""Resumo estruturado da Pasta Digital de Documentos e do Relatório de Visita para o Parecer de Crédito da IA."""
	data = get_onboarding(organization_name)
	visit = data["visit"]
	return {
		"document_checklist": {
			"all_required_verified": data["summary"]["all_required_verified"],
			"verified_count": data["summary"]["verified_count"],
			"total_documents": data["summary"]["total"],
			"items": [
				{
					"document": item["label"],
					"category": item["category"],
					"required": item["required"],
					"status": item["status"],
					"notes": item["notes"],
				}
				for item in data["checklist"]
			],
		},
		"visit_report": (
			{
				"status": visit["status"],
				"visit_date": visit["visit_date"],
				"visitor_name": visit["visitor_name"],
				"visit_type": visit["visit_type"],
				"property_type": visit["property_type"],
				"operational_status": visit["operational_status"],
				"inventory_level": visit["inventory_level"],
				"employees_observed": visit["employees_observed"],
				"recommendation": visit["recommendation"],
				"notes": visit["notes"],
				"photos_count": len(visit.get("photos") or []),
			}
			if visit.get("status") != "not_done"
			else {"status": "not_done"}
		),
	}
