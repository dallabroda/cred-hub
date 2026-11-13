from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


def _load_health_module():
	"""Load cred_hub/health.py with a lightweight fake `frappe` stub when running pure unit tests outside bench."""
	if "frappe" not in sys.modules:
		fake_frappe = types.ModuleType("frappe")
		fake_frappe.local = types.SimpleNamespace(conf={}, request=None, response={})
		fake_frappe.form_dict = {}
		fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
		fake_frappe.get_common_site_config = lambda: {}
		fake_frappe.get_site_path = lambda: "."
		sys.modules["frappe"] = fake_frappe

	health_path = pathlib.Path(__file__).resolve().parents[1] / "health.py"
	spec = importlib.util.spec_from_file_location("cred_hub_health_test_mod", health_path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


class TestHealthcheck(unittest.TestCase):
	def test_rejects_missing_or_wrong_token(self):
		health = _load_health_module()
		frappe = sys.modules["frappe"]
		frappe.local.conf = {"credhub_health_token": "super-secret-health-token"}
		frappe.local.response = {}
		frappe.form_dict = {"token": "wrong-token"}

		out = health.check()
		self.assertEqual(out["status"], "forbidden")
		self.assertEqual(frappe.local.response.get("http_status_code"), 403)

	def test_returns_healthy_when_all_subsystems_pass(self):
		health = _load_health_module()
		frappe = sys.modules["frappe"]
		frappe.local.conf = {"credhub_health_token": "valid-token-123"}
		frappe.local.response = {}
		frappe.form_dict = {"token": "valid-token-123"}

		with (
			patch.object(health, "_check_db", return_value={"ok": True, "latency_ms": 0.8}),
			patch.object(health, "_check_redis", return_value={"ok": True, "cache": True, "queue": True}),
			patch.object(health, "_check_queues", return_value={"ok": True, "active_workers": 3}),
			patch.object(health, "_check_scheduler", return_value={"ok": True, "disabled": False, "inactive": False}),
			patch.object(health, "_check_disk", return_value={"ok": True, "used_pct": 41.2, "free_gb": 42.0}),
		):
			out = health.check()

		self.assertEqual(out["status"], "healthy")
		self.assertEqual(out["failed"], [])
		self.assertNotIn("http_status_code", frappe.local.response)

	def test_returns_503_when_worker_queue_or_disk_fails(self):
		health = _load_health_module()
		frappe = sys.modules["frappe"]
		frappe.local.conf = {"credhub_health_token": "valid-token-123"}
		frappe.local.response = {}
		frappe.form_dict = {"token": "valid-token-123"}

		with (
			patch.object(health, "_check_db", return_value={"ok": True, "latency_ms": 1.1}),
			patch.object(health, "_check_redis", return_value={"ok": True}),
			patch.object(
				health,
				"_check_queues",
				return_value={"ok": False, "missing_worker_queues": ["credhub_provision"]},
			),
			patch.object(health, "_check_scheduler", return_value={"ok": True}),
			patch.object(health, "_check_disk", return_value={"ok": False, "used_pct": 92.0, "free_gb": 4.1}),
		):
			out = health.check()

		self.assertEqual(out["status"], "unhealthy")
		self.assertEqual(out["failed"], ["queues", "disk"])
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)


if __name__ == "__main__":
	unittest.main()
