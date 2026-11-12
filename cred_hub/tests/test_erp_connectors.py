"""Conformance kit of the ERP connectors (ADR 0007, credtech#375). Every registered connector must pass it.
Pure: `python -m pytest cred_hub/tests/test_erp_connectors.py` (no Frappe, nothing leaves the machine)."""

import json
import os
import unittest
from datetime import date

from cred_hub.erp import connectors
from cred_hub.erp.base import ErpAuthError, ErpError, ErpMaintenance, ErpRateLimited
from cred_hub.erp.connectors.base import TITLE_KEYS, TITLE_OUTCOMES, Connector
from cred_hub.tests.test_erp_clients import SMART_TOKEN, Clock, FakeResponse, FakeSession

# Capability -> the contract methods a connector declaring it must implement
REQUIRED = {
	"register_cedente": ("register_cedente", "finalize_cedente"),
	"overdue_titles": ("overdue_titles",),
	"settled_titles": ("settled_titles",),
	"sacado_registry": ("sacados",),
	"cedente_registry": ("cedentes",),
	"lists": ("lists",),
}


def _implements(cls, method: str) -> bool:
	return getattr(cls, method) is not getattr(Connector, method)


class TestEveryConnector(unittest.TestCase):
	def test_smart_and_wba_are_registered(self):
		self.assertLessEqual({"Smart Securities", "WBA", "QI Tech"}, set(connectors.keys()))

	def test_settings_offer_exactly_the_registered_systems(self):
		path = os.path.join(
			os.path.dirname(__file__), "..", "cred_hub", "doctype", "cred_hub_erp_settings", "cred_hub_erp_settings.json"
		)
		with open(path, encoding="utf-8") as f:
			field = next(f for f in json.load(f)["fields"] if f["fieldname"] == "erp_provider")
		self.assertEqual({o for o in field["options"].split("\n") if o}, set(connectors.keys()))

	def test_description_is_complete(self):
		for cls in connectors.all_connectors():
			with self.subTest(cls.key):
				self.assertTrue(cls.label)
				self.assertTrue(cls.credentials, "declares its access fields")
				self.assertTrue(any(c.secret for c in cls.credentials), "has at least one secret")
				self.assertEqual(len({c.fieldname for c in cls.credentials}), len(cls.credentials))
				self.assertTrue(_implements(cls, "make_client") and _implements(cls, "ping"))

	def test_declared_capabilities_are_implemented(self):
		for cls in connectors.all_connectors():
			for capability, methods in REQUIRED.items():
				if getattr(cls.capabilities, capability):
					for method in methods:
						with self.subTest(cls.key, method=method):
							self.assertTrue(_implements(cls, method), f"{cls.key} declares {capability}")

	def test_cedente_registration_has_catalog_and_suggested_de_para(self):
		for cls in connectors.all_connectors():
			if cls.capabilities.register_cedente:
				with self.subTest(cls.key):
					catalog = {f["erp_field"] for f in cls.catalog()}
					self.assertTrue(catalog)
					rules = cls.default_rules()
					self.assertTrue(rules)
					self.assertLessEqual({r["erp_field"] for r in rules}, catalog, "suggested fields exist")

	def test_suggested_transforms_exist_in_the_engine_and_the_field_map(self):
		from cred_hub.erp.mapper import TRANSFORMS

		path = os.path.join(
			os.path.dirname(__file__), "..", "cred_hub", "doctype", "cred_hub_erp_field_map", "cred_hub_erp_field_map.json"
		)
		with open(path, encoding="utf-8") as f:
			field = next(f for f in json.load(f)["fields"] if f["fieldname"] == "transform")
		allowed = set(field["options"].splitlines()) | {""}
		for cls in connectors.all_connectors():
			for rule in [*cls.default_rules(), *cls.catalog()]:
				with self.subTest(cls.key, field=rule["erp_field"]):
					self.assertIn(rule["transform"], TRANSFORMS)
					self.assertIn(rule["transform"], allowed)

	def test_missing_credentials_are_named(self):
		for cls in connectors.all_connectors():
			with self.subTest(cls.key):
				missing = cls.missing_credentials({})
				self.assertEqual(missing, [c.label for c in cls.credentials if c.required])

	def test_register_outcome_rejects_unknown_status(self):
		with self.assertRaises(ValueError):
			connectors.RegisterOutcome("done")
		self.assertEqual(
			connectors.RegisterOutcome("already", None, "x").as_dict(), {"status": "already", "id": None, "note": "x"}
		)


def smart_connector(routes):
	clock = Clock()
	session = FakeSession({("POST", "/oauth"): [SMART_TOKEN], **routes})
	cls = connectors.get("Smart Securities")
	client = cls.make_client(
		{"smart_client_id": "cid", "smart_client_secret": "csecret"}, session=session, clock=clock, sleep=clock.sleep
	)
	return cls(client), session


TITLE_ROW = {
	"ID_TITULO": "77", "DOCUMENTO": "NF-1", "CPF_CNPJ_SACADO": "12.345.678/0001-95", "SACADO": "Sacado SA",
	"CPF_CNPJ_CEDENTE": "23.268.852/0001-60", "CEDENTE": "Cedente LTDA", "VENCIMENTO": "01/10/2026",
	"VALOR": "1.587,65", "SITUACAO": "Em aberto",
}


def page(key, rows):
	return FakeResponse(200, {"_embedded": {key: rows}, "page": 1, "page_count": 1})


class TestSmartConformance(unittest.TestCase):
	"""The shared behaviors every connector must show, checked on the Smart connector with canned answers."""

	def test_titles_come_in_the_common_shape(self):
		conn, session = smart_connector({("GET", "/smartsecurities/titulo"): [page("titulo", [TITLE_ROW])]})
		titles = list(conn.overdue_titles(date(2026, 11, 1)))
		self.assertEqual(len(titles), 1)
		self.assertEqual(set(titles[0]), TITLE_KEYS)
		self.assertIn(titles[0]["outcome"], TITLE_OUTCOMES)
		self.assertEqual(titles[0]["raw"], TITLE_ROW)
		params = session.sent[-1]["params"]
		self.assertEqual(params["tipo"], "emaberto")

	def test_settled_titles_ask_the_settled_report(self):
		row = {**TITLE_ROW, "SITUACAO": "Quitado", "QUITACAO": "05/11/2026", "VALOR_LIQUIDADO": "1.587,65"}
		conn, session = smart_connector({("GET", "/smartsecurities/titulo"): [page("titulo", [row])]})
		titles = list(conn.settled_titles(date(2026, 11, 1), date(2026, 11, 11)))
		self.assertEqual(titles[0]["outcome"], "paid")
		self.assertEqual(session.sent[-1]["params"]["tipo"], "quitados")

	def test_401_twice_is_a_credential_error(self):
		conn, _session = smart_connector({
			("GET", "/smartsecurities/setor"): [FakeResponse(401, {"detail": "no"})],
		})
		with self.assertRaises(ErpAuthError):
			conn.lists("setor")

	def test_maintenance_stops(self):
		conn, _session = smart_connector({
			("GET", "/smartsecurities/setor"): [FakeResponse(500, {"detail": "Sistema em manutenção"})],
		})
		with self.assertRaises(ErpMaintenance):
			conn.lists("setor")

	def test_minimum_interval_is_a_rate_limit(self):
		conn, _session = smart_connector({
			("GET", "/smartsecurities/titulo"): [
				FakeResponse(429, {"detail": "A requisição deve ter um intervalo mínimo de 10 minuto(s)"})
			],
		})
		with self.assertRaises(ErpRateLimited):
			list(conn.overdue_titles(date(2026, 11, 1)))

	def test_lists_are_names_never_codes(self):
		conn, _session = smart_connector({
			("GET", "/smartsecurities/setor"): [page("setor", [{"id": "1", "descricao": "Indústria"}, {"id": "2"}])],
		})
		self.assertEqual(conn.lists("setor"), ["Indústria"])

	def test_pace_is_declared(self):
		self.assertGreaterEqual(connectors.get("Smart Securities").pace.report_cooldown, 600)

	def test_errors_never_carry_the_secret(self):
		conn, _session = smart_connector({
			("GET", "/smartsecurities/setor"): [FakeResponse(400, {"detail": "bad request"})],
		})
		with self.assertRaises(ErpError) as caught:
			conn.lists("setor")
		self.assertNotIn("csecret", str(caught.exception))


if __name__ == "__main__":
	unittest.main()
