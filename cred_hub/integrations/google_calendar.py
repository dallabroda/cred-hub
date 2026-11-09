"""Integração do HubTech CRM com o Google Calendar e Google Meet (OAuth2 por usuário).

Sincroniza tarefas com data/hora (`CRM Task`) com o Google Calendar do responsável
e gera link de videochamada no Google Meet quando solicitado.
"""

import urllib.parse
import frappe
from frappe import _
import requests

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def get_google_client_config():
	"""Obtém Client ID e Client Secret do Google Cloud das configurações do site ou ambiente."""
	client_id = frappe.conf.get("google_auth_client_id") or frappe.conf.get("google_client_id")
	client_secret = frappe.conf.get("google_auth_client_secret") or frappe.conf.get("google_client_secret")

	if not client_id or not client_secret:
		client_id = client_id or frappe.conf.get("GOOGLE_CLIENT_ID")
		client_secret = client_secret or frappe.conf.get("GOOGLE_CLIENT_SECRET")

	return client_id, client_secret


def get_redirect_uri():
	"""Retorna a URL de callback do OAuth2 baseada no domínio do site."""
	site_url = frappe.utils.get_url()
	return f"{site_url}/api/method/cred_hub.integrations.google_calendar.oauth_callback"


@frappe.whitelist()
def get_auth_url():
	"""Gera a URL de autorização OAuth2 do Google para o usuário logado."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Você precisa estar logado para conectar o Google Calendar."), frappe.PermissionError)

	client_id, _ = get_google_client_config()
	if not client_id:
		frappe.throw(_("Google Calendar não está configurado no servidor. Informe o Client ID e Client Secret nas configurações."))

	redirect_uri = get_redirect_uri()
	state = frappe.generate_hash(length=16)
	frappe.cache().set_value(f"google_oauth_state:{state}", user, expires_in_sec=600)

	params = {
		"client_id": client_id,
		"redirect_uri": redirect_uri,
		"response_type": "code",
		"scope": CALENDAR_SCOPE,
		"access_type": "offline",
		"prompt": "consent",
		"state": state,
	}

	return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


@frappe.whitelist(allow_guest=True)
def oauth_callback(code=None, state=None, error=None):
	"""Callback do Google OAuth2. Troca o código autorizador por tokens e salva no usuário."""
	if error:
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = f"/crm/settings/integrations?google_status=error&error={urllib.parse.quote(error)}"
		return

	if not code or not state:
		frappe.throw(_("Parâmetros inválidos na resposta do Google."))

	user = frappe.cache().get_value(f"google_oauth_state:{state}")
	if not user:
		user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Sessão expirada. Tente conectar novamente."))

	client_id, client_secret = get_google_client_config()
	redirect_uri = get_redirect_uri()

	response = requests.post(
		GOOGLE_TOKEN_URL,
		data={
			"code": code,
			"client_id": client_id,
			"client_secret": client_secret,
			"redirect_uri": redirect_uri,
			"grant_type": "authorization_code",
		},
		timeout=15,
	)

	if not response.ok:
		frappe.log_error(title="Google Calendar OAuth Error", message=response.text)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/crm/settings/integrations?google_status=failed"
		return

	data = response.json()
	access_token = data.get("access_token")
	refresh_token = data.get("refresh_token")
	expires_in = data.get("expires_in", 3600)

	expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=expires_in)

	user_doc = frappe.get_doc("User", user)
	if access_token:
		user_doc.db_set("google_calendar_access_token", access_token, update_modified=False)
	if refresh_token:
		user_doc.db_set("google_calendar_refresh_token", refresh_token, update_modified=False)
	user_doc.db_set("google_calendar_expires_at", expires_at, update_modified=False)
	user_doc.db_set("google_calendar_connected", 1, update_modified=False)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/crm/settings/integrations?google_status=connected"


@frappe.whitelist()
def get_connection_status():
	"""Retorna se o usuário logado está conectado com o Google Calendar."""
	user = frappe.session.user
	if user == "Guest":
		return {"connected": False}

	connected = bool(frappe.db.get_value("User", user, "google_calendar_connected"))
	expires_at = frappe.db.get_value("User", user, "google_calendar_expires_at")

	return {
		"connected": connected,
		"user": user,
		"expires_at": str(expires_at) if expires_at else None,
	}


@frappe.whitelist()
def disconnect():
	"""Desconecta a conta do Google Calendar do usuário ativo."""
	user = frappe.session.user
	if user == "Guest":
		return {"success": False}

	refresh_token = frappe.db.get_value("User", user, "google_calendar_refresh_token")
	if refresh_token:
		try:
			requests.post(GOOGLE_REVOKE_URL, params={"token": refresh_token}, timeout=5)
		except Exception:
			pass

	user_doc = frappe.get_doc("User", user)
	user_doc.db_set("google_calendar_access_token", None, update_modified=False)
	user_doc.db_set("google_calendar_refresh_token", None, update_modified=False)
	user_doc.db_set("google_calendar_expires_at", None, update_modified=False)
	user_doc.db_set("google_calendar_connected", 0, update_modified=False)

	return {"success": True}


def get_valid_access_token(user):
	"""Retorna um access token válido para o usuário, renovando via refresh_token se expirado."""
	user_data = frappe.db.get_value(
		"User",
		user,
		["google_calendar_access_token", "google_calendar_refresh_token", "google_calendar_expires_at", "google_calendar_connected"],
		as_dict=True,
	)

	if not user_data or not user_data.google_calendar_connected or not user_data.google_calendar_refresh_token:
		return None

	now = frappe.utils.now_datetime()
	if user_data.google_calendar_expires_at and user_data.google_calendar_expires_at > frappe.utils.add_to_date(now, minutes=2):
		return user_data.google_calendar_access_token

	client_id, client_secret = get_google_client_config()
	if not client_id or not client_secret:
		return None

	response = requests.post(
		GOOGLE_TOKEN_URL,
		data={
			"client_id": client_id,
			"client_secret": client_secret,
			"refresh_token": user_data.google_calendar_refresh_token,
			"grant_type": "refresh_token",
		},
		timeout=10,
	)

	if not response.ok:
		return None

	data = response.json()
	new_access_token = data.get("access_token")
	expires_in = data.get("expires_in", 3600)
	expires_at = frappe.utils.add_to_date(now, seconds=expires_in)

	user_doc = frappe.get_doc("User", user)
	user_doc.db_set("google_calendar_access_token", new_access_token, update_modified=False)
	user_doc.db_set("google_calendar_expires_at", expires_at, update_modified=False)

	return new_access_token


def sync_task_to_google_calendar(doc, method=None):
	"""Hook doc_event do CRM Task para criar/atualizar evento no Google Calendar."""
	if not doc.assigned_to:
		return

	access_token = get_valid_access_token(doc.assigned_to)
	if not access_token:
		return

	start_time = doc.start_date or doc.due_date
	if not start_time or doc.status in ("Canceled", "Done"):
		if doc.get("google_calendar_event_id"):
			delete_calendar_event(doc, access_token)
		return

	if not doc.get("google_calendar_event_id"):
		create_calendar_event(doc, access_token)
	else:
		update_calendar_event(doc, access_token)


def on_task_trash(doc, method=None):
	"""Hook doc_event ao deletar tarefa."""
	if doc.assigned_to and doc.get("google_calendar_event_id"):
		access_token = get_valid_access_token(doc.assigned_to)
		if access_token:
			delete_calendar_event(doc, access_token)


def create_calendar_event(doc, access_token):
	"""Cria evento no Google Calendar e gera Google Meet se `create_google_meet` ativo."""
	url = f"{GOOGLE_CALENDAR_API_URL}/calendars/primary/events?conferenceDataVersion=1"
	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

	start_dt = str(doc.due_date or doc.start_date)
	if len(start_dt) == 10:
		start_dt += "T09:00:00"

	start_obj = frappe.utils.get_datetime(start_dt)
	end_obj = frappe.utils.add_to_date(start_obj, hours=1)

	payload = {
		"summary": doc.title,
		"description": f"Tarefa do HubTech CRM\n\n{doc.description or ''}".strip(),
		"start": {"dateTime": start_obj.isoformat(), "timeZone": frappe.utils.get_time_zone()},
		"end": {"dateTime": end_obj.isoformat(), "timeZone": frappe.utils.get_time_zone()},
	}

	if doc.get("create_google_meet"):
		payload["conferenceData"] = {
			"createRequest": {
				"requestId": frappe.generate_hash(length=16),
				"conferenceSolutionKey": {"type": "hangoutsMeet"},
			}
		}

	try:
		res = requests.post(url, headers=headers, json=payload, timeout=10)
		if res.ok:
			data = res.json()
			event_id = data.get("id")
			meet_link = data.get("hangoutLink") or (
				data.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri")
				if data.get("conferenceData")
				else None
			)

			updates = {}
			if event_id:
				updates["google_calendar_event_id"] = event_id
			if meet_link:
				updates["google_meet_link"] = meet_link

			if updates:
				frappe.db.set_value("CRM Task", doc.name, updates, update_modified=False)
		else:
			frappe.log_error(title="Google Calendar Event Creation Failed", message=res.text)
	except Exception as e:
		frappe.log_error(title="Google Calendar Connection Error", message=str(e))


def update_calendar_event(doc, access_token):
	"""Atualiza evento existente no Google Calendar."""
	event_id = doc.get("google_calendar_event_id")
	if not event_id:
		return

	url = f"{GOOGLE_CALENDAR_API_URL}/calendars/primary/events/{event_id}?conferenceDataVersion=1"
	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

	start_dt = str(doc.due_date or doc.start_date)
	if len(start_dt) == 10:
		start_dt += "T09:00:00"

	start_obj = frappe.utils.get_datetime(start_dt)
	end_obj = frappe.utils.add_to_date(start_obj, hours=1)

	payload = {
		"summary": doc.title,
		"description": f"Tarefa do HubTech CRM\n\n{doc.description or ''}".strip(),
		"start": {"dateTime": start_obj.isoformat(), "timeZone": frappe.utils.get_time_zone()},
		"end": {"dateTime": end_obj.isoformat(), "timeZone": frappe.utils.get_time_zone()},
	}

	if doc.get("create_google_meet") and not doc.get("google_meet_link"):
		payload["conferenceData"] = {
			"createRequest": {
				"requestId": frappe.generate_hash(length=16),
				"conferenceSolutionKey": {"type": "hangoutsMeet"},
			}
		}

	try:
		res = requests.patch(url, headers=headers, json=payload, timeout=10)
		if res.ok:
			data = res.json()
			meet_link = data.get("hangoutLink")
			if meet_link:
				frappe.db.set_value("CRM Task", doc.name, "google_meet_link", meet_link, update_modified=False)
		else:
			frappe.log_error(title="Google Calendar Event Update Failed", message=res.text)
	except Exception as e:
		frappe.log_error(title="Google Calendar Update Connection Error", message=str(e))


def delete_calendar_event(doc, access_token):
	"""Remove evento do Google Calendar."""
	event_id = doc.get("google_calendar_event_id")
	if not event_id:
		return

	url = f"{GOOGLE_CALENDAR_API_URL}/calendars/primary/events/{event_id}"
	headers = {"Authorization": f"Bearer {access_token}"}

	try:
		res = requests.delete(url, headers=headers, timeout=10)
		if res.ok or res.status_code == 404:
			frappe.db.set_value(
				"CRM Task",
				doc.name,
				{"google_calendar_event_id": None, "google_meet_link": None},
				update_modified=False,
			)
	except Exception as e:
		frappe.log_error(title="Google Calendar Delete Event Error", message=str(e))
