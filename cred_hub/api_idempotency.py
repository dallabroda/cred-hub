"""Idempotency-Key on every write made through an API key (credtech#375, ADR 0007 section 11).

A client that sends `Idempotency-Key: <1-255 printable ASCII characters>` on a write (POST/PUT/PATCH/DELETE on
/api/resource, and every write method, batch included) may resend the same call safely:

- first call: a row `Processing` is inserted and COMMITTED before the call runs. Its name is
  sha256(api key + Idempotency-Key), so the primary key itself is the concurrency guard: a second call with the same
  key while the first runs hits a duplicate key and answers 409, whatever the worker or server it lands on;
- after the call, the answer (status, content type and body up to MAX_BODY_BYTES) is stored for TTL_HOURS;
- same key, same request (sha256 of verb, path, arguments and uploaded files): the stored answer is replayed with
  `Idempotent-Replayed: true` and nothing runs again;
- same key, different request: 422; a key past its 24 hours is free again.

What is never stored: the Idempotency-Key itself (only the digest), request headers (the Authorization secret),
the request body (only its hash). A call that did nothing (5xx, 409 "database busy, send again", 429) frees the key,
so the client retries with the same one. Errors of the call itself (403, 404, 417...) are stored and replayed: the
same request would fail the same way; fix it and use a new key.

Storage is a database table, not Redis: the cache Redis of the bench has no persistence and evicts keys under
memory pressure, which would silently turn a retry into a second write. The table is small (bounded by the write
rate limits and the 24-hour window), keyed by its primary key, with an index on `expires_at` for the hourly cleanup.
"""

import hashlib
import json
from urllib.parse import unquote

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response

DOCTYPE = "Cred Hub API Idempotency"
HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
TTL_HOURS = 24
MAX_KEY_LENGTH = 255
# Bigger answers are not kept: a replay then returns the status and a short note instead of the body
MAX_BODY_BYTES = 256 * 1024
# Answers meaning "nothing was done, send the same call again": the key is freed instead of stored
RETRY_STATUSES = frozenset({409, 429})
CLEANUP_BATCH = 5000
FILE_CHUNK = 64 * 1024


class InvalidIdempotencyKey(frappe.ValidationError):
	"""The Idempotency-Key header is empty, too long or has characters outside printable ASCII (400)."""

	http_status_code = 400


class IdempotencyKeyInUse(frappe.ValidationError):
	"""A call with the same Idempotency-Key is still running (409): wait for its answer and send again."""

	http_status_code = 409


class IdempotencyKeyReused(frappe.ValidationError):
	"""The Idempotency-Key was already used for a different request in the last 24 hours (422)."""

	http_status_code = 422


def _header() -> str | None:
	value = frappe.get_request_header(HEADER)
	return None if value is None else str(value)


def _valid(value: str) -> bool:
	return 1 <= len(value) <= MAX_KEY_LENGTH and all(" " <= ch <= "~" for ch in value)


def row_name(api_key: str, idempotency_key: str) -> str:
	return hashlib.sha256(f"{api_key}\n{idempotency_key}".encode()).hexdigest()


def request_fingerprint() -> str:
	"""sha256 of what makes the call: verb, path, arguments (sorted) and the uploaded files (name and bytes)."""
	digest = hashlib.sha256()
	request = frappe.request
	digest.update((request.method or "GET").upper().encode())
	digest.update(b"\n" + unquote(request.path or "").encode())
	form = {k: v for k, v in (frappe.local.form_dict or {}).items() if k != "cmd"}
	digest.update(b"\n" + json.dumps(form, sort_keys=True, default=str, separators=(",", ":")).encode())
	files = getattr(request, "files", None) or {}
	try:
		items = sorted(files.items(multi=True), key=lambda item: item[0]) if hasattr(files, "items") else []
	except TypeError:
		items = sorted(files.items(), key=lambda item: item[0])
	for field, storage in items:
		digest.update(f"\nfile:{field}:{getattr(storage, 'filename', '')}\n".encode())
		stream = getattr(storage, "stream", None)
		if stream is None or not hasattr(stream, "read"):
			continue
		position = stream.tell() if hasattr(stream, "tell") else 0
		while chunk := stream.read(FILE_CHUNK):
			digest.update(chunk if isinstance(chunk, bytes) else str(chunk).encode())
		if hasattr(stream, "seek"):
			stream.seek(position)
	return digest.hexdigest()


def _insert(name: str, api_key: str, fingerprint: str, route: str, request_id: str) -> bool:
	"""Claim the key; False when a row with this name already exists (in flight, done or expired)."""
	now = now_datetime()
	try:
		frappe.db.sql(
			f"""insert into `tab{DOCTYPE}`
			(name, creation, modified, owner, modified_by, api_key, status, route, request_hash, request_id, expires_at)
			values (%s, %s, %s, 'Administrator', 'Administrator', %s, 'Processing', %s, %s, %s, %s)""",
			(name, now, now, api_key, route[:240], fingerprint, request_id, add_to_date(now, hours=TTL_HOURS)),
		)
	except Exception as exc:
		if frappe.db.is_duplicate_entry(exc) or frappe.db.is_primary_key_violation(exc):
			return False
		raise
	return True


def _stored(name: str):
	return frappe.db.get_value(
		DOCTYPE,
		name,
		[
			"request_hash",
			"status",
			"response_status",
			"response_content_type",
			"response_body",
			"body_omitted",
			"request_id",
			"expires_at",
		],
		as_dict=True,
	)


def begin(api_key: str, request_id: str) -> dict | None:
	"""Called by authenticate for writes, once the key, the route and the rate limit passed. Returns the claim to
	finish after the call, or raises: 400 bad key, 409 in flight, 422 reused, or the stored answer (replay)."""
	value = _header()
	if value is None:
		return None
	if not _valid(value):
		frappe.throw(
			_("Idempotency-Key must have 1 to {0} printable ASCII characters.").format(MAX_KEY_LENGTH),
			InvalidIdempotencyKey,
		)
	name = row_name(api_key, value)
	fingerprint = request_fingerprint()
	route = f"{(frappe.request.method or 'GET').upper()} {unquote(frappe.request.path or '')}"
	try:
		claimed = _insert(name, api_key, fingerprint, route, request_id)
		row = None if claimed else _stored(name)
		if row and row.expires_at and row.expires_at < now_datetime():
			# a key past its 24 hours is free again: drop it and claim once more. Only after reading that it expired:
			# a delete on a live claim would make concurrent waiters deadlock
			frappe.db.delete(DOCTYPE, {"name": name, "expires_at": ("<", now_datetime())})
			claimed = _insert(name, api_key, fingerprint, route, request_id)
			row = None if claimed else _stored(name)
	except frappe.QueryDeadlockError:
		# several calls raced for the same key: the database picked one; the others are told to send again
		frappe.db.rollback()
		frappe.throw(_("A request with this Idempotency-Key is being processed. Send it again."), IdempotencyKeyInUse)
	if claimed:
		# committed before the call runs: another worker sees the claim at once
		frappe.db.commit()
		return {"name": name}
	if not row:
		# the claim is not visible yet to this transaction, or was just freed: ask for a resend
		frappe.throw(_("A request with this Idempotency-Key is being processed. Send it again."), IdempotencyKeyInUse)
	if row.request_hash != fingerprint:
		frappe.throw(
			_("This Idempotency-Key was already used for a different request. Use a new key for a new request."),
			IdempotencyKeyReused,
		)
	if row.status != "Completed":
		frappe.throw(
			_("A request with this Idempotency-Key is still being processed. Wait for its answer and send it again."),
			IdempotencyKeyInUse,
		)
	_replay(row)


def _replay(row):
	"""Answer with what the first call answered; nothing runs. Raised as an HTTPException carrying the response:
	Frappe returns it as is, rolls back (nothing to undo) and still runs the after_request hooks."""
	if row.body_omitted:
		body = json.dumps(
			{
				"idempotent_replay": {
					"body_omitted": True,
					"original_request_id": row.request_id,
					"message": "The first answer was too large to keep; read the record again to get it.",
				}
			}
		)
		content_type = "application/json"
	else:
		body = row.response_body or ""
		content_type = row.response_content_type or "application/json"
	response = Response(body, status=row.response_status or 200, content_type=content_type)
	response.headers[REPLAY_HEADER] = "true"
	info = getattr(frappe.local, "credhub_api_key", None)
	if info is not None:
		info["idempotency"] = {"replayed": True}
	raise HTTPException(response=response)


def finish(claim: dict | None, response) -> None:
	"""after_request: keep the answer of the claimed call, or free the key when the call did nothing."""
	if not claim or claim.get("replayed") or not claim.get("name"):
		return
	status = getattr(response, "status_code", None) or 500
	if status >= 500 or status in RETRY_STATUSES:
		frappe.db.delete(DOCTYPE, {"name": claim["name"]})
		frappe.db.commit()
		return
	body = None
	omitted = 0
	try:
		if getattr(response, "is_streamed", False) or getattr(response, "direct_passthrough", False):
			omitted = 1
		else:
			data = response.get_data()
			if len(data) > MAX_BODY_BYTES:
				omitted = 1
			else:
				body = data.decode("utf-8")
	except (UnicodeDecodeError, RuntimeError):
		omitted, body = 1, None
	frappe.db.set_value(
		DOCTYPE,
		claim["name"],
		{
			"status": "Completed",
			"response_status": status,
			"response_content_type": (response.headers.get("Content-Type") or "")[:140] if response is not None else "",
			"response_body": body,
			"body_omitted": omitted,
		},
		update_modified=False,
	)
	frappe.db.commit()


def delete_expired():
	"""Hourly, in batches: keys past their 24 hours."""
	if not frappe.db.exists("DocType", DOCTYPE):
		return
	while True:
		names = frappe.get_all(
			DOCTYPE, {"expires_at": ("<", now_datetime())}, pluck="name", limit_page_length=CLEANUP_BATCH
		)
		if not names:
			break
		frappe.db.delete(DOCTYPE, {"name": ("in", names)})
		frappe.db.commit()
		if len(names) < CLEANUP_BATCH:
			break
