from __future__ import annotations

import hmac
import os
import shutil
import time
from typing import Any

import frappe

MAX_QUEUE_BACKLOG = 250
MAX_DISK_USED_PCT = 88.0
MIN_DISK_FREE_GB = 5.0
REQUIRED_WORKER_QUEUES = ("short", "long", "credhub_provision")


def _expected_health_token() -> str:
	conf_token = ""
	if getattr(frappe.local, "conf", None):
		conf_token = str(frappe.local.conf.get("credhub_health_token") or "").strip()
	if not conf_token:
		try:
			conf_token = str(frappe.get_common_site_config().get("credhub_health_token") or "").strip()
		except Exception:
			conf_token = ""
	if not conf_token:
		conf_token = (os.environ.get("CREDHUB_HEALTH_TOKEN") or "").strip()
	return conf_token


def _provided_health_token() -> str:
	req = getattr(frappe.local, "request", None)
	if req and req.headers:
		hdr = (req.headers.get("X-Health-Token") or "").strip()
		if hdr:
			return hdr
	return str(frappe.form_dict.get("token") or "").strip()


def _check_db() -> dict[str, Any]:
	t0 = time.monotonic()
	val = frappe.db.sql("SELECT 1")[0][0]
	ms = round((time.monotonic() - t0) * 1000, 2)
	return {"ok": int(val) == 1, "latency_ms": ms}


def _check_redis() -> dict[str, Any]:
	from frappe.utils.background_jobs import get_redis_conn

	cache_ok = bool(frappe.cache.ping())
	rq_conn = get_redis_conn()
	queue_ok = bool(rq_conn.ping())
	return {"ok": cache_ok and queue_ok, "cache": cache_ok, "queue": queue_ok}


def _check_queues() -> dict[str, Any]:
	from rq import Queue, Worker

	from frappe.utils.background_jobs import generate_qname, get_redis_conn

	rq_conn = get_redis_conn()
	workers = Worker.all(connection=rq_conn)
	covered_queues: set[str] = set()
	for w in workers:
		for qname in w.queue_names():
			# Frappe prefixes queue names with bench id, e.g. "home-frappe-frappe-bench:short"
			short_name = qname.rsplit(":", 1)[-1]
			covered_queues.add(short_name)

	missing_workers = [q for q in REQUIRED_WORKER_QUEUES if q not in covered_queues]

	depths: dict[str, int] = {}
	overloaded: list[str] = []
	for logical_q in ("short", "default", "long", "credhub_provision"):
		q = Queue(generate_qname(logical_q), connection=rq_conn)
		count = int(q.count)
		depths[logical_q] = count
		if count > MAX_QUEUE_BACKLOG:
			overloaded.append(f"{logical_q}:{count}")

	ok = (not missing_workers) and (not overloaded)
	return {
		"ok": ok,
		"active_workers": len(workers),
		"missing_worker_queues": missing_workers,
		"depths": depths,
		"overloaded": overloaded,
	}


def _check_scheduler() -> dict[str, Any]:
	from frappe.utils.scheduler import is_scheduler_disabled, is_scheduler_inactive

	disabled = bool(is_scheduler_disabled(verbose=False))
	inactive = bool(is_scheduler_inactive(verbose=False))
	return {
		"ok": (not disabled) and (not inactive),
		"disabled": disabled,
		"inactive": inactive,
	}


def _check_disk(target_path: str | None = None) -> dict[str, Any]:
	path = target_path or frappe.get_site_path()
	usage = shutil.disk_usage(path)
	used_pct = round((usage.used / usage.total) * 100.0, 2) if usage.total else 0.0
	free_gb = round(usage.free / (1024**3), 2)
	ok = (used_pct < MAX_DISK_USED_PCT) and (free_gb >= MIN_DISK_FREE_GB)
	return {
		"ok": ok,
		"used_pct": used_pct,
		"free_gb": free_gb,
	}


def evaluate_all_checks() -> dict[str, Any]:
	checks: dict[str, dict[str, Any]] = {}
	failed: list[str] = []
	for name, fn in (
		("db", _check_db),
		("redis", _check_redis),
		("queues", _check_queues),
		("scheduler", _check_scheduler),
		("disk", _check_disk),
	):
		try:
			res = fn()
		except Exception as exc:
			res = {"ok": False, "error": type(exc).__name__}
		checks[name] = res
		if not res.get("ok"):
			failed.append(name)

	return {
		"status": "healthy" if not failed else "unhealthy",
		"failed": failed,
		"checks": checks,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def check() -> dict[str, Any]:
	"""Authenticated deep healthcheck inspecting MariaDB, Redis, RQ workers/backlog, Scheduler and Disk."""
	expected = _expected_health_token()
	provided = _provided_health_token()
	if not expected or not provided or not hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8")):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden"}

	report = evaluate_all_checks()
	if report["status"] != "healthy":
		frappe.local.response["http_status_code"] = 503
	return report
