"""Version of the HubTech public API (credtech#375, ADR 0007 section 11).

- Every call made with an API key answers `X-HubTech-API-Version` with the version that served it.
- A client may pin the version by sending the same header. Only "1" exists today; any other value is refused with
  400 before anything runs, so an integration written for a future version never runs against an older one.
- Policy (documented in credtech docs/sistemas/cred-hub-api.md): a breaking change only ships in a new version;
  the old one keeps working for at least 90 days after the new one is announced. A route or behaviour scheduled to
  go away answers `Deprecation` (RFC 9745, `@<unix time>`), `Sunset` (RFC 8594, HTTP date) and a `Link` to the
  documentation, from the day it is deprecated until the sunset date.
"""

from datetime import date, datetime, time, timezone
from email.utils import format_datetime

import frappe
from frappe import _
from frappe.utils import getdate

HEADER = "X-HubTech-API-Version"
CURRENT = "1"
SUPPORTED = (CURRENT,)
# Minimum notice between announcing a breaking change (Deprecation) and removing it (Sunset). Every entry of
# DEPRECATED_ROUTES must respect it (tested). Owner-decided exceptions, outside this table: the old user tokens
# (`token api_key:api_secret`) got a 30-day transition (owner decision, credtech#175), announced with the same headers
# by api_keys._check_legacy_token; security fixes may restrict a route without notice (credtech docs, section 18).
MIN_NOTICE_DAYS = 90
# Routes on their way out: "<VERB> <path>" or "* <path>" -> {"deprecated_on", "sunset_on" (YYYY-MM-DD)}.
# Empty today: nothing in version 1 is deprecated.
DEPRECATED_ROUTES: dict[str, dict] = {}
DOCS_PATH = "/api-docs"


class UnsupportedApiVersion(frappe.ValidationError):
	"""The version asked in X-HubTech-API-Version does not exist (400)."""

	http_status_code = 400


def requested_version() -> str:
	"""The version the client pinned, or the current one. An unknown version is refused (400)."""
	value = (frappe.get_request_header(HEADER) or "").strip()
	if not value:
		return CURRENT
	if value not in SUPPORTED:
		frappe.throw(
			_("API version {0} does not exist. Supported versions: {1}.").format(
				frappe.utils.escape_html(value[:20]), ", ".join(SUPPORTED)
			),
			UnsupportedApiVersion,
		)
	return value


def _midnight_utc(day) -> datetime:
	return datetime.combine(getdate(day), time.min, tzinfo=timezone.utc)


def deprecation_headers(deprecated_on, sunset_on, today: date | None = None) -> dict:
	"""Headers of something deprecated on `deprecated_on` and removed on `sunset_on` (both dates). Nothing before the
	deprecation date; nothing after the sunset either (by then the route answers its own error)."""
	today = today or getdate()
	if not deprecated_on or getdate(deprecated_on) > today:
		return {}
	if sunset_on and getdate(sunset_on) < today:
		return {}
	headers = {
		"Deprecation": f"@{int(_midnight_utc(deprecated_on).timestamp())}",
		"Link": f'<{frappe.utils.get_url(DOCS_PATH)}>; rel="deprecation"; type="text/html"',
	}
	if sunset_on:
		headers["Sunset"] = format_datetime(_midnight_utc(sunset_on), usegmt=True)
	return headers


def route_deprecation(verb: str, path: str) -> dict:
	entry = DEPRECATED_ROUTES.get(f"{verb} {path}") or DEPRECATED_ROUTES.get(f"* {path}")
	if not entry:
		return {}
	return deprecation_headers(entry.get("deprecated_on"), entry.get("sunset_on"))


def set_version_headers(set_header, verb: str, path: str, version: str = CURRENT):
	"""Version header always; deprecation headers when the route is on its way out."""
	set_header(HEADER, version)
	for name, value in route_deprecation(verb, path).items():
		set_header(name, value)
