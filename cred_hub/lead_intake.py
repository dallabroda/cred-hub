"""Leads por webhook: Google Ads (formulário de lead), site da empresa, Typeform e qualquer outro sistema — credtech#51.

Mesmo caminho dos leads da Meta (meta_leads): cada envio vira lead com o funil da origem; o Cred Hub o transforma
em card na 1ª etapa, distribui pela regra do funil e trata repetido (mesmo e-mail/telefone com card aberto). Tudo
fica registrado em "Cred Hub Lead Event" para a tela de Origens de leads.

Entrada: POST /api/method/cred_hub.lead_intake.receive?source=<origem>, cada tipo com sua autenticação:
  - google_ads: o Google manda a chave no corpo (`google_key`, o campo "Chave" da extensão de formulário);
  - typeform: assinatura HMAC no cabeçalho Typeform-Signature (sha256 do corpo com a chave);
  - webhook: cabeçalho X-CredHub-Key;
  - website: a chave vai no corpo (`key`) e é pública (fica no HTML do site), então o envio só vale vindo do
    endereço cadastrado (Origin/Referer), com limite por visitante e o campo isca `website` vazio. O corpo vem como
    text/plain com JSON (assim o navegador não faz preflight de CORS em cada site cliente).
- Formato do site e do webhook: {fields: {first_name, last_name, email, mobile_no, organization, ...},
  answers: {pergunta: resposta}, utm_source, utm_medium, utm_campaign, utm_content, utm_term, gclid, fbclid,
  li_fat_id, landing_page, page, website (isca)}.
"""

import base64
import hashlib
import hmac
import json
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import cint, get_url, now_datetime

SOURCE = "Cred Hub Lead Source"
EVENT = "Cred Hub Lead Event"
TYPES = ("google_ads", "website", "typeform", "webhook")
TYPE_LABELS = {"google_ads": "Google Ads", "website": "Site", "typeform": "Typeform", "webhook": "Webhook"}
TRACKING = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "li_fat_id")
LEAD_FIELDS = ("first_name", "last_name", "email", "mobile_no", "phone", "organization", "job_title", "website")
WEBSITE_LIMIT = 10  # envios por visitante a cada 10 minutos
GOOGLE_COLUMNS = {
	"FULL_NAME": "full_name",
	"FIRST_NAME": "first_name",
	"LAST_NAME": "last_name",
	"EMAIL": "email",
	"WORK_EMAIL": "email",
	"PHONE_NUMBER": "mobile_no",
	"WORK_PHONE": "phone",
	"COMPANY_NAME": "organization",
	"JOB_TITLE": "job_title",
}


def _only_managers():
	if not {"System Manager", "Sales Manager"} & set(frappe.get_roles()):
		raise frappe.PermissionError


def _url(name):
	return get_url(f"/api/method/cred_hub.lead_intake.receive?source={name}")


# Tela de Origens de leads


@frappe.whitelist(methods=["GET"])
def get_webhook_sources():
	_only_managers()
	sources = frappe.get_all(
		SOURCE, fields=["name", "type", "label", "pipeline", "enabled", "key_hint", "allowed_origins"], order_by="creation asc"
	)
	for source in sources:
		last = frappe.get_all(
			EVENT, {"source": source.name, "status": "Created"}, pluck="received_on", order_by="received_on desc", limit=1
		)
		source.update({"url": _url(source.name), "enabled": bool(source.enabled), "last_lead_at": last[0] if last else None})
	return sources


def _new_key():
	return frappe.generate_hash(length=32)


@frappe.whitelist(methods=["POST"])
def create_webhook_source(type: str, label: str, pipeline=None, allowed_origins=None):
	_only_managers()
	if type not in TYPES:
		frappe.throw(_("Unknown lead source type."))
	key = _new_key()
	doc = frappe.get_doc(
		{
			"doctype": SOURCE,
			"type": type,
			"label": (label or TYPE_LABELS[type]).strip()[:140],
			"pipeline": pipeline or None,
			"enabled": 1,
			"key": key,
			"key_hint": key[-4:],
			"allowed_origins": allowed_origins,
		}
	).insert(ignore_permissions=True)
	# A chave aparece só agora: depois a tela mostra só os 4 últimos caracteres
	return {"name": doc.name, "url": _url(doc.name), "key": key}


@frappe.whitelist(methods=["POST"])
def update_webhook_source(name: str, label=None, pipeline=None, enabled=None, allowed_origins=None):
	_only_managers()
	doc = frappe.get_doc(SOURCE, name)
	if label is not None:
		doc.label = label.strip()[:140] or doc.label
	if pipeline is not None:
		doc.pipeline = pipeline or None
	if enabled is not None:
		doc.enabled = 1 if cint(enabled) else 0
	if allowed_origins is not None:
		doc.allowed_origins = allowed_origins
	doc.save(ignore_permissions=True)
	return {"name": doc.name}


@frappe.whitelist(methods=["POST"])
def regenerate_webhook_key(name: str):
	_only_managers()
	doc = frappe.get_doc(SOURCE, name)
	key = _new_key()
	doc.update({"key": key, "key_hint": key[-4:]})
	doc.save(ignore_permissions=True)
	return {"key": key}


@frappe.whitelist(methods=["POST"])
def delete_webhook_source(name: str):
	_only_managers()
	frappe.delete_doc(SOURCE, name, ignore_permissions=True)
	return {"ok": True}


# Entrada


def _origin_allowed(source):
	allowed = {
		f"{urlsplit(line.strip()).scheme}://{urlsplit(line.strip()).netloc}".lower()
		for line in (source.allowed_origins or "").splitlines()
		if line.strip()
	}
	request = frappe.request
	sent = request.headers.get("Origin") or request.headers.get("Referer") or ""
	parts = urlsplit(sent)
	return bool(allowed) and f"{parts.scheme}://{parts.netloc}".lower() in allowed


def _too_many(source):
	key = f"credhub_intake:{source.name}:{frappe.local.request_ip}"
	count = cint(frappe.cache.get_value(key)) + 1
	frappe.cache.set_value(key, count, expires_in_sec=10 * 60)
	return count > WEBSITE_LIMIT


def _authorized(source, raw, body):
	key = source.get_password("key", raise_exception=False) or ""
	if not key:
		return False
	if source.type == "google_ads":
		return hmac.compare_digest(key, str(body.get("google_key") or ""))
	if source.type == "typeform":
		expected = "sha256=" + base64.b64encode(hmac.new(key.encode(), raw, hashlib.sha256).digest()).decode()
		return hmac.compare_digest(expected, frappe.request.headers.get("Typeform-Signature") or "")
	if source.type == "website":
		return hmac.compare_digest(key, str(body.get("key") or "")) and _origin_allowed(source)
	return hmac.compare_digest(key, frappe.request.headers.get("X-CredHub-Key") or "")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive(source=None):
	name = source or frappe.form_dict.get("source")
	doc = frappe.get_doc(SOURCE, name) if name and frappe.db.exists(SOURCE, name) else None
	if not doc:
		raise frappe.DoesNotExistError
	raw = frappe.request.get_data() or b""
	try:
		body = json.loads(raw or b"{}")
	except ValueError:
		frappe.throw(_("Invalid JSON."))
	if not isinstance(body, dict) or not _authorized(doc, raw, body):
		raise frappe.AuthenticationError
	if doc.type == "website":
		# Isca: gente não preenche o campo escondido; robô preenche. Responde ok para o robô não insistir.
		if str(body.get("website") or "").strip() or _too_many(doc):
			return {"ok": True}
	if not doc.enabled or not doc.pipeline:
		return {"ok": False, "message": _("This lead source is not connected to a pipeline.")}
	if doc.type == "google_ads" and body.get("is_test"):
		# Botão "Enviar dados de teste" do Google Ads: confirma a ligação sem criar card
		_record(doc, "Created", str(body.get("lead_id") or "teste"), error=_("Google Ads test lead (no card created)."))
		frappe.db.commit()
		return {"ok": True}
	values, answers, external_id = _parse(doc.type, body)
	return ingest(doc, values, answers, external_id)


def _parse(kind, body):
	if kind == "google_ads":
		return _parse_google(body)
	if kind == "typeform":
		return _parse_typeform(body)
	fields = body.get("fields") or {}
	values = {k: str(v).strip() for k, v in fields.items() if k in LEAD_FIELDS and v not in (None, "")}
	values.update({k: str(body[k])[:140] for k in TRACKING if body.get(k)})
	page = body.get("landing_page") or body.get("page")
	if page:
		values["landing_page"] = str(page)[:1000]
	answers = {str(k): str(v) for k, v in (body.get("answers") or {}).items() if v not in (None, "")}
	return values, answers, str(body.get("id") or "") or None


def _parse_google(body):
	values, answers = {}, {}
	for column in body.get("user_column_data") or []:
		value = column.get("string_value")
		if value in (None, ""):
			continue
		field = GOOGLE_COLUMNS.get(column.get("column_id"))
		if field:
			values[field] = value
		else:
			answers[column.get("column_name") or column.get("column_id")] = value
	values.update(
		{
			"ad_platform": "Google Ads",
			"ad_campaign_id": str(body.get("campaign_id") or "") or None,
			"ad_adset_id": str(body.get("adgroup_id") or "") or None,
			"ad_id": str(body.get("creative_id") or "") or None,
			"ad_form_name": str(body.get("form_id") or "") or None,
			"gclid": body.get("gcl_id"),
			"utm_source": "google",
			"utm_medium": "cpc",
		}
	)
	return {k: v for k, v in values.items() if v}, answers, str(body.get("lead_id") or "") or None


def _parse_typeform(body):
	response = body.get("form_response") or {}
	titles = {f.get("id"): f.get("title") for f in (response.get("definition") or {}).get("fields") or []}
	values, answers = {}, {}
	for answer in response.get("answers") or []:
		kind = answer.get("type")
		value = answer.get(kind)
		if isinstance(value, dict):
			value = value.get("label") or value.get("labels") or json.dumps(value, ensure_ascii=False)
		if isinstance(value, list):
			value = ", ".join(map(str, value))
		field_id = (answer.get("field") or {}).get("id")
		title = titles.get(field_id) or (answer.get("field") or {}).get("ref") or field_id
		if kind == "email" and "email" not in values:
			values["email"] = value
		elif kind == "phone_number" and "mobile_no" not in values:
			values["mobile_no"] = value
		elif kind == "text" and "first_name" not in values and ("nome" in str(title).lower() or "name" in str(title).lower()):
			values["first_name"] = value
		else:
			answers[str(title)] = str(value)
	hidden = response.get("hidden") or {}
	values.update({k: str(hidden[k])[:140] for k in TRACKING if hidden.get(k)})
	values["ad_form_name"] = (response.get("definition") or {}).get("title")
	return {k: v for k, v in values.items() if v}, answers, response.get("token")


def _record(source, status, external_id, **values):
	frappe.get_doc(
		{
			"doctype": EVENT,
			"source": source.name,
			"status": status,
			"leadgen_id": external_id,
			"received_on": now_datetime(),
			"pipeline": source.pipeline,
			**values,
		}
	).insert(ignore_permissions=True)


def ingest(source, values, answers, external_id=None):
	"""Cria o lead com o funil da origem; o Cred Hub faz o card, distribui e trata repetido."""
	if external_id and frappe.db.exists(
		EVENT, {"source": source.name, "leadgen_id": external_id, "status": ("in", ("Created", "Duplicate"))}
	):
		return {"ok": True}
	savepoint = "credhub_lead_intake"
	frappe.db.savepoint(savepoint)
	try:
		values = dict(values)
		if not values.get("first_name"):
			full = values.pop("full_name", "") or answers.get("full_name") or ""
			first, _sep, last = full.strip().partition(" ")
			values["first_name"] = first or values.get("email") or _("Website lead")
			values.setdefault("last_name", last or None)
		values.pop("full_name", None)
		lead = frappe.get_doc(
			{
				"doctype": "CRM Lead",
				"source": _lead_source(source),
				"pipeline": source.pipeline,
				"lead_captured_at": now_datetime(),
				**values,
			}
		).insert(ignore_permissions=True)
		duplicate = lead.flags.duplicate_of
		card = duplicate or frappe.db.get_value("CRM Deal", {"lead": lead.name})
		if card and answers and not duplicate:
			_note_answers(card, answers)
		_record(
			source,
			"Duplicate" if duplicate else "Created",
			external_id,
			lead=lead.name,
			form_name=values.get("ad_form_name"),
			card_doctype="CRM Deal" if card else None,
			card_name=card,
			assigned_to=frappe.db.get_value("CRM Deal", card, "deal_owner") if card else None,
		)
		frappe.db.commit()
		return {"ok": True}
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"Lead intake {source.name}")
		_record(source, "Failed", external_id, error=str(e)[:500])
		frappe.db.commit()
		return {"ok": False}


def _lead_source(source):
	"""Origem do lead (CRM Lead Source) pelo tipo; cria a origem se ainda não existir."""
	label = TYPE_LABELS[source.type]
	if not frappe.db.exists("CRM Lead Source", label):
		frappe.get_doc({"doctype": "CRM Lead Source", "source_name": label}).insert(ignore_permissions=True)
	return label


def _note_answers(card, answers):
	from cred_hub.meta_leads import _note_answers as note

	note(card, answers)
