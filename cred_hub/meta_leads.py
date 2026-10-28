"""Leads da Meta (Facebook/Instagram Lead Ads) no funil em segundos — credtech#51 e #52.

Pedido do dono (26/10/2026): o admin da empresa conecta o Facebook com um botão, escolhe página, formulário e
funil; cada lead vira card na 1ª etapa em segundos, já atribuído (distribuição do funil), com campanha, conjunto e
anúncio para o ROI. Vale para todas as empresas, por conta própria.

Como funciona
- Conectar: o login da Meta só aceita endereços de retorno exatos, então todas as empresas voltam pelo mesmo
  endereço no site central (oauth_callback). O `state` vai assinado (site da empresa + nonce); o site central
  troca o código pelo token, lista as páginas e entrega os tokens ao site da empresa por variável de ambiente
  (nunca na linha de comando), como no cadastro e na cobrança. Também anota de qual empresa é cada página.
- Token de página tirado de token de usuário de longa duração não expira. Fica criptografado (campo Password) e é
  apagado ao desconectar (compromisso da política de privacidade).
- Webhook do app no site central (meta_webhook), conferido pela assinatura X-Hub-Signature-256; cada `leadgen` vai
  para a empresa dona da página, que busca o lead na Graph API (com campanha, conjunto e anúncio) e cria o lead
  com o funil; o Cred Hub o transforma em card, distribui e trata repetidos (crm, credtech#51).
- Rede de segurança: a cada 10 min cada empresa busca os leads recentes dos formulários ligados (com paginação)
  e cria os que o webhook não trouxe.
- Página compartilhada (agência, sócio de cliente): a rota página → empresa admite várias empresas; cada lead vai a
  todas e só cria card onde o formulário está ligado; desconectar numa empresa não tira a página do app.
- Custo: cada lead roda um `bench execute` no site da empresa (~2 s de CPU); uma rajada de muitos leads enfileira.
- Token inválido (senha trocada, permissão removida): a conexão mostra o erro e os admins são avisados.
Variáveis de ambiente: FB_APP_ID, FB_APP_SECRET (app "CredTech Digital"), CREDHUB_META_VERIFY_TOKEN e, se a
Meta exigir o Login for Business, META_LOGIN_CONFIG_ID.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, convert_utc_to_system_timezone, get_datetime, now_datetime

from cred_hub import welcome

GRAPH = "https://graph.facebook.com/v23.0"
DIALOG = "https://www.facebook.com/v23.0/dialog/oauth"
SCOPES = "pages_show_list,pages_read_engagement,pages_manage_metadata,leads_retrieval,ads_read,business_management"
LEAD_FIELDS = "id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,form_id,platform"
CONNECTION = "Cred Hub Meta Connection"
PAGE = "Cred Hub Meta Page"
EVENT = "Cred Hub Lead Event"
ROUTE = "Cred Hub Meta Route"
STATE_SECONDS = 15 * 60
MANAGERS = ("System Manager", "Sales Manager")
PLATFORMS = {"fb": "Facebook", "ig": "Instagram", "an": "Audience Network", "msg": "Messenger"}


class MetaError(Exception):
	pass


class MetaTokenError(MetaError):
	"""Token inválido ou revogado (código 190 da Graph API)."""


# Configuração


def _app():
	app_id, secret = os.environ.get("FB_APP_ID"), os.environ.get("FB_APP_SECRET")
	if not app_id or not secret:
		frappe.throw(_("The Facebook connection is not available right now."))
	return app_id, secret


def _hub_url(path):
	from cred_hub.signup import _domain

	base = frappe.conf.get("credhub_hub_url") or f"https://hub.{_domain()}"
	return base.rstrip("/") + path


def _redirect_uri():
	return _hub_url("/api/method/cred_hub.meta_leads.oauth_callback")


def _only_managers():
	if not set(MANAGERS) & set(frappe.get_roles()):
		raise frappe.PermissionError


def _is_central():
	return bool(frappe.conf.get("credhub_signup"))


# Graph API


def _graph(method, path, token=None, params=None, data=None, url=None):
	params = dict(params or {})
	if token:
		params["access_token"] = token
	response = requests.request(method, url or GRAPH + path, params=params, data=data, timeout=20)
	try:
		body = response.json()
	except ValueError:
		body = {}
	error = body.get("error") if isinstance(body, dict) else None
	if error or response.status_code >= 400:
		error = error or {}
		message = error.get("message") or f"HTTP {response.status_code}"
		if error.get("code") == 190:
			raise MetaTokenError(message)
		raise MetaError(message)
	return body


def _graph_all(path, token, params):
	"""Todas as páginas de uma lista da Graph API (paging.next)."""
	items, body = [], _graph("GET", path, token, params)
	for _page in range(1000):
		items.extend(body.get("data", []))
		next_url = (body.get("paging") or {}).get("next")
		if not next_url:
			break
		body = _graph("GET", "", url=next_url)
	return items


# Conectar (site da empresa → site central → site da empresa)


def _sign(payload):
	_app_id, secret = _app()
	raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
	signature = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
	return f"{raw}.{signature}"


def _unsign(state):
	_app_id, secret = _app()
	raw, _sep, signature = (state or "").partition(".")
	expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
	if not raw or not hmac.compare_digest(expected, signature):
		return None
	try:
		payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
	except ValueError:
		return None
	if time.time() - payload.get("ts", 0) > STATE_SECONDS:
		return None
	return payload


@frappe.whitelist(methods=["POST"])
def start_facebook_connect():
	"""Endereço do login da Meta (a tela abre num popup). O nonce prende a volta a quem pediu."""
	_only_managers()
	app_id, _secret = _app()
	nonce = frappe.generate_hash(length=24)
	frappe.cache.set_value(f"credhub_meta_nonce:{nonce}", frappe.session.user, expires_in_sec=STATE_SECONDS)
	params = {
		"client_id": app_id,
		"redirect_uri": _redirect_uri(),
		"state": _sign({"site": frappe.local.site, "nonce": nonce, "ts": int(time.time())}),
		"response_type": "code",
	}
	config_id = os.environ.get("META_LOGIN_CONFIG_ID")
	if config_id:
		params["config_id"] = config_id
	else:
		params["scope"] = SCOPES
	return {"auth_url": f"{DIALOG}?{urlencode(params)}"}


def _finish_page(title, message, site=None, success=True):
	"""Última página do popup: fecha sozinha; sem popup, volta para o Cred Hub da empresa."""
	back = f"https://{site}/crm" if site else "/"
	script = (
		"<script>setTimeout(function(){if(window.opener){window.close()}else{"
		f"window.location.href={json.dumps(back)}"
		"}},1500)</script>"
	)
	frappe.respond_as_web_page(
		title, frappe.utils.escape_html(message) + script, success=success, http_status_code=200 if success else 400
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def oauth_callback(code=None, state=None, error=None, error_description=None, **kwargs):
	"""Site central: troca o código, lista as páginas e entrega tudo ao site da empresa."""
	from cred_hub.signup import _bench, _site_exists

	if not _is_central():
		raise frappe.PermissionError
	data = _unsign(state)
	site = (data or {}).get("site")
	if not data or not site or not _site_exists(site):
		return _finish_page(_("Facebook"), _("This link expired. Start the connection again."), success=False)
	if error or not code:
		return _finish_page(_("Facebook"), _("The connection was cancelled."), site, success=False)
	app_id, secret = _app()
	try:
		short = _graph(
			"GET",
			"/oauth/access_token",
			params={"client_id": app_id, "client_secret": secret, "redirect_uri": _redirect_uri(), "code": code},
		)
		user_token = _graph(
			"GET",
			"/oauth/access_token",
			params={
				"grant_type": "fb_exchange_token",
				"client_id": app_id,
				"client_secret": secret,
				"fb_exchange_token": short["access_token"],
			},
		)["access_token"]
		me = _graph("GET", "/me", user_token, {"fields": "id,name"})
		pages = _graph_all("/me/accounts", user_token, {"fields": "id,name,category,access_token", "limit": 100})
	except MetaError as e:
		frappe.log_error(title="Meta: connect", message=str(e))
		return _finish_page(_("Facebook"), _("Facebook refused the connection: {0}").format(e), site, False)

	payload = {"nonce": data["nonce"], "user": me, "pages": pages}
	try:
		if site == frappe.local.site:
			store_connection(payload)
		else:
			_bench(
				["--site", site, "execute", "cred_hub.meta_leads.store_connection_from_env"],
				env={"CREDHUB_META_PAYLOAD": json.dumps(payload)},
			)
	except Exception:
		frappe.log_error(title="Meta: deliver connection")
		return _finish_page(_("Facebook"), _("We could not finish the connection. Please try again."), site, False)
	for page in pages:
		_route_page(page["id"], site)
	frappe.db.commit()
	return _finish_page(_("Facebook connected"), _("Facebook connected. You can close this window."), site)


def _route_page(page_id, site):
	# Uma página pode servir a várias empresas (agência, sócio de cliente): cada uma ganha a sua rota
	if not frappe.db.exists(ROUTE, {"page_id": page_id, "site": site}):
		frappe.get_doc({"doctype": ROUTE, "page_id": page_id, "site": site}).insert(ignore_permissions=True)


def store_connection_from_env():
	"""Site da empresa (chamado pelo site central com bench execute; os tokens vêm pelo ambiente)."""
	return store_connection(json.loads(os.environ["CREDHUB_META_PAYLOAD"]))


def store_connection(payload):
	from crm.lead_syncing.doctype.lead_sync_source.facebook import fetch_and_store_leadgen_forms_from_facebook

	nonce_key = f"credhub_meta_nonce:{payload.get('nonce')}"
	if not frappe.cache.get_value(nonce_key):
		frappe.throw(_("This connection request expired. Start it again."))
	frappe.cache.delete_value(nonce_key)

	kept = []
	for page in payload.get("pages") or []:
		page_id, token = page["id"], page.get("access_token")
		if not token:
			continue
		doc = frappe.get_doc(PAGE, page_id) if frappe.db.exists(PAGE, page_id) else frappe.new_doc(PAGE)
		doc.update({"page_id": page_id, "page_name": page.get("name"), "access_token": token})
		doc.save(ignore_permissions=True)
		if not frappe.db.exists("Facebook Page", page_id):
			# Registro do Cred Hub para ligar os formulários; o token fica só no Cred Hub Meta Page (criptografado)
			frappe.get_doc(
				{
					"doctype": "Facebook Page",
					"id": page_id,
					"page_name": page.get("name"),
					"category": page.get("category"),
					"account_id": (payload.get("user") or {}).get("id"),
				}
			).insert(ignore_permissions=True)
		try:
			_graph("POST", f"/{page_id}/subscribed_apps", token, {"subscribed_fields": "leadgen"})
			doc.db_set("subscribed", 1)
		except MetaError as e:
			frappe.log_error(title="Meta: subscribe page", message=f"{page_id}: {e}")
		fetch_and_store_leadgen_forms_from_facebook(page_id, token)
		kept.append(page_id)
	# Páginas que o admin deixou de compartilhar: o token sai
	for name in frappe.get_all(PAGE, filters={"name": ("not in", kept or [""])}, pluck="name"):
		frappe.delete_doc(PAGE, name, ignore_permissions=True, force=True)

	connection = frappe.get_single(CONNECTION)
	user = payload.get("user") or {}
	connection.update(
		{
			"connected": 1,
			"user_name": user.get("name"),
			"fb_user_id": user.get("id"),
			"connected_on": now_datetime(),
			"error": None,
			"error_on": None,
		}
	)
	connection.save(ignore_permissions=True)
	frappe.db.commit()
	return {"pages": kept}


# Tela de Origens de leads (site da empresa)


def _source_of(form_id):
	name = frappe.db.get_value("Lead Sync Source", {"type": "Facebook", "facebook_lead_form": form_id})
	return frappe.get_doc("Lead Sync Source", name) if name else None


@frappe.whitelist(methods=["GET"])
def get_facebook_connection():
	_only_managers()
	connection = frappe.get_single(CONNECTION)
	pages = []
	for page in frappe.get_all(PAGE, fields=["page_id", "page_name"], order_by="page_name asc"):
		forms = []
		for form in frappe.get_all("Facebook Lead Form", {"page": page.page_id}, ["id", "form_name"]):
			source = _source_of(form.id)
			questions = frappe.get_all(
				"Facebook Lead Form Question",
				filters={"parent": form.id, "parenttype": "Facebook Lead Form"},
				fields=["key", "label", "mapped_to_crm_field as mapped_to"],
				order_by="idx asc",
			)
			last = frappe.get_all(
				EVENT, {"form_id": form.id, "status": "Created"}, pluck="received_on", order_by="received_on desc", limit=1
			)
			forms.append(
				{
					"id": form.id,
					"name": form.form_name,
					"pipeline": source.pipeline if source else None,
					"enabled": bool(source and source.enabled),
					"last_lead_at": last[0] if last else None,
					"questions": questions,
				}
			)
		pages.append({"id": page.page_id, "name": page.page_name, "forms": forms})
	return {
		"connected": bool(connection.connected),
		"user_name": connection.user_name,
		"error": connection.error,
		"pages": pages,
	}


@frappe.whitelist(methods=["POST"])
def save_facebook_form(form_id: str, pipeline=None, enabled=None, mapping=None):
	"""Funil, liga/desliga e mapeamento (pergunta → campo do lead) de um formulário."""
	_only_managers()
	form = frappe.get_doc("Facebook Lead Form", form_id)
	if mapping is not None:
		mapping = frappe.parse_json(mapping) if isinstance(mapping, str) else mapping
		lead_meta = frappe.get_meta("CRM Lead")
		for question in form.questions:
			field = (mapping or {}).get(question.key)
			if field and not lead_meta.has_field(field):
				frappe.throw(_("Unknown field: {0}").format(field))
			question.mapped_to_crm_field = field or None
		form.save(ignore_permissions=True)

	source = _source_of(form_id)
	if not source:
		source = frappe.new_doc("Lead Sync Source")
		# Lead Sync Source é nomeado à mão (Prompt): um por formulário
		source.set("__newname", f"Facebook {form.form_name or form_id}"[:120] + f" ({form_id})")
		# Sem access_token aqui: quem busca os leads é o cred_hub (com o token criptografado da página)
		source.update(
			{
				"type": "Facebook",
				"facebook_page": form.page,
				"facebook_lead_form": form_id,
				"background_sync_frequency": "Daily",
				"enabled": 0,
			}
		)
	if pipeline is not None:
		source.pipeline = pipeline or None
	if enabled is not None:
		source.enabled = 1 if frappe.utils.cint(enabled) else 0
	source.flags.ignore_permissions = True
	# access_token fica vazio de propósito: o token da página mora criptografado no Cred Hub Meta Page e é o
	# cred_hub que busca os leads (com campanha e anúncio); assim a sincronização antiga não roda em paralelo
	source.flags.ignore_mandatory = True
	source.save()
	return get_facebook_connection()


@frappe.whitelist(methods=["POST"])
def disconnect_facebook():
	"""Apaga os tokens (política de privacidade) e desliga os formulários. A página continua assinada no app:
	outra empresa pode usar a mesma página (agência); sem token aqui, os leads dela não entram nesta empresa."""
	_only_managers()
	for page in frappe.get_all(PAGE, pluck="name"):
		frappe.delete_doc(PAGE, page, ignore_permissions=True, force=True)
	for name in frappe.get_all("Lead Sync Source", {"type": "Facebook", "enabled": 1}, pluck="name"):
		frappe.db.set_value("Lead Sync Source", name, "enabled", 0)
	connection = frappe.get_single(CONNECTION)
	connection.update({"connected": 0, "user_name": None, "fb_user_id": None, "error": None, "error_on": None})
	connection.save(ignore_permissions=True)
	return get_facebook_connection()


@frappe.whitelist(methods=["GET"])
def get_lead_source_activity(source=None, limit=50):
	"""Últimos leads e falhas de uma origem (ou de todas, sem `source`)."""
	_only_managers()
	events = frappe.get_all(
		EVENT,
		filters={"source": source} if source else {},
		fields=[
			"source", "form_id", "received_on", "status", "lead", "form_name", "pipeline", "assigned_to", "error",
			"card_doctype", "card_name", "welcome", "welcome_error", "welcome_conversation",
		],
		order_by="received_on desc",
		limit=min(frappe.utils.cint(limit) or 50, 200),
	)
	names = dict(
		frappe.get_all("CRM Lead", {"name": ("in", [e.lead for e in events if e.lead] or [""])}, ["name", "lead_name"], as_list=True)
	)
	labels = dict(
		frappe.get_all(
			"Cred Hub Lead Source", {"name": ("in", [e.source for e in events] or [""])}, ["name", "label"], as_list=True
		)
	)
	pipelines = dict(
		frappe.get_all(
			"CRM Pipeline", {"name": ("in", [e.pipeline for e in events if e.pipeline] or [""])}, ["name", "pipeline_name"], as_list=True
		)
	)
	return [
		{
			"time": e.received_on,
			"source": e.source,
			"source_label": labels.get(e.source, e.source),
			"status": e.status,
			"lead": e.lead,
			"lead_name": names.get(e.lead),
			"form_name": e.form_name,
			"pipeline": pipelines.get(e.pipeline, e.pipeline),
			"assigned_to": e.assigned_to,
			"error": e.error,
			"card": {"doctype": e.card_doctype, "name": e.card_name} if e.card_name else None,
			# Boas-vindas pelo WhatsApp (credtech#53): Queued, Sent, Failed ou nada (desligada)
			"welcome": e.welcome or None,
			"welcome_error": e.welcome_error,
			"welcome_url": welcome.conversation_url(e),
		}
		for e in events
	]


# Webhook (site central) e ingestão (site da empresa)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def meta_webhook():
	if not _is_central():
		raise frappe.PermissionError
	request = frappe.request
	if request.method == "GET":
		args = request.args
		token = os.environ.get("CREDHUB_META_VERIFY_TOKEN") or ""
		if args.get("hub.mode") == "subscribe" and token and hmac.compare_digest(token, args.get("hub.verify_token") or ""):
			frappe.response.update({"type": "binary", "filename": "challenge.txt", "filecontent": args.get("hub.challenge") or ""})
			return
		raise frappe.PermissionError
	_app_id, secret = _app()
	raw = request.get_data() or b""
	expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256") or ""):
		raise frappe.AuthenticationError
	body = json.loads(raw or b"{}")
	for entry in body.get("entry") or []:
		for change in entry.get("changes") or []:
			value = change.get("value") or {}
			if change.get("field") != "leadgen" or not value.get("leadgen_id"):
				continue
			page_id = str(value.get("page_id") or entry.get("id"))
			# Cada empresa que conectou a página recebe; só cria o card quem tem o formulário ligado
			for site in frappe.get_all(ROUTE, {"page_id": page_id}, pluck="site"):
				frappe.enqueue(
					"cred_hub.meta_leads.dispatch_lead",
					queue="short",
					site=site,
					leadgen_id=str(value["leadgen_id"]),
					page_id=page_id,
					form_id=str(value.get("form_id") or ""),
				)
	return {"ok": True}


def dispatch_lead(site, leadgen_id, page_id, form_id):
	from cred_hub.signup import _bench, _site_exists

	if site == frappe.local.site:
		return ingest_lead(leadgen_id, page_id, form_id)
	if _site_exists(site):
		_bench(
			[
				"--site",
				site,
				"execute",
				"cred_hub.meta_leads.ingest_lead",
				"--kwargs",
				json.dumps({"leadgen_id": leadgen_id, "page_id": page_id, "form_id": form_id}),
			],
			timeout=120,
		)


def _record(status, leadgen_id, form_id, **values):
	return frappe.get_doc(
		{
			"doctype": EVENT,
			"source": "Facebook",
			"status": status,
			"leadgen_id": leadgen_id,
			"form_id": form_id,
			"received_on": now_datetime(),
			**values,
		}
	).insert(ignore_permissions=True)


def _already_seen(leadgen_id):
	return frappe.db.exists("CRM Lead", {"facebook_lead_id": leadgen_id}) or frappe.db.exists(
		EVENT, {"leadgen_id": leadgen_id, "status": ("in", ("Created", "Duplicate"))}
	)


def ingest_lead(leadgen_id, page_id=None, form_id=None, lead=None):
	"""Site da empresa: busca o lead (se não veio) e o põe no funil do formulário.
	Webhook e rede de segurança podem chegar juntos: a trava por lead vai até depois do commit."""
	lock = f"credhub_meta_lead:{leadgen_id}"[:64]
	if not frappe.db.sql("SELECT GET_LOCK(%s, 30)", lock)[0][0]:
		return
	try:
		_ingest_lead(leadgen_id, page_id, form_id, lead)
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock)


def _ingest_lead(leadgen_id, page_id, form_id, lead):
	if _already_seen(leadgen_id):
		return
	source = _source_of(form_id) if form_id else None
	# Empresa que conectou a página mas não usa este formulário (página compartilhada): não é com ela
	if not source or not source.enabled or not source.pipeline:
		return
	form_name = frappe.db.get_value("Facebook Lead Form", form_id, "form_name")
	if not frappe.db.exists(PAGE, page_id or source.facebook_page):
		# Formulário ligado, mas a página foi desconectada: o admin precisa ver
		_record("Failed", leadgen_id, form_id, form_name=form_name, pipeline=source.pipeline, error=_("The Facebook Page is not connected."))
		frappe.db.commit()
		return
	page_id = page_id or source.facebook_page
	# Falha desfaz só o que este lead fez (não o resto da transação de quem chamou)
	savepoint = "credhub_meta_lead"
	frappe.db.savepoint(savepoint)
	try:
		token = frappe.get_doc(PAGE, page_id).get_password("access_token")
		lead = lead or _graph("GET", f"/{leadgen_id}", token, {"fields": LEAD_FIELDS})
		doc, unmapped = _lead_doc(lead, source, form_id, form_name)
		doc.insert(ignore_permissions=True)
		duplicate = doc.flags.duplicate_of
		card = duplicate or frappe.db.get_value("CRM Deal", {"lead": doc.name})
		if card and unmapped and not duplicate:
			_note_answers(card, unmapped)
		event = _record(
			"Duplicate" if duplicate else "Created",
			leadgen_id,
			form_id,
			form_name=form_name,
			pipeline=source.pipeline,
			lead=doc.name,
			card_doctype="CRM Deal" if card else None,
			card_name=card,
			assigned_to=frappe.db.get_value("CRM Deal", card, "deal_owner") if card else None,
		)
		if card and not duplicate:
			welcome.queue(event)
		frappe.db.commit()
	except MetaTokenError as e:
		frappe.db.rollback(save_point=savepoint)
		_token_failed(str(e))
		_record("Failed", leadgen_id, form_id, form_name=form_name, pipeline=source.pipeline, error=str(e))
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(title=f"Meta lead {leadgen_id}")
		_record("Failed", leadgen_id, form_id, form_name=form_name, pipeline=source.pipeline, error=str(e)[:500])
		frappe.db.commit()


def _lead_doc(lead, source, form_id, form_name):
	questions = frappe.get_all(
		"Facebook Lead Form Question",
		filters={"parent": form_id, "parenttype": "Facebook Lead Form"},
		fields=["key", "label", "mapped_to_crm_field"],
	)
	mapping = {q.key: q.mapped_to_crm_field for q in questions if q.mapped_to_crm_field}
	labels = {q.key: q.label or q.key for q in questions}
	answers = {item["name"]: item["values"][0] for item in lead.get("field_data") or [] if item.get("values")}
	values = {mapping[k]: v for k, v in answers.items() if k in mapping}
	# Na anotação vai o texto da pergunta, não a chave técnica ("Cidade", não "cidade")
	unmapped = {labels.get(k, k): v for k, v in answers.items() if k not in mapping}
	captured = lead.get("created_time")
	from cred_hub.lead_intake import card_source

	# Origem do card com a plataforma e o formulário, ex.: "Facebook: Simulação de crédito" (credtech#188)
	platform = PLATFORMS.get(lead.get("platform") or "fb", str(lead.get("platform")).title())
	values.update(
		{
			"source": card_source(f"{platform}: {form_name}" if form_name else platform),
			"facebook_lead_id": lead["id"],
			"facebook_form_id": form_id,
			"pipeline": source.pipeline,
			"ad_platform": PLATFORMS.get(lead.get("platform") or "fb", str(lead.get("platform")).title()),
			"ad_campaign_id": lead.get("campaign_id"),
			"ad_campaign_name": lead.get("campaign_name"),
			"ad_adset_id": lead.get("adset_id"),
			"ad_adset_name": lead.get("adset_name"),
			"ad_id": lead.get("ad_id"),
			"ad_name": lead.get("ad_name"),
			"ad_form_name": form_name,
			"lead_captured_at": convert_utc_to_system_timezone(get_datetime(captured)).replace(tzinfo=None)
			if captured
			else None,
		}
	)
	if not values.get("first_name"):
		values["first_name"] = answers.get("full_name") or answers.get("first_name") or values.get("email") or _("Facebook lead")
	return frappe.get_doc({"doctype": "CRM Lead", **values}), unmapped


def _note_answers(card, answers):
	rows = "".join(
		f"<li><b>{frappe.utils.escape_html(k)}</b>: {frappe.utils.escape_html(str(v))}</li>" for k, v in answers.items()
	)
	frappe.get_doc(
		{
			"doctype": "FCRM Note",
			# Webhooks come as Guest, in the language of nobody: the note keeps the company's language
			"title": _("Lead form answers", lang=frappe.db.get_default("lang") or "pt-BR"),
			"content": f"<ul>{rows}</ul>",
			"reference_doctype": "CRM Deal",
			"reference_docname": card,
		}
	).insert(ignore_permissions=True)


def _token_failed(message):
	"""Conexão caiu: aparece na tela e os admins recebem aviso (no máximo um por dia)."""
	connection = frappe.get_single(CONNECTION)
	connection.update({"error": message[:500], "error_on": now_datetime()})
	connection.save(ignore_permissions=True)
	if frappe.cache.get_value("credhub_meta_token_alert"):
		return
	frappe.cache.set_value("credhub_meta_token_alert", 1, expires_in_sec=24 * 60 * 60)
	text = _("The Facebook connection stopped working. Open Settings › Lead Sources and connect again.")
	for user in frappe.get_all("Has Role", {"role": ("in", MANAGERS), "parenttype": "User"}, pluck="parent", distinct=True):
		if user in ("Administrator", "Guest") or not frappe.db.get_value("User", user, "enabled"):
			continue
		frappe.get_doc(
			{"doctype": "CRM Notification", "from_user": "Administrator", "to_user": user, "type": "Automation", "notification_text": text}
		).insert(ignore_permissions=True)


def catch_up():
	"""Agendador (10 min): leads recentes dos formulários ligados que o webhook não trouxe."""
	since = int(add_to_date(now_datetime(), hours=-3).timestamp())
	for source in frappe.get_all(
		"Lead Sync Source", {"type": "Facebook", "enabled": 1}, ["facebook_lead_form", "facebook_page"]
	):
		if not frappe.db.exists(PAGE, source.facebook_page):
			continue
		token = frappe.get_doc(PAGE, source.facebook_page).get_password("access_token")
		try:
			leads = _graph_all(
				f"/{source.facebook_lead_form}/leads",
				token,
				{
					"fields": LEAD_FIELDS,
					"limit": 100,
					"filtering": json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": since}]),
				},
			)
		except MetaTokenError as e:
			_token_failed(str(e))
			frappe.db.commit()
			continue
		except MetaError:
			frappe.log_error(title="Meta: catch up")
			continue
		for lead in leads:
			if not _already_seen(lead["id"]):
				ingest_lead(lead["id"], source.facebook_page, source.facebook_lead_form, lead=lead)


def ensure_meta_webhook():
	"""Site central (deploy): assina o webhook `leadgen` de páginas no app da Meta. Pode repetir."""
	if not _is_central() or not os.environ.get("FB_APP_ID") or not os.environ.get("CREDHUB_META_VERIFY_TOKEN"):
		return "webhook da Meta: pulado"
	app_id, secret = _app()
	try:
		_graph(
			"POST",
			f"/{app_id}/subscriptions",
			f"{app_id}|{secret}",
			{
				"object": "page",
				"callback_url": _hub_url("/api/method/cred_hub.meta_leads.meta_webhook"),
				"fields": "leadgen",
				"verify_token": os.environ["CREDHUB_META_VERIFY_TOKEN"],
			},
		)
	except MetaError as e:
		# Não derruba o deploy: os leads ainda entram pela rede de segurança; o erro fica no log do deploy
		frappe.log_error(title="Meta: subscribe app webhook", message=str(e))
		return f"webhook da Meta: FALHOU ({e})"
	return "webhook da Meta: assinado (leadgen)"
