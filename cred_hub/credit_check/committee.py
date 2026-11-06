"""Mesa de Comitê de Crédito, Motor de Rating/Sizing de Limite, Grupo Econômico, IA Leitora da Pasta Digital e Ciclo de Renovação."""

from __future__ import annotations

import hashlib
import json
import os
import re
from html import escape
from typing import Any
import urllib.parse

import frappe
from frappe import _
from frappe.utils import add_days, cint, date_diff, flt, getdate, now_datetime, today

from cred_hub.cnpj.mapping import format_cnpj, only_digits

COMMITTEE_DOCTYPE = "Cred Hub Credit Committee"
ONBOARDING_STORE = "Cred Hub Onboarding Store"
COMMITTEE_PREFIX = "committee::"

COVENANT_OPTIONS = [
	{"key": "aval_socios", "label": "Aval solidário dos sócios administradores"},
	{"key": "confirmacao_lastro", "label": "Confirmação obrigatória de 100% dos títulos / NF-e"},
	{"key": "trava_domicilio", "label": "Trava de domicílio bancário / conta vinculada (escrow)"},
	{"key": "alienacao_fiduciaria", "label": "Garantia real / alienação fiduciária de bem"},
	{"key": "limite_escalonado", "label": "Liberação escalonada (50% nas primeiras 3 operações)"},
]

PERIODIC_DOCUMENT_KEYS = {"faturamento_12m", "balanco_dre", "endividamento_extratos", "curva_abc_sacados"}


def _kv_get(field: str) -> str:
	rows = frappe.db.sql(
		"SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s LIMIT 1",
		(ONBOARDING_STORE, field),
	)
	return str(rows[0][0] or "") if rows and rows[0] else ""


def _kv_set(field: str, value: str) -> None:
	exists = frappe.db.sql(
		"SELECT 1 FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s LIMIT 1",
		(ONBOARDING_STORE, field),
	)
	if exists:
		frappe.db.sql(
			"UPDATE `tabSingles` SET `value` = %s WHERE `doctype` = %s AND `field` = %s",
			(value, ONBOARDING_STORE, field),
		)
	else:
		frappe.db.sql(
			"INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, %s)",
			(ONBOARDING_STORE, field, value),
		)


# --- 1. IA Leitora da Pasta Digital (Extração de PDF / Texto) -------------------------------------------------


def _read_file_text(file_url: str) -> str:
	"""Lê texto de um arquivo anexado no Frappe (PDF, TXT, CSV ou extração de strings legíveis)."""
	if not file_url:
		return ""
	rel_path = file_url.lstrip("/")
	site_path = frappe.get_site_path(rel_path)
	if not os.path.exists(site_path):
		# Tenta localizar pelo registro File no banco
		file_doc_path = frappe.db.get_value("File", {"file_url": file_url}, "file_url")
		if file_doc_path:
			site_path = frappe.get_site_path(str(file_doc_path).lstrip("/"))
	if not os.path.exists(site_path) or not os.path.isfile(site_path):
		return ""

	ext = os.path.splitext(site_path)[1].lower()
	try:
		if ext in (".txt", ".csv", ".json", ".md", ".html", ".xml"):
			with open(site_path, "r", encoding="utf-8", errors="ignore") as f:
				return f.read(200_000)

		if ext == ".pdf":
			# 1. Tenta pypdf / PyPDF2 se instalados no ambiente
			for mod_name in ("pypdf", "PyPDF2"):
				try:
					mod = __import__(mod_name)
					reader = mod.PdfReader(site_path)
					pages_text = []
					for page in reader.pages[:25]:
						pages_text.append(page.extract_text() or "")
					joined = "\n".join(pages_text).strip()
					if joined:
						return joined[:200_000]
				except Exception:
					pass

			# 2. Fallback: extrai blocos de texto legíveis do binário PDF (streams descompactadas ou literais PDF)
			import zlib

			with open(site_path, "rb") as f:
				raw_bytes = f.read(5 * 1024 * 1024)
			chunks: list[str] = []
			for match in re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", raw_bytes, re.DOTALL):
				stream_data = match.group(1)
				try:
					decompressed = zlib.decompress(stream_data)
					chunks.append(decompressed.decode("latin-1", errors="ignore"))
				except Exception:
					chunks.append(stream_data.decode("latin-1", errors="ignore"))
			chunks.append(raw_bytes.decode("latin-1", errors="ignore"))
			raw_str = "\n".join(chunks)
			literals = re.findall(r"\(([^()]{2,200})\)", raw_str)
			ascii_runs = re.findall(r"[A-Za-zÀ-ÿ0-9R\$\.,/\-:; ]{4,120}", raw_str)
			return "\n".join(literals + ascii_runs)[:200_000]
	except Exception:
		return ""
	return ""


def _parse_brl_numbers(text: str) -> list[float]:
	"""Extrai valores monetários em formato brasileiro (ex: R$ 125.400,00 ou 125.400,00) do texto."""
	values: list[float] = []
	pattern = re.compile(r"(?:R\$\s*)?(\d{1,3}(?:\.\d{3})+(?:,\d{2})|\d{4,9},\d{2})")
	for match in pattern.finditer(text or ""):
		raw_num = match.group(1).replace(".", "").replace(",", ".")
		try:
			val = float(raw_num)
			if 100.0 <= val <= 10_000_000_000.0:
				values.append(val)
		except ValueError:
			pass
	return values


def extract_document_insights(
	organization_name: str,
	item_key: str,
	label: str,
	file_url: str,
	file_name: str = "",
) -> dict[str, Any]:
	"""Analisa o documento anexado na Pasta Digital, valida CNPJ/titularidade e extrai indicadores chave."""
	org_cnpj = only_digits(frappe.db.get_value("CRM Organization", organization_name, "cnpj") or "")
	text = _read_file_text(file_url)
	combined_source = f"{file_name or ''}\n{text}"

	# 1. Detecção de CNPJs no documento
	cnpjs_found: list[str] = []
	for match in re.finditer(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", combined_source):
		digits = only_digits(match.group(0))
		if len(digits) == 14 and digits != digits[0] * 14 and digits not in cnpjs_found:
			cnpjs_found.append(digits)

	cnpj_match: bool | None = None
	warnings: list[str] = []
	highlights: list[str] = []

	if cnpjs_found and org_cnpj:
		if org_cnpj in cnpjs_found:
			cnpj_match = True
			highlights.append(f"CNPJ {format_cnpj(org_cnpj)} confirmado no documento")
		else:
			cnpj_match = False
			warnings.append(
				f"CNPJ detectado no arquivo ({format_cnpj(cnpjs_found[0])}) diverge do CNPJ da organização ({format_cnpj(org_cnpj)})"
			)

	lower_text = combined_source.lower()
	brl_values = _parse_brl_numbers(combined_source)
	estimated_monthly_revenue: float | None = None
	signing_rule = ""
	capital_social_detected: float | None = None

	# 2. Regras específicas por tipo de documento
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
			# Filtra o maior valor (geralmente Total 12M) se houver mais de 3 parcelas
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

	if not highlights and file_url:
		ext = os.path.splitext(file_name or file_url)[1].upper().lstrip(".") or "DOC"
		highlights.append(f"Arquivo {ext} catalogado e vinculado ao dossiê ({len(text)} caracteres lidos)")

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
	}


# --- 2. Detecção Automática de Grupo Econômico (Cruzamento de QSA / Sócios) -----------------------------------


def get_economic_group(organization_name: str) -> dict[str, Any]:
	"""Cruza os sócios (Contatos) da Organização com todas as outras Organizações e Cards da base HubTech."""
	if not organization_name or not frappe.db.exists("CRM Organization", organization_name):
		return {"has_group": False, "shared_partners": [], "related_organizations": [], "total_group_limit": 0.0}

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

	# Também verifica se a organização participa como 2ª organização em Cards de outros clientes (Cedente/Sacado)
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
		approved_lim = flt(other_comm.get("approved_limit") or 0.0) if other_comm.get("status") in ("Approved", "Approved with Reservations") else 0.0
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
	own_limit = flt(own_comm.get("approved_limit") or 0.0) if own_comm.get("status") in ("Approved", "Approved with Reservations") else 0.0

	return {
		"has_group": bool(related_list),
		"related_count": len(related_list),
		"related_organizations": related_list,
		"own_approved_limit": own_limit,
		"related_approved_limit": round(total_group_limit, 2),
		"total_group_exposure": round(own_limit + total_group_limit, 2),
	}


# --- 3. Motor de Rating Interno (AA–E) e Calculadora de Limite Sugerido ---------------------------------------


def calculate_rating_and_limit(
	organization_name: str,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Calcula o Score Interno (0-1000), o Rating (AA a E) e o Limite Sugerido pela Política."""
	if not organization_name or not frappe.db.exists("CRM Organization", organization_name):
		return {
			"score": 0,
			"rating": "C",
			"rating_label": "Risco Moderado",
			"suggested_limit": 0.0,
			"suggested_sacado_limit": 0.0,
			"monthly_revenue_base": 0.0,
			"revenue_source": "Não informado",
			"factors": [],
		}

	org = frappe.get_doc("CRM Organization", organization_name)
	score = 500  # Base neutra (0 a 1000)
	factors: list[dict[str, Any]] = []

	# Fator 1: Tempo de Atividade
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
			score += 120
			factors.append({"label": f"Maturidade societária ({age_months // 12} anos)", "impact": "+120", "tone": "positive"})
		elif age_months >= 24:
			score += 70
			factors.append({"label": f"Tempo de atividade ({age_months} meses)", "impact": "+70", "impact_num": 70, "tone": "positive"})
		elif age_months < 12:
			score -= 80
			factors.append({"label": f"Empresa recém-constituída ({age_months} meses)", "impact": "-80", "tone": "negative"})

	# Fator 2: Triagem Jurídica & Fiscal (Cred Hub Credit Report)
	verdict = org.get("credit_verdict") or "Not Checked"
	report_name = org.get("credit_report")
	if report_name and frappe.db.exists("Cred Hub Credit Report", report_name):
		rep = frappe.get_doc("Cred Hub Credit Report", report_name)
		if rep.get("ceis_cnep_sanctioned"):
			score -= 400
			factors.append({"label": "Sanção pública ativa no CEIS/CNEP", "impact": "-400", "tone": "negative"})
		elif rep.get("ceis_cnep_verified"):
			score += 40
			factors.append({"label": "Nada consta em Sanções CEIS/CNEP", "impact": "+40", "tone": "positive"})

		if rep.get("has_bankruptcy_recovery"):
			score -= 350
			factors.append({"label": "Recuperação Judicial / Falência detectada", "impact": "-350", "tone": "negative"})

		protests = cint(rep.get("cenprot_protests_count") or 0)
		if rep.get("cenprot_has_protests") or protests > 0:
			penalty = min(220, 60 + protests * 20)
			score -= penalty
			factors.append({"label": f"{protests} protesto(s) em cartório (CENPROT)", "impact": f"-{penalty}", "tone": "negative"})
		else:
			score += 80
			factors.append({"label": "Sem protestos em cartório (CENPROT)", "impact": "+80", "tone": "positive"})

		pgfn_debt = flt(rep.get("pgfn_total_debt") or 0.0)
		if pgfn_debt > 0:
			score -= 120
			factors.append({"label": f"Dívida ativa na PGFN (R$ {pgfn_debt:,.2f})", "impact": "-120", "tone": "negative"})
		elif rep.get("pgfn_status"):
			score += 60
			factors.append({"label": "Regularidade fiscal PGFN verificada", "impact": "+60", "tone": "positive"})
	elif verdict == "Pre-Approved":
		score += 120
		factors.append({"label": "Pré-filtro aprovado", "impact": "+120", "tone": "positive"})
	elif verdict == "Rejected":
		score -= 250
		factors.append({"label": "Reprovado na triagem inicial", "impact": "-250", "tone": "negative"})

	# Fator 3: Pasta Digital e Relatório de Visita
	if onboarding_data:
		summary = onboarding_data.get("summary") or {}
		if summary.get("all_required_verified") and not summary.get("expired_count"):
			score += 80
			factors.append({"label": "Pasta Digital 100% conferida e em dia", "impact": "+80", "tone": "positive"})
		elif summary.get("expired_count"):
			score -= 60
			factors.append({"label": f"{summary.get('expired_count')} documento(s) vencido(s) na Pasta Digital", "impact": "-60", "tone": "negative"})

		visit = onboarding_data.get("visit") or {}
		if visit.get("status") == "completed":
			rec = visit.get("recommendation")
			if rec == "favoravel":
				score += 90
				factors.append({"label": "Visita técnica realizada com parecer Favorável", "impact": "+90", "tone": "positive"})
			elif rec == "ressalvas":
				score += 20
				factors.append({"label": "Visita técnica com ressalvas", "impact": "+20", "tone": "neutral"})
			elif rec == "desfavoravel":
				score -= 200
				factors.append({"label": "Visita técnica com parecer Desfavorável", "impact": "-200", "tone": "negative"})

	score = max(0, min(1000, int(round(score))))

	# Classificação em Rating AA .. E e Fator de Capacidade sobre Faturamento Mensal
	if score >= 820:
		rating, rating_label, rev_multiplier = "AA", "Risco Mínimo · Prime", 0.30
	elif score >= 700:
		rating, rating_label, rev_multiplier = "A", "Risco Baixo · Sólido", 0.22
	elif score >= 560:
		rating, rating_label, rev_multiplier = "B", "Risco Moderado · Padrão", 0.15
	elif score >= 420:
		rating, rating_label, rev_multiplier = "C", "Risco Médio-Alto · Exige Garantias", 0.08
	elif score >= 280:
		rating, rating_label, rev_multiplier = "D", "Risco Elevado · Restrito", 0.03
	else:
		rating, rating_label, rev_multiplier = "E", "Risco Crítico · Impeditivo", 0.0

	# Base de Faturamento Mensal (1º: extraído da Pasta Digital; 2º: Receita Anual / 12; 3º: Capital Social)
	monthly_revenue = 0.0
	revenue_source = "Não informado"
	if onboarding_data:
		for item in onboarding_data.get("checklist") or []:
			ext = item.get("extracted_data") or {}
			if ext.get("estimated_monthly_revenue"):
				monthly_revenue = flt(ext["estimated_monthly_revenue"])
				revenue_source = f"Extraído de {item['label']}"
				break

	if not monthly_revenue and flt(org.get("annual_revenue")) > 0:
		monthly_revenue = round(flt(org.annual_revenue) / 12.0, 2)
		revenue_source = "Receita anual do cadastro (÷ 12)"

	if not monthly_revenue and flt(org.get("share_capital")) > 0:
		monthly_revenue = round(flt(org.share_capital) * 0.25, 2)
		revenue_source = "Estimativa conservadora (25% do Capital Social)"

	suggested_limit = round((monthly_revenue * rev_multiplier) / 1000.0) * 1000.0 if monthly_revenue > 0 else 0.0
	if monthly_revenue > 0 and suggested_limit == 0.0 and rev_multiplier > 0:
		suggested_limit = round(monthly_revenue * rev_multiplier, 2)
	suggested_sacado_limit = round(suggested_limit * 0.25, 2) if suggested_limit > 0 else 0.0

	return {
		"score": score,
		"rating": rating,
		"rating_label": rating_label,
		"revenue_multiplier_percent": int(rev_multiplier * 100),
		"monthly_revenue_base": monthly_revenue,
		"revenue_source": revenue_source,
		"suggested_limit": suggested_limit,
		"suggested_sacado_limit": suggested_sacado_limit,
		"factors": factors,
	}


# --- 4. Mesa de Comitê de Crédito (Proposta, Condicionantes, Alçadas e Votos) ---------------------------------


def _tier_for_limit(limit_brl: float) -> tuple[str, int]:
	"""Retorna (nome da alçada, quantidade de votos exigidos) conforme o valor recomendado/solicitado."""
	val = flt(limit_brl)
	if val <= 100_000:
		return "Alçada 1 · Mesa Sênior (1 voto)", 1
	if val <= 500_000:
		return "Alçada 2 · Comitê Gerencial (2 votos)", 2
	return "Alçada 3 · Comitê Diretoria / Pleno (2 votos)", 2


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
			"status": state.get("status") or "Draft",
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

	deal_requested = 0.0
	if deal_name and frappe.db.exists("CRM Deal", deal_name):
		deal_requested = flt(frappe.db.get_value("CRM Deal", deal_name, "annual_revenue") or 0.0)

	requested_limit = flt(raw.get("requested_limit") or deal_requested or sizing["suggested_limit"])
	recommended_limit = flt(raw.get("recommended_limit") or sizing["suggested_limit"])
	approved_limit = flt(raw.get("approved_limit") or 0.0)
	max_sacado = flt(raw.get("max_sacado_concentration") or sizing["suggested_sacado_limit"])
	tier_label, req_votes = _tier_for_limit(recommended_limit or requested_limit)

	limit_valid_until = str(raw.get("limit_valid_until") or "")
	limit_expired = False
	days_to_limit_expiry = None
	if limit_valid_until:
		try:
			days_to_limit_expiry = date_diff(getdate(limit_valid_until), getdate(today()))
			limit_expired = days_to_limit_expiry < 0
		except Exception:
			pass

	votes = raw.get("votes") if isinstance(raw.get("votes"), list) else []
	covenants = raw.get("covenants") if isinstance(raw.get("covenants"), list) else ["aval_socios", "confirmacao_lastro"]

	return {
		"status": raw.get("status") or "Draft",
		"requested_limit": requested_limit,
		"suggested_limit": sizing["suggested_limit"],
		"recommended_limit": recommended_limit,
		"approved_limit": approved_limit,
		"max_sacado_concentration": max_sacado,
		"interest_rate_monthly": flt(raw.get("interest_rate_monthly") or 2.85),
		"max_term_days": cint(raw.get("max_term_days") or 60),
		"limit_valid_until": limit_valid_until or str(add_days(today(), 180)),
		"limit_expired": limit_expired,
		"days_to_limit_expiry": days_to_limit_expiry,
		"required_tier": tier_label,
		"required_votes": req_votes,
		"covenants": covenants,
		"covenant_options": COVENANT_OPTIONS,
		"committee_notes": str(raw.get("committee_notes") or ""),
		"votes": votes,
		"decided_on": str(raw.get("decided_on") or ""),
		"decided_by": str(raw.get("decided_by") or ""),
		"sizing": sizing,
		"economic_group": group,
	}


def save_committee_proposal(
	organization_name: str,
	proposal_data: dict[str, Any] | str,
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Salva/atualiza a Proposta de Limite e Condicionantes da Mesa de Comitê."""
	if isinstance(proposal_data, str):
		proposal_data = frappe.parse_json(proposal_data)
	if not isinstance(proposal_data, dict):
		frappe.throw(_("Invalid committee proposal data."))

	sizing = calculate_rating_and_limit(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)
	raw = _load_committee_raw(organization_name)

	requested_limit = flt(proposal_data.get("requested_limit", raw.get("requested_limit") or sizing["suggested_limit"]))
	recommended_limit = flt(proposal_data.get("recommended_limit", raw.get("recommended_limit") or sizing["suggested_limit"]))
	max_sacado = flt(proposal_data.get("max_sacado_concentration", raw.get("max_sacado_concentration") or sizing["suggested_sacado_limit"]))
	rate = flt(proposal_data.get("interest_rate_monthly", raw.get("interest_rate_monthly") or 2.85))
	term = cint(proposal_data.get("max_term_days", raw.get("max_term_days") or 60))
	valid_until = str(proposal_data.get("limit_valid_until") or raw.get("limit_valid_until") or add_days(today(), 180))
	covenants = proposal_data.get("covenants")
	if not isinstance(covenants, list):
		covenants = raw.get("covenants") or ["aval_socios"]
	notes = str(proposal_data.get("committee_notes") if proposal_data.get("committee_notes") is not None else raw.get("committee_notes") or "").strip()[:500]

	tier_label, req_votes = _tier_for_limit(recommended_limit or requested_limit)
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
	decision: str,
	approved_limit: float | None = None,
	notes: str = "",
	deal_name: str | None = None,
	onboarding_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Registra um voto auditável na Mesa de Comitê e, atingida a alçada, consolida a decisão e dispara o webhook."""
	if decision not in ("Approve", "Approve with Reservations", "Reject"):
		frappe.throw(_("Invalid committee vote decision."))

	state = get_committee_state(organization_name, deal_name=deal_name, onboarding_data=onboarding_data)
	raw = _load_committee_raw(organization_name)
	votes: list[dict[str, Any]] = [v for v in (raw.get("votes") or []) if isinstance(v, dict)]

	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or user
	voted_on = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	limit_val = (
		0.0
		if decision == "Reject"
		else flt(approved_limit if approved_limit is not None else state["recommended_limit"] or state["suggested_limit"])
	)
	raw_sig = f"{organization_name}|{user}|{decision}|{limit_val}|{voted_on}"
	vote_hash = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()[:16]

	# Substitui voto anterior do mesmo usuário se já tiver votado nesta rodada
	votes = [v for v in votes if v.get("user") != user]
	votes.append(
		{
			"user": user,
			"full_name": full_name,
			"decision": decision,
			"approved_limit": limit_val,
			"notes": (notes or "").strip()[:300],
			"voted_on": voted_on,
			"vote_hash": vote_hash,
		}
	)

	req_votes = cint(state.get("required_votes") or 1)
	reject_votes = [v for v in votes if v["decision"] == "Reject"]
	approve_votes = [v for v in votes if v["decision"] in ("Approve", "Approve with Reservations")]

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
		has_reservations = any(v["decision"] == "Approve with Reservations" for v in approve_votes)
		new_status = "Approved with Reservations" if has_reservations else "Approve"
		if new_status == "Approve":
			new_status = "Approved"
		# O limite aprovado pelo comitê é o menor entre os votos aprovadores (prudência de crédito)
		final_limit = min(flt(v["approved_limit"]) for v in approve_votes)
		decided_on = voted_on
		decided_by = ", ".join(v["full_name"] for v in approve_votes)

	valid_until = str(raw.get("limit_valid_until") or state["limit_valid_until"] or add_days(today(), 180))
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

	# Dispara webhook se a decisão final foi atingida
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
	onboarding_data: dict[str, Any],
	deal_name: str | None = None,
	phone: str | None = None,
) -> dict[str, Any]:
	"""Monta a mensagem estruturada de cobrança da Pasta Digital + link wa.me / SmartChat."""
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

	# Resolve telefone da Organização ou Contato se não informado
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
	wa_url = f"https://wa.me/{resolved_phone}?text={encoded_msg}" if resolved_phone else f"https://wa.me/?text={encoded_msg}"

	return {
		"phone": resolved_phone,
		"message": message_text,
		"whatsapp_url": wa_url,
		"pending_count": len(pending_req),
		"rejected_count": len(rejected),
		"expired_count": len(expired),
	}
