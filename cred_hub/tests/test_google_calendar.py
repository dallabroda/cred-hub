"""Testes unitários da integração do Google Calendar e Google Meet no HubTech CRM (sem depender do ambiente Frappe ativo)."""

import json
import sys
from unittest.mock import MagicMock, patch
import pytest

# Mock do frappe se não estiver inicializado no ambiente local
if "frappe" not in sys.modules or isinstance(sys.modules["frappe"], MagicMock):
	mock_frappe = MagicMock()
	mock_frappe.whitelist.side_effect = lambda *args, **kwargs: (lambda fn: fn)
	mock_frappe.utils.get_url.return_value = "https://hub.credtechdigital.com.br"
	mock_frappe.utils.now_datetime.return_value = "2026-11-10 12:00:00"
	mock_frappe.utils.add_to_date.side_effect = lambda dt, **kwargs: dt
	mock_frappe.utils.get_time_zone.return_value = "America/Sao_Paulo"
	mock_frappe.conf.get.return_value = None
	mock_frappe.session = MagicMock()
	mock_frappe.session.user = "test@credtechdigital.com.br"
	sys.modules["frappe"] = mock_frappe

from cred_hub.integrations import google_calendar


class FakeResponse:
	def __init__(self, status_code, body=None):
		self.status_code = status_code
		self.ok = 200 <= status_code < 300
		self._body = body or {}
		self.text = json.dumps(self._body)

	def json(self):
		return self._body


def test_get_google_client_config(monkeypatch):
	def fake_get(key):
		if "id" in key:
			return "id123"
		if "secret" in key:
			return "sec123"
		return None

	monkeypatch.setattr(google_calendar.frappe.conf, "get", fake_get)
	client_id, client_secret = google_calendar.get_google_client_config()
	assert client_id == "id123"
	assert client_secret == "sec123"


def test_get_auth_url_guest_throws(monkeypatch):
	monkeypatch.setattr(google_calendar.frappe.session, "user", "Guest")
	google_calendar.frappe.PermissionError = Exception
	google_calendar.frappe.throw = MagicMock(side_effect=Exception("Permission Error"))

	with pytest.raises(Exception):
		google_calendar.get_auth_url()


def test_get_auth_url_valid_user(monkeypatch):
	monkeypatch.setattr(google_calendar.frappe.session, "user", "test@credtechdigital.com.br")
	monkeypatch.setattr(google_calendar, "get_google_client_config", lambda: ("client_123", "secret_123"))
	monkeypatch.setattr(google_calendar.frappe.utils, "get_url", lambda: "https://hub.credtechdigital.com.br")

	url = google_calendar.get_auth_url()
	assert "accounts.google.com" in url
	assert "client_id=client_123" in url
	assert "calendar.events" in urllib_parse_unquote(url)


def urllib_parse_unquote(url):
	import urllib.parse
	return urllib.parse.unquote(url)


def test_create_calendar_event(monkeypatch):
	fake_task = MagicMock()
	fake_task.name = "TASK-0001"
	fake_task.title = "Reunião de Alinhamento"
	fake_task.description = "Discutir termos do contrato"
	fake_task.start_date = "2026-11-10"
	fake_task.due_date = "2026-11-10 14:00:00"
	fake_task.create_google_meet = 1
	fake_task.get = lambda k, default=None: getattr(fake_task, k, default)

	called_set_value = {}

	def fake_set_value(doctype, name, values, update_modified=False):
		called_set_value.update(values if isinstance(values, dict) else {name: values})

	monkeypatch.setattr(google_calendar.frappe.db, "set_value", fake_set_value)

	def fake_post(url, headers=None, json=None, timeout=None):
		assert "conferenceDataVersion=1" in url
		assert json["summary"] == "Reunião de Alinhamento"
		assert "conferenceData" in json
		return FakeResponse(
			200,
			{
				"id": "google_event_999",
				"hangoutLink": "https://meet.google.com/abc-defg-hij",
			},
		)

	monkeypatch.setattr(google_calendar.requests, "post", fake_post)

	google_calendar.create_calendar_event(fake_task, "access_token_123")

	assert called_set_value.get("google_calendar_event_id") == "google_event_999"
	assert called_set_value.get("google_meet_link") == "https://meet.google.com/abc-defg-hij"


def test_delete_calendar_event(monkeypatch):
	fake_task = MagicMock()
	fake_task.name = "TASK-0001"
	fake_task.get = lambda k, default=None: "google_event_999" if k == "google_calendar_event_id" else default

	called_set_value = {}

	def fake_set_value(doctype, name, values, update_modified=False):
		called_set_value.update(values if isinstance(values, dict) else {name: values})

	monkeypatch.setattr(google_calendar.frappe.db, "set_value", fake_set_value)

	def fake_delete(url, headers=None, timeout=None):
		assert "google_event_999" in url
		return FakeResponse(200, {})

	monkeypatch.setattr(google_calendar.requests, "delete", fake_delete)

	google_calendar.delete_calendar_event(fake_task, "access_token_123")

	assert called_set_value.get("google_calendar_event_id") is None
	assert called_set_value.get("google_meet_link") is None
