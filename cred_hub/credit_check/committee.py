"""Motor de Inteligência de Crédito:
1. Extração Inteligente (OCR/Parser) de Documentos da Pasta Digital
2. Detecção Automática de Grupo Econômico e Risco Cruzado (QSA / Sócios)
3. Rating Interno Configurável (AA–E) e Calculadora de Limite Sugerido (5 Cs do Crédito)
4. Mesa de Comitê de Crédito com Alçadas, Quórum, Condicionantes, Votos Auditáveis e Vigência
5. Lembrete Automático de Cobrança de Documentos via WhatsApp (SmartChat / wa.me)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.parse
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, getdate, now_datetime, today

from cred_hub.cnpj.mapping import format_cnpj, only_digits

ONBOARDING_STORE = "Cred Hub Onboarding Store"
SETTINGS_DOCTYPE = "Cred Hub Credit Settings"
COMMITTEE_DOCTYPE = "Cred Hub Credit Committee"
COMMITTEE_PREFIX = "committee::"
RATING_POLICY_FIELD = "credit_rating_policy_json"

DEFAULT_RATING_POLICY: dict[str, Any] = {
	"aa_min_score": 820,
	"aa_revenue_pct": 30.0,
	"aa_sacado_pct": 25.0,
	"a_min_score": 700,
	"a_revenue_pct": 22.0,
	"a_sacado_pct": 25.0,
	"b_min_score": 560,
	"b_revenue_pct": 15.0,
	"b_sacado_pct": 20.0,
	"c_min_score": 420,
	"c_revenue_pct": 8.0,
	"c_sacado_pct": 15.0,
	"d_min_score": 280,
	"d_revenue_pct": 3.0,
	"d_sacado_pct": 10.0,
	"max_global_limit": 2000000.0,
	"tier_1_max_limit": 100000.0,
	"tier_2_max_limit": 500000.0,
	"default_validity_days": 180,
	"hard_stop_ceis_cnep": True,
	"hard_stop_rj_falencia": True,
}

COVENANT_OPTIONS: list[dict[str, str]] = [
	{"key": "aval_socios", "label": "Aval solidário dos sócios administradores"},
	{"key": "duplicata_performada", "label": "Operar apenas duplicatas performadas (mercadoria entregue)"},
	{"key": "confirmacao_lastro", "label": "Confirmação obrigatória de lastro / NF-e acima de R$ 10 mil"},
	{"key": "trava_domicilio", "label": "Trava de domicílio bancário / conta vinculada (escrow)"},
	{"key": "garantia_real", "label": "Garantia real / alienação fiduciária complementar"},
	{"key": "notificacao_sacado", "label": "Notificação prévia obrigatória ao sacado"},
]


def _kv_get(field: str, doctype: str = ONBOARDING_STORE) -> str:
	rows = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s LIMIT 1",
		(doctype, field),
	)
	return str(rows[0][0] or "") if rows and rows[0] else ""


def _kv_set(field: str, value: str, doctype: str = ONBOARDING_STORE) -> None:
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


def get_rating_policy() -> dict[str, Any]:
	"""Retorna a metodologia/política do Rating Interno (AA–E) e Calculadora de Limite Sugerido (com fallback para o Padrão)."""
	raw = _kv_get(RATING_POLICY_FIELD, doctype=SETTINGS_DOCTYPE)
	saved: dict[str, Any] = {}
	if raw:
		try:
			parsed = json.loads(raw)
			if isinstance(parsed, dict):
				saved = parsed
		except Exception:
			saved = {}

	policy = dict(DEFAULT_RATING_POLICY)
	for k, default_val in DEFAULT_RATING_POLICY.items():
		if k in saved and saved[k] is not None:
			if isinstance(default_val, bool):
				policy[k] = bool(cint(saved[k])) if isinstance(saved[k], (int, str)) else bool(saved[k])
			elif isinstance(default_val, int):
				policy[k] = max(0, cint(saved[k]))
			else:
				policy[k] = max(0.0, flt(saved[k]))
	policy["is_default"] = not bool(saved)
	return policy


def save_rating_policy(policy_data: dict[str, Any] | str | None = None, reset_default: bool = False) -> dict[str, Any]:
	"""Salva (ou restaura para o padrão) os parâmetros do Rating Interno (AA–E) e Calculadora de Limite."""
	if reset_default:
		_kv_set(RATING_POLICY_FIELD, "", doctype=SETTINGS_DOCTYPE)
		return get_rating_policy()

	if isinstance(policy_data, str):
		policy_data = frappe.parse_json(policy_data)
	if not isinstance(policy_data, dict):
		frappe.throw(_("Invalid rating policy format."))

	cleaned: dict[str, Any] = {}
	for k, default_val in DEFAULT_RATING_POLICY.items():
		val = policy_data.get(k, default_val)
		if isinstance(default_val, bool):
			cleaned[k] = bool(cint(val)) if isinstance(val, (int, str)) else bool(val)
		elif isinstance(default_val, int):
			cleaned[k] = max(0, cint(val))
		else:
			cleaned[k] = max(0.0, flt(val))

	_kv_set(RATING_POLICY_FIELD, json.dumps(cleaned, ensure_ascii=False), doctype=SETTINGS_DOCTYPE)
	return get_rating_policy()


# --- 1. Leitura Inteligente (OCR/Parser) de Documentos da Pasta Digital ---------------------------------------


def _resolve_file_path(file_url: str) -> str | None:
	if not file_url:
		return None
	site_path = frappe.get_site_path()
	rel = file_url.lstrip("/")
	if rel.startswith("private/files/") or rel.startswith("public/files/"):
		candidate = os.path.join(site_path, rel)
		if os.path.isfile(candidate):
			return candidate
	elif rel.startswith("files/"):
		candidate = os.path.join(site_path, "public", rel)
		if os.path.isfile(candidate):
			return candidate
	return None


def _extract_raw_text_from_bytes(raw_bytes: bytes, ext: str) -> str:
	if not raw_bytes:
		return ""
	ext = (ext or "").lower()
	try:
		if ext in (".csv", ".txt"):
			return raw_bytes[: 512 * 1024].decode("utf-8", errors="ignore")
		if ext == ".pdf":
			for mod_name in ("pypdf", "PyPDF2"):
				try:
					pdf_mod = __import__(mod_name)
					reader = pdf_mod.PdfReader(io.BytesIO(raw_bytes))
					pages_text = []
					for p in reader.pages[:15]:
						pages_text.append(p.extract_text() or "")
					return "\n".join(pages_text)
				except Exception:
					continue
			ascii_chunks = re.findall(rb"[\x20-\x7E\xC0-\xFF]{4,}", raw_bytes[: 512 * 1024])
			return "\n".join(c.decode("latin-1", errors="ignore") for c in ascii_chunks[:2000])
		if ext == ".xlsx":
			try:
				import openpyxl

				wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
				lines = []
				for sheet in wb.worksheets[:3]:
					for row in sheet.iter_rows(max_row=200, values_only=True):
						vals = [str(cell) for cell in row if cell is not None]
						if vals:
							lines.append(" | ".join(vals))
				return "\n".join(lines)
			except Exception:
				return ""
	except Exception:
		return ""
	return ""


def _extract_raw_text(file_path: str) -> str:
	if not file_path or not os.path.isfile(file_path):
		return ""
	ext = os.path.splitext(file_path)[1].lower()
	try:
		with open(file_path, "rb") as f:
			return _extract_raw_text_from_bytes(f.read(), ext)
	except Exception:
		return ""


def _parse_brl_numbers(text: str) -> list[float]:
	if not text:
		return []
	pattern = re.compile(r"(?:R\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d{2})|\d{4,9},\d{2})")
	values: list[float] = []
	for match in pattern.findall(text):
		clean = match.replace(".", "").replace(",", ".")
		try:
			val = float(clean)
			if 100.0 <= val <= 5_000_000_000.0:
				values.append(val)
		except ValueError:
			pass
	return values


DOC_VALIDATION_SCHEMA: dict[str, Any] = {
	"type": "OBJECT",
	"properties": {
		"is_valid_type": {
			"type": "BOOLEAN",
			"description": "True se o documento anexado corresponde ao tipo solicitado pelo analista de crédito.",
		},
		"detected_type": {
			"type": "STRING",
			"description": "Tipo do documento identificado no texto (ex: Contrato Social, Extrato Bancário, Balanço Patrimonial, CND, Comprovante de Endereço, etc.).",
		},
		"cnpj_matches": {
			"type": "BOOLEAN",
			"description": "True se o CNPJ impresso no documento confere com o CNPJ esperado da empresa.",
		},
		"detected_cnpj": {
			"type": "STRING",
			"description": "CNPJ principal encontrado no documento com máscara ou vazio.",
		},
		"is_signed": {
			"type": "BOOLEAN",
			"description": "True se há evidência de assinatura física, digital (Gov.br/ICP) ou chancela de Junta Comercial / Contador com CRC.",
		},
		"is_expired": {
			"type": "BOOLEAN",
			"description": "True se a certidão/documento tiver data de validade vencida.",
		},
		"expiration_date": {
			"type": "STRING",
			"description": "Data de validade ou competência em formato YYYY-MM-DD se identificada, ou vazio.",
		},
		"rejection_risk": {
			"type": "BOOLEAN",
			"description": "True se há motivo claro para rejeição pelo analista de crédito.",
		},
		"rejection_reasons": {
			"type": "ARRAY",
			"items": {"type": "STRING"},
			"description": "Lista de motivos objetivos para rejeição, se houver.",
		},
		"extracted_metrics": {
			"type": "OBJECT",
			"properties": {
				"capital_social": {"type": "NUMBER", "description": "Capital social identificado em R$ se aplicável"},
				"faturamento_anual": {"type": "NUMBER", "description": "Faturamento anual ou receita bruta em R$ se aplicável"},
				"administrador": {"type": "STRING", "description": "Nome do sócio administrador identificado"},
				"regime_assinatura": {"type": "STRING", "description": "Isoladamente ou Em conjunto"},
			},
		},
		"summary": {
			"type": "STRING",
			"description": "Resumo executivo de 1 a 2 frases para o analista de crédito sobre a conformidade do documento.",
		},
	},
	"required": ["is_valid_type", "detected_type", "cnpj_matches", "rejection_risk", "summary"],
}


def validate_document_with_ai(
	organization_name: str,
	item_key: str,
	label: str,
	file_name: str,
	text: str,
) -> dict[str, Any] | None:
	"""Valida o documento com o Google Vertex AI (Gemini Flash-Lite). Retorna None se não configurado ou erro."""
	if not text or not text.strip():
		return None
	try:
		from cred_hub.credit_check.opinion import is_configured as is_vertex_configured, vertex_client

		if not is_vertex_configured():
			return None
	except Exception:
		return None

	org_doc = (
		frappe.db.get_value("CRM Organization", organization_name, ["organization_name", "cnpj"], as_dict=True)
		or {}
	)
	org_title = org_doc.get("organization_name") or organization_name
	org_cnpj = format_cnpj(only_digits(org_doc.get("cnpj") or ""))

	system = (
		"Você é um auditor de conformidade documental para análise de crédito de empresas (PJ). "
		"Sua função é avaliar se o documento anexado por um cliente PJ corresponde ao item solicitado, "
		"se pertence à empresa esperada e se há indícios de invalidade, vencimento ou inconsistência. "
		"Responda estritamente em conformidade com o esquema JSON solicitado."
	)
	text_sample = text[:8000]
	cnpj_instruction = org_cnpj if org_cnpj else "Não informado no cadastro (ignorar divergência de CNPJ se não houver no cadastro)"
	user_text = (
		f"Dados da Empresa no CRM:\n"
		f"- Razão Social: {org_title}\n"
		f"- CNPJ: {cnpj_instruction}\n\n"
		f"Item Solicitado na Pasta Digital:\n"
		f"- Chave: {item_key}\n"
		f"- Descrição: {label or item_key}\n"
		f"- Arquivo: {file_name}\n\n"
		f"Conteúdo do Documento (Amostra extraída):\n"
		f"---\n{text_sample}\n---"
	)

	try:
		client = vertex_client()
		client.timeout = 15  # Timeout curto (15s) para validação rápida de documento sem travar upload HTTP
		raw, result = client.generate_json(
			model="gemini-3.5-flash-lite",
			system=system,
			user_text=user_text,
			schema=DOC_VALIDATION_SCHEMA,
			max_output_tokens=2048,
		)
		rejection_reasons = [str(r).strip() for r in (raw.get("rejection_reasons") or []) if str(r).strip()]
		rejection_risk = bool(raw.get("rejection_risk"))

		# Proteção defensiva: se a IA detectou tipo inválido ou CNPJ divergente, garante rejection_risk = True
		if raw.get("is_valid_type") is False:
			rejection_risk = True
			type_reason = f"Documento anexado ({raw.get('detected_type') or 'não reconhecido'}) não confere com o solicitado ({label or item_key})"
			if type_reason not in rejection_reasons:
				rejection_reasons.append(type_reason)

		if org_cnpj and raw.get("cnpj_matches") is False and raw.get("detected_cnpj"):
			rejection_risk = True
			cnpj_reason = f"CNPJ detectado ({raw.get('detected_cnpj')}) diverge do cadastro ({org_cnpj})"
			if cnpj_reason not in rejection_reasons:
				rejection_reasons.append(cnpj_reason)

		if not rejection_risk and rejection_reasons:
			rejection_risk = True

		cnpj_valid = True if not org_cnpj else bool(raw.get("cnpj_matches"))
		suggested_action = (
			"reject"
			if rejection_risk
			else ("verify" if raw.get("is_valid_type") and cnpj_valid else "review")
		)
		suggested_reason = " · ".join(rejection_reasons) if rejection_risk else ""

		return {
			"ai_validated": True,
			"model": "gemini-3.5-flash-lite",
			"is_valid_type": bool(raw.get("is_valid_type")),
			"detected_type": str(raw.get("detected_type") or "").strip(),
			"cnpj_matches": bool(raw.get("cnpj_matches")),
			"detected_cnpj": str(raw.get("detected_cnpj") or "").strip(),
			"is_signed": bool(raw.get("is_signed")),
			"is_expired": bool(raw.get("is_expired")),
			"expiration_date": str(raw.get("expiration_date") or "").strip(),
			"rejection_risk": rejection_risk,
			"rejection_reasons": rejection_reasons,
			"suggested_action": suggested_action,
			"suggested_reason": suggested_reason,
			"extracted_metrics": raw.get("extracted_metrics") or {},
			"summary": str(raw.get("summary") or "").strip(),
			"cost_usd": result.cost_usd,
		}
	except Exception as e:
		frappe.log_error(title="Cred Hub: Falha na validação IA de documento", message=str(e))
		return None


def extract_document_insights(
	organization_name: str,
	item_key: str,
	file_url: str,
	file_name: str = "",
	label: str = "",
) -> dict[str, Any]:
	"""Extrai indicadores estruturados do arquivo anexado (Faturamento, Balanço, Contrato Social, Endividamento) e valida via IA."""
	from cred_hub.storage.s3 import read_bytes_by_url

	raw_doc_bytes = read_bytes_by_url(file_url)
	ext_hint = os.path.splitext(file_name or file_url or "")[1].lower()
	text = _extract_raw_text_from_bytes(raw_doc_bytes, ext_hint) if raw_doc_bytes else ""
	org_cnpj = only_digits(frappe.db.get_value("CRM Organization", organization_name, "cnpj") or "")

	cnpjs_found = list(
		dict.fromkeys(
			only_digits(m)
			for m in re.findall(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", text)
			if len(only_digits(m)) == 14
		)
	)
	cnpj_match = None
	if cnpjs_found and org_cnpj:
		cnpj_match = org_cnpj in cnpjs_found

	brl_values = _parse_brl_numbers(text)
	lower_text = text.lower()
	highlights: list[str] = []
	warnings: list[str] = []

	if cnpj_match is True:
		highlights.append(f"CNPJ {format_cnpj(org_cnpj)} confirmado no documento")
	elif cnpj_match is False and cnpjs_found:
		warnings.append(
			f"Atenção: CNPJ(s) lido(s) no arquivo ({', '.join(format_cnpj(c) for c in cnpjs_found[:2])}) diverge(m) do cadastro ({format_cnpj(org_cnpj)})"
		)

	estimated_monthly_revenue: float | None = None
	signing_rule = ""
	capital_social_detected: float | None = None

	if "contrato" in item_key or "societ" in label.lower():
		if "isoladamente" in lower_text or "individualmente" in lower_text:
			signing_rule = "Assinatura isolada permitida"
			highlights.append("Administração: assinatura isolada identificada")
		elif "em conjunto" in lower_text or "conjuntamente" in lower_text:
			signing_rule = "Exige assinatura em conjunto"
			highlights.append("Administração: exige assinatura em conjunto")
		if brl_values:
			capital_social_detected = max(brl_values)
			highlights.append(f"Maior valor societário/capital identificado: R$ {capital_social_detected:,.2f}")

	elif "faturamento" in item_key or "balanco" in item_key or "dre" in item_key or "faturamento" in label.lower():
		if brl_values:
			sorted_vals = sorted(brl_values)
			if len(sorted_vals) >= 4 and sorted_vals[-1] > sum(sorted_vals[:-1]) * 0.7:
				monthly_vals = sorted_vals[:-1]
			else:
				monthly_vals = sorted_vals[-12:]
			avg_val = sum(monthly_vals) / max(1, len(monthly_vals))
			estimated_monthly_revenue = round(avg_val, 2)
			highlights.append(
				f"Média mensal estimada ({len(monthly_vals)} ref.): R$ {estimated_monthly_revenue:,.2f}"
			)
		if "contador" not in lower_text and "crc" not in lower_text and text:
			warnings.append("Menção a contador/CRC não localizada no texto extraído — conferir assinatura")

	elif "endividamento" in item_key or "extrato" in item_key:
		if brl_values:
			max_debt = max(brl_values)
			highlights.append(f"Maior exposição/saldo identificado: R$ {max_debt:,.2f}")

	# Executa validação inteligente com Vertex AI (Gemini Flash-Lite)
	clean_label = label or item_key.replace("_", " ").title()
	ai_result = validate_document_with_ai(
		organization_name=organization_name,
		item_key=item_key,
		label=clean_label,
		file_name=file_name or os.path.basename(file_url),
		text=text,
	)

	if ai_result:
		if ai_result.get("rejection_risk"):
			warnings.append(f"IA: Risco de rejeição — {ai_result['suggested_reason']}")
		elif ai_result.get("summary"):
			highlights.append(f"IA: {ai_result['summary']}")
		suggested_action = ai_result["suggested_action"]
		suggested_reason = ai_result["suggested_reason"]
		rejection_risk = ai_result["rejection_risk"]
	else:
		# Fallback heurístico caso Vertex AI não esteja configurado
		if cnpj_match is False and cnpjs_found:
			suggested_action = "reject"
			suggested_reason = f"CNPJ lido no arquivo ({', '.join(format_cnpj(c) for c in cnpjs_found[:2])}) diverge do cadastro ({format_cnpj(org_cnpj)})"
			rejection_risk = True
		elif cnpj_match is True:
			suggested_action = "verify"
			suggested_reason = ""
			rejection_risk = False
		else:
			suggested_action = "review"
			suggested_reason = ""
			rejection_risk = False

		ai_result = {
			"ai_validated": False,
			"is_valid_type": True,
			"detected_type": clean_label,
			"cnpj_matches": cnpj_match,
			"detected_cnpj": format_cnpj(cnpjs_found[0]) if cnpjs_found else "",
			"rejection_risk": rejection_risk,
			"rejection_reasons": [suggested_reason] if suggested_reason else [],
			"suggested_action": suggested_action,
			"suggested_reason": suggested_reason,
			"summary": (
				"Validação heurística: "
				+ (
					"CNPJ conferido"
					if cnpj_match
					else ("CNPJ diverge do cadastro" if cnpj_match is False else "Aguardando conferência")
				)
			),
		}

	if not highlights and file_url:
		ext = os.path.splitext(file_name or file_url)[1].upper().lstrip(".") or "DOC"
		highlights.append(f"Arquivo {ext} catalogado e vinculado ao dossiê ({len(text)} caracteres lidos)")

	summary_parts = highlights + [f"⚠ {w}" for w in warnings]
	return {
		"analyzed_on": now_datetime().strftime("%Y-%m-%d %H:%M"),
		"text_extracted": bool(text.strip()),
		"chars_read": len(text),
		"cnpj_match": cnpj_match,
		"detected_cnpjs": [format_cnpj(c) for c in cnpjs_found[:3]],
		"estimated_monthly_revenue": estimated_monthly_revenue,
		"signing_rule": signing_rule,
		"capital_social_detected": capital_social_detected,
		"highlights": highlights,
		"warnings": warnings,
		"summary_text": " · ".join(summary_parts),
		"ai_validation": ai_result,
		"suggested_action": suggested_action,
		"suggested_reason": suggested_reason,
		"rejection_risk": rejection_risk,
	}


# --- 2. Detecção Automática de Grupo Econômico (Cruzamento de QSA / Sócios) -----------------------------------


def get_economic_group(organization_name: str) -> dict[str, Any]:
	"""Cruza os sócios (Contatos) da Organização com todas as outras Organizações e Cards da base HubTech."""
	if not organization_name or not frappe.db.exists("CRM Organization", organization_name):
		return {
			"has_group": False,
			"linked_count": 0,
			"related_count": 0,
			"shared_partners": [],
			"related_organizations": [],
			"total_group_limit": 0.0,
		}

	my_contacts = frappe.get_all(
		"Contact",
		filters={"company_name": organization_name},
		fields=["name", "full_name", "cpf", "designation", "share_percentage"],
	)
	cpf_to_partner: dict[str, str] = {}
	name_to_partner: dict[str, str] = {}
	for c in my_contacts:
		digits = only_digits(c.get("cpf") or "")
		full_name = str(c.get("full_name") or c.get("name") or "").strip()
		if len(digits) == 11:
			cpf_to_partner[digits] = full_name
		if len(full_name) >= 6:
			name_to_partner[full_name.upper()] = full_name

	related_orgs_map: dict[str, dict[str, Any]] = {}

	if cpf_to_partner or name_to_partner:
		other_contacts = frappe.get_all(
			"Contact",
			filters={"company_name": ["not in", ["", organization_name]]},
			fields=["name", "full_name", "cpf", "company_name", "designation", "share_percentage"],
			limit_page_length=2000,
		)
		for oc in other_contacts:
			other_org = oc.get("company_name")
			if not other_org or other_org == organization_name:
				continue
			oc_digits = only_digits(oc.get("cpf") or "")
			oc_name = str(oc.get("full_name") or "").strip().upper()
			matched_partner = None
			match_type = ""
			if oc_digits and oc_digits in cpf_to_partner:
				matched_partner = cpf_to_partner[oc_digits]
				match_type = "CPF"
			elif oc_name and oc_name in name_to_partner:
				matched_partner = name_to_partner[oc_name]
				match_type = "Nome"

			if matched_partner:
				entry = related_orgs_map.setdefault(
					other_org,
					{
						"organization": other_org,
						"shared_partners": [],
					},
				)
				label = f"{matched_partner} ({match_type})"
				if label not in entry["shared_partners"]:
					entry["shared_partners"].append(label)

	if frappe.db.table_exists("CRM Deal Organization"):
		linked_deals = frappe.get_all(
			"CRM Deal Organization",
			filters={"organization": organization_name, "parenttype": "CRM Deal"},
			pluck="parent",
		)
		if linked_deals:
			primary_orgs = frappe.get_all(
				"CRM Deal",
				filters={"name": ["in", linked_deals], "organization": ["not in", ["", organization_name]]},
				fields=["name", "organization"],
			)
			for row in primary_orgs:
				other_org = row.organization
				entry = related_orgs_map.setdefault(
					other_org,
					{"organization": other_org, "shared_partners": []},
				)
				lbl = f"Vínculo Comercial / Sacado no Card {row.name}"
				if lbl not in entry["shared_partners"]:
					entry["shared_partners"].append(lbl)

	related_list: list[dict[str, Any]] = []
	total_group_limit = 0.0

	for other_org, info in related_orgs_map.items():
		if not frappe.db.exists("CRM Organization", other_org):
			continue
		org_row = (
			frappe.db.get_value(
				"CRM Organization",
				other_org,
				["organization_name", "cnpj", "credit_verdict", "annual_revenue"],
				as_dict=True,
			)
			or {}
		)
		other_comm = _load_committee_raw(other_org)
		approved_lim = (
			flt(other_comm.get("approved_limit") or 0.0)
			if str(other_comm.get("status") or "").lower() in ("approved", "approved with reservations", "approved_with_conditions")
			else 0.0
		)
		total_group_limit += approved_lim

		deals = frappe.get_all(
			"CRM Deal",
			filters={"organization": other_org},
			fields=["name", "pipeline", "status"],
			limit_page_length=20,
		)
		related_list.append(
			{
				"organization": other_org,
				"organization_name": org_row.get("organization_name") or other_org,
				"cnpj": org_row.get("cnpj") or "",
				"credit_verdict": org_row.get("credit_verdict") or "Not Checked",
				"approved_limit": approved_lim,
				"deals_count": len(deals),
				"shared_partners": info["shared_partners"],
			}
		)

	own_comm = _load_committee_raw(organization_name)
	own_limit = (
		flt(own_comm.get("approved_limit") or 0.0)
		if str(own_comm.get("status") or "").lower() in ("approved", "approved with reservations", "approved_with_conditions")
		else 0.0
	)

	return {
		"has_group": bool(related_list),
		"linked_count": len(related_list),
		"related_count": len(related_list),
		"related_organizations": related_list,
		"own_approved_limit": own_limit,
		"related_approved_limit": round(total_group_limit, 2),
		"total_group_exposure": round(own_limit + total_group_limit, 2),
	}


# --- 3. Motor de Rating Interno (AA–E) e Calculadora de Limite Sugerido ---------------------------------------


def _add_factor(factors: list[dict[str, Any]], label: str, delta: int) -> None:
	factors.append(
		{
			"factor": label,
			"label": label,
			"impact": delta,
			"impact_str": f"+{delta}" if delta >= 0 else str(delta),
			"tone": "positive" if delta > 0 else ("negative" if delta < 0 else "neutral"),
		}
	)


def calculate_rating_and_limit(
	organization_name: str,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Calcula o Score Interno (0-1000), o Rating (AA a E) e o Limite Sugerido pela Política configurada."""
	policy = get_rating_policy()
	if not organization_name or not frappe.db.exists("CRM Organization", organization_name):
		return {
			"score": 0,
			"rating": "C",
			"rating_label": "Risco Moderado",
			"multiplier_applied": 0.08,
			"revenue_multiplier_percent": 8,
			"suggested_limit": 0.0,
			"suggested_max_concentration": 0.0,
			"suggested_sacado_limit": 0.0,
			"monthly_revenue_base": 0.0,
			"revenue_source": "Não informado",
			"factors": [],
			"policy": policy,
		}

	org = frappe.get_doc("CRM Organization", organization_name)
	score = 500  # Base neutra (0 a 1000)
	factors: list[dict[str, Any]] = []
	hard_stop_reason = ""

	_add_factor(factors, "Pontuação base inicial (Neutra)", 500)

	# Pilar 1: Capital & Maturidade Societária (Receita Federal)
	age_months = None
	if org.get("activity_start_date"):
		try:
			age_months = max(0, date_diff(today(), getdate(org.activity_start_date)) // 30)
		except Exception:
			age_months = None
	elif org.get("years_in_business"):
		age_months = cint(org.years_in_business) * 12

	if age_months is not None:
		if age_months >= 60:
			score += 60
			_add_factor(factors, f"Maturidade societária ({age_months // 12} anos de CNPJ)", 60)
		elif age_months >= 24:
			score += 30
			_add_factor(factors, f"Tempo de atividade ({age_months} meses)", 30)
		elif age_months < 12:
			score -= 60
			_add_factor(factors, f"Empresa recém-constituída ({age_months} meses)", -60)

	share_cap = flt(org.get("share_capital") or 0.0)
	if share_cap >= 500_000:
		score += 40
		_add_factor(factors, f"Capital social sólido (R$ {share_cap:,.0f})", 40)
	elif share_cap >= 100_000:
		score += 20
		_add_factor(factors, f"Capital social integralizado (R$ {share_cap:,.0f})", 20)

	# Pilar 2, 3 e 4: Caráter / Restritivos, Capacidade SCR Bacen & Bureau Score Externo
	verdict = org.get("credit_verdict") or "Not Checked"
	report_name = org.get("credit_report")
	if report_name and frappe.db.exists("Cred Hub Credit Report", report_name):
		rep = frappe.get_doc("Cred Hub Credit Report", report_name)
		raw_details: dict[str, Any] = {}
		if rep.get("raw_details_json"):
			try:
				raw_details = json.loads(rep.get("raw_details_json")) or {}
			except Exception:
				raw_details = {}
		checked_sources = raw_details.get("checked_sources") if isinstance(raw_details.get("checked_sources"), dict) else {}
		directd_prot = raw_details.get("directd_protestos") if isinstance(raw_details.get("directd_protestos"), dict) else {}
		directd_sc = raw_details.get("directd_score") if isinstance(raw_details.get("directd_score"), dict) else {}

		if rep.get("ceis_cnep_sanctioned"):
			score -= 400
			_add_factor(factors, "Sanção pública ativa no CEIS/CNEP", -400)
			if policy.get("hard_stop_ceis_cnep", True):
				hard_stop_reason = "Sanção CEIS/CNEP ativa (Trava Eliminatória)"
		elif rep.get("ceis_cnep_verified"):
			score += 30
			_add_factor(factors, "Nada consta em Sanções CEIS/CNEP", 30)

		if rep.get("has_bankruptcy_recovery"):
			score -= 350
			_add_factor(factors, "Recuperação Judicial / Falência detectada", -350)
			if policy.get("hard_stop_rj_falencia", True):
				hard_stop_reason = "Recuperação Judicial / Falência ativa (Trava Eliminatória)"

		protests = cint(rep.get("cenprot_protests_count") or directd_prot.get("protests_count") or 0)
		protests_amt = flt(rep.get("cenprot_total_amount") or directd_prot.get("total_value") or 0.0)
		if rep.get("cenprot_has_protests") or protests > 0:
			if protests_amt > 50_000 or protests >= 10:
				penalty = -160
			elif protests_amt > 5_000 or protests >= 3:
				penalty = -90
			else:
				penalty = -40
			score += penalty
			_add_factor(factors, f"{protests} protesto(s) em cartório (CENPROT)", penalty)
		elif rep.get("protests_checked") or checked_sources.get("protests"):
			score += 60
			_add_factor(factors, "Sem protestos em cartório (CENPROT)", 60)

		pgfn_debt = flt(rep.get("pgfn_total_debt") or 0.0)
		if pgfn_debt > 0:
			score -= 120
			_add_factor(factors, f"Dívida ativa na PGFN (R$ {pgfn_debt:,.0f})", -120)
		elif rep.get("pgfn_status") and rep.get("pgfn_status") != "Não verificado":
			score += 40
			_add_factor(factors, "Regularidade fiscal PGFN verificada", 40)

		# SCR Bacen (se disponível no relatório)
		scr_overdue = flt(rep.get("scr_overdue_amount") or 0.0)
		scr_loss = flt(rep.get("scr_loss_amount") or 0.0)
		scr_total = flt(rep.get("scr_total_exposure") or 0.0)
		if scr_loss > 0:
			score -= 220
			_add_factor(factors, f"Prejuízo registrado no SCR Bacen (R$ {scr_loss:,.0f})", -220)
		elif scr_overdue > 0:
			ratio = (scr_overdue / scr_total) if scr_total > 0 else 0.05
			pen = -180 if ratio > 0.10 else (-100 if ratio > 0.02 else -40)
			score += pen
			_add_factor(factors, f"Carteira vencida no SCR Bacen (R$ {scr_overdue:,.0f})", pen)
		elif scr_total > 0:
			score += 70
			_add_factor(factors, "SCR Bacen 100% adimplente (zero vencido)", 70)

		# Bureau Score Externo / Proprietário (extraído de raw_details_json ou campos diretos)
		ext_score = cint(
			rep.get("credit_score")
			or rep.get("quod_score")
			or rep.get("serasa_score")
			or directd_sc.get("score")
			or 0
		)
		if ext_score > 0:
			if ext_score >= 750:
				score += 90
				_add_factor(factors, f"Score externo elevado ({ext_score}/1000)", 90)
			elif ext_score >= 600:
				score += 45
				_add_factor(factors, f"Score externo positivo ({ext_score}/1000)", 45)
			elif ext_score >= 400:
				score -= 30
				_add_factor(factors, f"Score externo moderado/baixo ({ext_score}/1000)", -30)
			else:
				score -= 100
				_add_factor(factors, f"Score externo crítico ({ext_score}/1000)", -100)
	elif verdict == "Pre-Approved":
		score += 100
		_add_factor(factors, "Pré-filtro automático aprovado", 100)
	elif verdict == "Rejected":
		score -= 220
		_add_factor(factors, "Reprovado na triagem inicial", -220)

	# Pilar 5: Governança Operacional — Pasta Digital e Visita Técnica
	if onboarding_data:
		summary = onboarding_data.get("summary") or {}
		if summary.get("all_required_verified") and not summary.get("expired_count"):
			score += 50
			_add_factor(factors, "Pasta Digital 100% conferida e sem documento vencido", 50)
		elif summary.get("expired_count"):
			score -= 60
			_add_factor(factors, f"{summary.get('expired_count')} documento(s) vencido(s) na Pasta Digital", -60)

		visit = onboarding_data.get("visit") or {}
		if visit.get("status") == "completed":
			rec = visit.get("recommendation")
			if rec == "favoravel":
				score += 75
				_add_factor(factors, "Visita técnica realizada com parecer Favorável", 75)
			elif rec == "ressalvas":
				score -= 30
				_add_factor(factors, "Visita técnica com ressalvas", -30)
			elif rec == "desfavoravel":
				score -= 180
				_add_factor(factors, "Visita técnica com parecer Desfavorável", -180)

	score = max(0, min(1000, int(round(score))))

	# Classificação em Rating AA .. E conforme Política configurada
	if hard_stop_reason:
		rating = "E"
		rating_label = f"Risco Crítico · {hard_stop_reason}"
		rev_multiplier = 0.0
		sacado_pct = 0.0
	elif score >= cint(policy["aa_min_score"]):
		rating = "AA"
		rating_label = "Risco Mínimo · Prime"
		rev_multiplier = flt(policy["aa_revenue_pct"]) / 100.0
		sacado_pct = flt(policy["aa_sacado_pct"]) / 100.0
	elif score >= cint(policy["a_min_score"]):
		rating = "A"
		rating_label = "Risco Baixo · Sólido"
		rev_multiplier = flt(policy["a_revenue_pct"]) / 100.0
		sacado_pct = flt(policy["a_sacado_pct"]) / 100.0
	elif score >= cint(policy["b_min_score"]):
		rating = "B"
		rating_label = "Risco Moderado · Padrão"
		rev_multiplier = flt(policy["b_revenue_pct"]) / 100.0
		sacado_pct = flt(policy["b_sacado_pct"]) / 100.0
	elif score >= cint(policy["c_min_score"]):
		rating = "C"
		rating_label = "Risco Médio-Alto · Exige Garantias"
		rev_multiplier = flt(policy["c_revenue_pct"]) / 100.0
		sacado_pct = flt(policy["c_sacado_pct"]) / 100.0
	elif score >= cint(policy["d_min_score"]):
		rating = "D"
		rating_label = "Risco Elevado · Restrito"
		rev_multiplier = flt(policy["d_revenue_pct"]) / 100.0
		sacado_pct = flt(policy["d_sacado_pct"]) / 100.0
	else:
		rating = "E"
		rating_label = "Risco Crítico · Reprovação Recomendada"
		rev_multiplier = 0.0
		sacado_pct = 0.0

	# Base de Faturamento Mensal (1º: extraído da Pasta Digital; 2º: Receita Anual / 12; 3º: Capital Social)
	monthly_revenue = 0.0
	revenue_source = "Não informado"
	if onboarding_data:
		for item in onboarding_data.get("checklist") or []:
			ext = item.get("extracted_data") or {}
			if isinstance(ext, dict) and ext.get("estimated_monthly_revenue"):
				monthly_revenue = flt(ext["estimated_monthly_revenue"])
				revenue_source = f"Extraído de {item['label']}"
				break

	if not monthly_revenue and flt(org.get("annual_revenue")) > 0:
		monthly_revenue = round(flt(org.annual_revenue) / 12.0, 2)
		revenue_source = "Receita anual do cadastro (÷ 12)"

	if not monthly_revenue and flt(org.get("share_capital")) > 0:
		monthly_revenue = round(flt(org.share_capital) * 0.25, 2)
		revenue_source = "Estimativa conservadora (25% do Capital Social)"

	raw_suggested = monthly_revenue * rev_multiplier if monthly_revenue > 0 else 0.0
	max_cap = flt(policy.get("max_global_limit") or 0.0)
	if max_cap > 0 and raw_suggested > max_cap:
		raw_suggested = max_cap

	suggested_limit = round(raw_suggested / 1000.0) * 1000.0 if raw_suggested >= 1000 else round(raw_suggested, 2)
	suggested_sacado_limit = round(suggested_limit * sacado_pct, 2) if suggested_limit > 0 else 0.0

	return {
		"score": score,
		"rating": rating,
		"rating_label": rating_label,
		"multiplier_applied": rev_multiplier,
		"revenue_multiplier_percent": round(rev_multiplier * 100, 1),
		"sacado_concentration_percent": round(sacado_pct * 100, 1),
		"monthly_revenue_base": monthly_revenue,
		"revenue_source": revenue_source,
		"suggested_limit": suggested_limit,
		"suggested_max_concentration": suggested_sacado_limit,
		"suggested_sacado_limit": suggested_sacado_limit,
		"factors": factors,
		"policy": policy,
	}


# --- 4. Mesa de Comitê de Crédito (Proposta, Condicionantes, Alçadas e Votos) ---------------------------------


def _tier_for_limit(limit_brl: float) -> tuple[str, int]:
	"""Retorna (nome da alçada, quantidade de votos exigidos) conforme a política configurada."""
	policy = get_rating_policy()
	t1 = flt(policy.get("tier_1_max_limit") or 100_000.0)
	t2 = flt(policy.get("tier_2_max_limit") or 500_000.0)
	val = flt(limit_brl)
	if val <= t1:
		return "Alçada 1 · Mesa Sênior (1 voto)", 1
	if val <= t2:
		return "Alçada 2 · Comitê Gerencial (2 votos)", 2
	return "Alçada 3 · Comitê Diretoria / Pleno (2 votos)", 2


def _normalize_status_ui(status_val: str) -> str:
	s = str(status_val or "").strip().lower()
	if s in ("approved", "approve"):
		return "approved"
	if s in ("approved with reservations", "approved_with_conditions", "approve_with_conditions"):
		return "approved_with_conditions"
	if s in ("rejected", "reject"):
		return "rejected"
	if s in ("in voting", "in_voting"):
		return "in_voting"
	return "pending"


def _normalize_status_doc(status_val: str) -> str:
	ui = _normalize_status_ui(status_val)
	if ui == "approved":
		return "Approved"
	if ui == "approved_with_conditions":
		return "Approved with Reservations"
	if ui == "rejected":
		return "Rejected"
	if ui == "in_voting":
		return "In Voting"
	return "Draft"


def _load_committee_raw(organization_name: str) -> dict[str, Any]:
	raw = _kv_get(f"{COMMITTEE_PREFIX}{organization_name}")
	if raw:
		try:
			data = json.loads(raw)
			if isinstance(data, dict):
				return data
		except Exception:
			pass
	if frappe.db.table_exists(COMMITTEE_DOCTYPE):
		row = frappe.db.get_value(
			COMMITTEE_DOCTYPE,
			{"organization": organization_name},
			[
				"name",
				"deal",
				"status",
				"internal_rating",
				"internal_score",
				"requested_limit",
				"suggested_limit",
				"recommended_limit",
				"approved_limit",
				"max_sacado_concentration",
				"interest_rate_monthly",
				"max_term_days",
				"limit_valid_until",
				"required_tier",
				"required_votes",
				"covenants_json",
				"votes_json",
				"committee_notes",
				"decided_on",
				"decided_by",
			],
			as_dict=True,
		)
		if row:
			return {
				"status": row.status or "Draft",
				"deal": row.deal,
				"requested_limit": flt(row.requested_limit),
				"suggested_limit": flt(row.suggested_limit),
				"recommended_limit": flt(row.recommended_limit),
				"approved_limit": flt(row.approved_limit),
				"max_sacado_concentration": flt(row.max_sacado_concentration),
				"interest_rate_monthly": flt(row.interest_rate_monthly),
				"max_term_days": cint(row.max_term_days),
				"limit_valid_until": str(row.limit_valid_until or ""),
				"covenants": json.loads(row.covenants_json or "[]"),
				"votes": json.loads(row.votes_json or "[]"),
				"committee_notes": row.committee_notes or "",
				"decided_on": str(row.decided_on or ""),
				"decided_by": row.decided_by or "",
			}
	return {}


def _save_committee_raw(organization_name: str, state: dict[str, Any], deal_name: str | None = None) -> None:
	_kv_set(f"{COMMITTEE_PREFIX}{organization_name}", json.dumps(state, ensure_ascii=False))
	if not frappe.db.table_exists(COMMITTEE_DOCTYPE) or not frappe.db.exists("CRM Organization", organization_name):
		return
	try:
		values = {
			"organization": organization_name,
			"deal": deal_name or state.get("deal") or None,
			"status": _normalize_status_doc(state.get("status") or "Draft"),
			"internal_rating": state.get("internal_rating") or "",
			"internal_score": cint(state.get("internal_score") or 0),
			"requested_limit": flt(state.get("requested_limit") or 0),
			"suggested_limit": flt(state.get("suggested_limit") or 0),
			"recommended_limit": flt(state.get("recommended_limit") or 0),
			"approved_limit": flt(state.get("approved_limit") or 0),
			"max_sacado_concentration": flt(state.get("max_sacado_concentration") or 0),
			"interest_rate_monthly": flt(state.get("interest_rate_monthly") or 0),
			"max_term_days": cint(state.get("max_term_days") or 0),
			"limit_valid_until": state.get("limit_valid_until") or None,
			"required_tier": state.get("required_tier") or "",
			"required_votes": cint(state.get("required_votes") or 1),
			"covenants_json": json.dumps(state.get("covenants") or [], ensure_ascii=False),
			"votes_json": json.dumps(state.get("votes") or [], ensure_ascii=False),
			"committee_notes": str(state.get("committee_notes") or "")[:500],
			"decided_on": state.get("decided_on") or None,
			"decided_by": state.get("decided_by") or "",
		}
		existing = frappe.db.get_value(COMMITTEE_DOCTYPE, {"organization": organization_name}, "name")
		if existing:
			frappe.db.set_value(COMMITTEE_DOCTYPE, existing, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": COMMITTEE_DOCTYPE, **values}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Cred Hub Committee: falha ao sincronizar DocType relacional")


def get_committee_state(
	organization_name: str,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Retorna o estado completo da Mesa de Comitê + Sizing/Rating + Grupo Econômico."""
	sizing = calculate_rating_and_limit(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)
	group = get_economic_group(organization_name)
	raw = _load_committee_raw(organization_name)
	policy = sizing.get("policy") or get_rating_policy()

	deal_requested = 0.0
	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		deal_requested = flt(frappe.db.get_value("CRM Deal", deal_name, "annual_revenue") or 0.0)

	requested_limit = flt(raw.get("requested_limit") or deal_requested or sizing["suggested_limit"])
	recommended_limit = flt(raw.get("recommended_limit") or sizing["suggested_limit"])
	approved_limit = flt(raw.get("approved_limit") or 0.0)
	max_sacado = flt(raw.get("max_sacado_concentration") or sizing["suggested_sacado_limit"])
	tier_label, default_votes = _tier_for_limit(recommended_limit or requested_limit)
	req_votes = cint(raw.get("required_votes") or default_votes or 1)
	validity_days = cint(raw.get("validity_days") or policy.get("default_validity_days") or 180)

	limit_valid_until = str(raw.get("limit_valid_until") or "")
	limit_expired = False
	expiring_soon = False
	days_to_limit_expiry = None
	if limit_valid_until:
		try:
			days_to_limit_expiry = date_diff(getdate(limit_valid_until), getdate(today()))
			limit_expired = days_to_limit_expiry < 0
			expiring_soon = 0 <= days_to_limit_expiry <= 15
		except Exception:
			pass

	votes = raw.get("votes") if isinstance(raw.get("votes"), list) else []
	approve_count = sum(
		1
		for v in votes
		if isinstance(v, dict)
		and str(v.get("vote") or v.get("decision") or "").lower()
		in ("approve", "approved", "approve_with_conditions", "approve with reservations")
	)
	covenants = raw.get("covenants") if isinstance(raw.get("covenants"), list) else ["aval_socios", "confirmacao_lastro"]
	notes = str(raw.get("committee_notes") or "")

	return {
		"status": _normalize_status_ui(raw.get("status") or "Draft"),
		"status_doc": _normalize_status_doc(raw.get("status") or "Draft"),
		"requested_limit": requested_limit,
		"suggested_limit": sizing["suggested_limit"],
		"recommended_limit": recommended_limit,
		"proposed_limit": recommended_limit,
		"approved_limit": approved_limit,
		"max_sacado_concentration": max_sacado,
		"proposed_concentration": max_sacado,
		"interest_rate_monthly": flt(raw.get("interest_rate_monthly") or 2.85),
		"max_term_days": cint(raw.get("max_term_days") or 60),
		"validity_days": validity_days,
		"limit_valid_until": limit_valid_until or str(add_days(today(), validity_days)),
		"valid_until": limit_valid_until,
		"limit_expired": limit_expired,
		"is_expired": limit_expired,
		"expiring_soon": expiring_soon,
		"days_to_limit_expiry": days_to_limit_expiry,
		"required_tier": tier_label,
		"required_votes": req_votes,
		"required_quorum": req_votes,
		"approve_count": approve_count,
		"covenants": covenants,
		"covenant_options": COVENANT_OPTIONS,
		"committee_notes": notes,
		"conditions_notes": notes,
		"votes": votes,
		"decided_on": str(raw.get("decided_on") or ""),
		"decided_by": str(raw.get("decided_by") or ""),
		"rating": sizing,
		"sizing": sizing,
		"economic_group": group,
	}


def save_committee_proposal(
	organization_name: str,
	proposal_data: dict[str, Any] | str | None = None,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
	proposed_limit: float | None = None,
	proposed_concentration: float | None = None,
	required_quorum: int | None = None,
	validity_days: int | None = None,
	conditions_notes: str | None = None,
) -> dict[str, Any]:
	"""Salva/atualiza a Proposta de Limite e Condicionantes da Mesa de Comitê (suporta dict ou kwargs diretos)."""
	if isinstance(proposal_data, str):
		proposal_data = frappe.parse_json(proposal_data)
	if not isinstance(proposal_data, dict):
		proposal_data = {}

	if proposed_limit is not None:
		proposal_data["recommended_limit"] = proposed_limit
	if proposed_concentration is not None:
		proposal_data["max_sacado_concentration"] = proposed_concentration
	if required_quorum is not None:
		proposal_data["required_votes"] = required_quorum
	if validity_days is not None:
		proposal_data["validity_days"] = validity_days
		proposal_data["limit_valid_until"] = str(add_days(today(), max(1, cint(validity_days))))
	if conditions_notes is not None:
		proposal_data["committee_notes"] = conditions_notes

	sizing = calculate_rating_and_limit(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)
	raw = _load_committee_raw(organization_name)

	requested_limit = flt(proposal_data.get("requested_limit", raw.get("requested_limit") or sizing["suggested_limit"]))
	recommended_limit = flt(proposal_data.get("recommended_limit", raw.get("recommended_limit") or sizing["suggested_limit"]))
	max_sacado = flt(proposal_data.get("max_sacado_concentration", raw.get("max_sacado_concentration") or sizing["suggested_sacado_limit"]))
	rate = flt(proposal_data.get("interest_rate_monthly", raw.get("interest_rate_monthly") or 2.85))
	term = cint(proposal_data.get("max_term_days", raw.get("max_term_days") or 60))
	v_days = cint(proposal_data.get("validity_days", raw.get("validity_days") or 180))
	valid_until = str(proposal_data.get("limit_valid_until") or raw.get("limit_valid_until") or add_days(today(), v_days))
	covenants = proposal_data.get("covenants")
	if not isinstance(covenants, list):
		covenants = raw.get("covenants") or ["aval_socios"]
	notes = str(proposal_data.get("committee_notes") if proposal_data.get("committee_notes") is not None else raw.get("committee_notes") or "").strip()[:500]

	tier_label, default_votes = _tier_for_limit(recommended_limit or requested_limit)
	req_votes = max(1, cint(proposal_data.get("required_votes") or raw.get("required_votes") or default_votes))
	status = raw.get("status") or "Draft"
	if status == "Draft":
		status = "In Voting"

	updated = {
		**raw,
		"deal": deal_name or raw.get("deal"),
		"status": status,
		"internal_rating": sizing["rating"],
		"internal_score": sizing["score"],
		"requested_limit": requested_limit,
		"suggested_limit": sizing["suggested_limit"],
		"recommended_limit": recommended_limit,
		"max_sacado_concentration": max_sacado,
		"interest_rate_monthly": rate,
		"max_term_days": term,
		"validity_days": v_days,
		"limit_valid_until": valid_until,
		"required_tier": tier_label,
		"required_votes": req_votes,
		"covenants": covenants,
		"committee_notes": notes,
	}
	_save_committee_raw(organization_name, updated, deal_name=deal_name)
	return get_committee_state(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)


def cast_committee_vote(
	organization_name: str,
	decision: str | None = None,
	approved_limit: float | None = None,
	notes: str = "",
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
	vote: str | None = None,
	voted_limit: float | None = None,
) -> dict[str, Any]:
	"""Registra um voto auditável na Mesa de Comitê e, atingida a alçada, consolida a decisão e dispara o webhook."""
	raw_vote = str(vote or decision or "approve").strip().lower()
	if raw_vote in ("approve", "approved"):
		norm_vote, doc_decision = "approve", "Approve"
	elif raw_vote in ("approve_with_conditions", "approve with reservations", "approved with reservations"):
		norm_vote, doc_decision = "approve_with_conditions", "Approve with Reservations"
	elif raw_vote in ("reject", "rejected"):
		norm_vote, doc_decision = "reject", "Reject"
	else:
		frappe.throw(_("Invalid committee vote decision: {0}").format(raw_vote))

	eff_limit = voted_limit if voted_limit is not None else approved_limit
	state = get_committee_state(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)
	raw = _load_committee_raw(organization_name)
	votes: list[dict[str, Any]] = [v for v in (raw.get("votes") or []) if isinstance(v, dict)]

	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or user
	voted_on = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	limit_val = (
		0.0
		if norm_vote == "reject"
		else flt(eff_limit if eff_limit is not None else state["recommended_limit"] or state["suggested_limit"])
	)
	raw_sig = f"{organization_name}|{user}|{doc_decision}|{limit_val}|{voted_on}"
	vote_hash = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()[:16]

	votes = [v for v in votes if v.get("user") != user]
	votes.append(
		{
			"user": user,
			"full_name": full_name,
			"vote": norm_vote,
			"decision": doc_decision,
			"voted_limit": limit_val,
			"approved_limit": limit_val,
			"notes": (notes or "").strip()[:300],
			"voted_on": voted_on,
			"vote_hash": vote_hash,
		}
	)

	req_votes = cint(state.get("required_votes") or 1)
	reject_votes = [v for v in votes if v.get("vote") == "reject" or v.get("decision") == "Reject"]
	approve_votes = [
		v
		for v in votes
		if v.get("vote") in ("approve", "approve_with_conditions")
		or v.get("decision") in ("Approve", "Approve with Reservations")
	]

	new_status = "In Voting"
	final_limit = flt(raw.get("approved_limit") or 0.0)
	decided_on = raw.get("decided_on") or ""
	decided_by = raw.get("decided_by") or ""

	if reject_votes:
		new_status = "Rejected"
		final_limit = 0.0
		decided_on = voted_on
		decided_by = full_name
	elif len(approve_votes) >= req_votes:
		has_reservations = any(
			v.get("vote") == "approve_with_conditions" or v.get("decision") == "Approve with Reservations"
			for v in approve_votes
		)
		new_status = "Approved with Reservations" if has_reservations else "Approved"
		final_limit = min(flt(v.get("voted_limit") or v.get("approved_limit") or 0.0) for v in approve_votes)
		decided_on = voted_on
		decided_by = ", ".join(v["full_name"] for v in approve_votes)

	v_days = cint(raw.get("validity_days") or state.get("validity_days") or 180)
	valid_until = str(raw.get("limit_valid_until") or state["limit_valid_until"] or add_days(today(), v_days))
	updated = {
		**raw,
		"deal": deal_name or raw.get("deal"),
		"status": new_status,
		"internal_rating": state["sizing"]["rating"],
		"internal_score": state["sizing"]["score"],
		"requested_limit": state["requested_limit"],
		"suggested_limit": state["suggested_limit"],
		"recommended_limit": state["recommended_limit"],
		"approved_limit": final_limit,
		"max_sacado_concentration": state["max_sacado_concentration"],
		"interest_rate_monthly": state["interest_rate_monthly"],
		"max_term_days": state["max_term_days"],
		"validity_days": v_days,
		"limit_valid_until": valid_until,
		"required_tier": state["required_tier"],
		"required_votes": req_votes,
		"covenants": state["covenants"],
		"committee_notes": state["committee_notes"],
		"votes": votes,
		"decided_on": decided_on,
		"decided_by": decided_by,
	}
	_save_committee_raw(organization_name, updated, deal_name=deal_name)

	if new_status in ("Approved", "Approved with Reservations", "Rejected"):
		try:
			from cred_hub.webhooks import trigger_credit_limit_decided

			trigger_credit_limit_decided(
				organization_name=organization_name,
				deal_name=deal_name,
				committee_payload={
					"organization": organization_name,
					"deal": deal_name,
					"status": new_status,
					"internal_rating": state["sizing"]["rating"],
					"internal_score": state["sizing"]["score"],
					"approved_limit": final_limit,
					"max_sacado_concentration": state["max_sacado_concentration"],
					"interest_rate_monthly": state["interest_rate_monthly"],
					"max_term_days": state["max_term_days"],
					"limit_valid_until": valid_until,
					"covenants": state["covenants"],
					"votes": votes,
					"decided_on": decided_on,
				},
			)
		except Exception:
			pass

	return get_committee_state(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)


# --- 5. Ponte Nativa HubTech <-> SmartChat (Cobrar Pendências via WhatsApp) -----------------------------------


def build_whatsapp_onboarding_reminder(
	organization_name: str,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
	phone: str | None = None,
) -> dict[str, Any]:
	"""Monta a mensagem estruturada de cobrança da Pasta Digital + link wa.me / SmartChat."""
	if onboarding_data is None:
		from cred_hub.credit_check.onboarding import generate_portal_link

		onboarding_data = generate_portal_link(organization_name, deal_name=deal_name, regenerate=False)

	portal = onboarding_data.get("portal") or {}
	portal_url = portal.get("url") or ""
	checklist = onboarding_data.get("checklist") or []

	rejected = [i for i in checklist if i.get("status") == "rejected"]
	expired = [i for i in checklist if i.get("is_expired")]
	pending_req = [i for i in checklist if i.get("status") == "pending" and i.get("required")]

	lines = [f"Olá! Tudo bem? Referente à análise de crédito de *{organization_name}*:"]
	if rejected:
		lines.append("\n⚠️ *Documentos que precisam de substituição:*")
		for item in rejected:
			reason = f" ({item['notes']})" if item.get("notes") else ""
			lines.append(f"• {item['label']}{reason}")
	if expired:
		lines.append("\n📅 *Documentos com prazo de validade vencido:*")
		for item in expired:
			lines.append(f"• {item['label']} (venceu em {item.get('valid_until')})")
	if pending_req:
		lines.append("\n📄 *Documentos obrigatórios pendentes de envio:*")
		for item in pending_req:
			lines.append(f"• {item['label']}")

	if not (rejected or expired or pending_req):
		lines.append("\nEnvie ou atualize os documentos da sua Pasta Digital através do nosso portal seguro.")

	if portal_url:
		lines.append(f"\n🔒 *Link da Caixa de Correio Blindada (protegido por CNPJ):*\n{portal_url}")

	message_text = "\n".join(lines)

	resolved_phone = only_digits(phone or "")
	if not resolved_phone and deal_name and frappe.db.exists("CRM Deal", deal_name):
		resolved_phone = only_digits(frappe.db.get_value("CRM Deal", deal_name, "mobile_no") or "")
	if not resolved_phone:
		contact_phone = frappe.db.get_value(
			"Contact",
			{"company_name": organization_name, "mobile_no": ("is", "set")},
			"mobile_no",
		) or frappe.db.get_value(
			"Contact",
			{"company_name": organization_name, "phone": ("is", "set")},
			"phone",
		)
		resolved_phone = only_digits(contact_phone or "")

	if resolved_phone and len(resolved_phone) in (10, 11) and not resolved_phone.startswith("55"):
		resolved_phone = f"55{resolved_phone}"

	encoded_msg = urllib.parse.quote(message_text)
	wa_url = f"https://wa.me/{resolved_phone}?text={encoded_msg}" if resolved_phone else ""

	return {
		"phone": resolved_phone,
		"message": message_text,
		"whatsapp_url": wa_url or f"https://wa.me/?text={encoded_msg}",
		"whatsapp_web_url": wa_url,
		"pending_count": len(pending_req),
		"rejected_count": len(rejected),
		"expired_count": len(expired),
	}
