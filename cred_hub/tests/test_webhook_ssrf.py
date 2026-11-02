"""Testes de proteção contra SSRF nos Webhooks de saída do HubTech."""

import datetime
import json
import sys
import unittest
from unittest.mock import MagicMock

# Stub frappe se não estiver rodando no bench
if "frappe" not in sys.modules:
    frappe_mock = MagicMock()
    sys.modules["frappe"] = frappe_mock
    sys.modules["frappe.utils"] = MagicMock()

from cred_hub.email_company import safe_http
from cred_hub.email_company.safe_http import BlockedAddress


class TestWebhookSsrf(unittest.TestCase):
    def test_safe_http_rejects_localhost_and_internal_ips(self):
        # Loopback e IPs locais
        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://127.0.0.1:8000/webhook")

        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://localhost:3000/webhook")

        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://10.0.0.5:8080/webhook")

        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://192.168.1.1:8080/webhook")

        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://meta-service.internal/webhook")

        # Portas não permitidas
        with self.assertRaises(BlockedAddress):
            safe_http.check_url("http://example.com:22/webhook")

        # Nomes públicos são aceitos pela checagem sintática
        host = safe_http.check_url("https://api.credtechdigital.com.br/webhook")
        self.assertEqual(host, "api.credtechdigital.com.br")

    def test_send_webhook_safeguard(self):
        import frappe
        from frappe.utils import now_datetime
        from cred_hub import webhooks

        now_datetime.return_value = datetime.datetime(2026, 11, 2, 12, 0, 0)
        frappe.as_json.side_effect = json.dumps
        fake_doc = MagicMock()
        fake_doc.status = "Active"
        fake_doc.webhook_url = "http://127.0.0.1:8000/internal-api"
        fake_doc.get_password.return_value = "whsec_test123"
        frappe.get_doc.return_value = fake_doc
        frappe.db.exists.return_value = True

        # Dispara webhook para destino bloqueado
        webhooks.send_webhook(
            webhook_name="WH-001",
            event_name="deal.created",
            payload_data={"name": "test-deal"},
            reference_doctype="CRM Deal",
            reference_name="DEAL-001",
        )

        # Confere que registrou falha de segurança
        calls = frappe.db.set_value.call_args_list
        self.assertTrue(any(call[0][2].get("last_delivery_status") == "Failed" for call in calls))
