"""Cedente registration from the credit funnel, on a site (credtech#367). The ERP and the Receita are faked.
Runs on the bench: `bench --site <site> run-tests --module cred_hub.tests.test_erp_cedente_site`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.erp import cedente
from cred_hub.erp import settings as erp_settings

SECTORS = [{"idSetor": "264", "descricao": "Serviços"}, {"idSetor": "282", "descricao": "Serviço de TI"}]
MANAGERS = [{"idGerente": "1060026", "descricao": "NF SECURITIZADORA SA"}]
REGIMES = [{"idRegimeTributario": "S", "descricao": "Simples"}, {"idRegimeTributario": "R", "descricao": "Real"}]


class FakeSmart:
	def __init__(self, existing=None, duplicate=False):
		self.existing, self.duplicate, self.sent = existing, duplicate, []

	def find_cedente(self, cnpj):
		return self.existing

	def lookup(self, resource):
		return {"setor": SECTORS, "gerente": MANAGERS, "regime-tributario": REGIMES}[resource]

	def next_contract_number(self):
		return 4001125

	def create_cedente(self, payload):
		self.sent.append(payload)
		if self.duplicate:
			return {"created": False, "duplicate": True, "id": None, "message": "existe"}
		return {"created": True, "duplicate": False, "id": 2513, "message": ""}


class TestCedenteRegistration(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		from crm.api.pipeline import save_pipeline

		self.pipeline = save_pipeline(
			{"pipeline_name": "Crédito 367", "stages": [{"deal_status": "Análise"}, {"deal_status": "Aprovado"}]}
		)["name"]
		self.stage = frappe.db.get_value("CRM Deal Status", {"pipeline": self.pipeline, "deal_status": "Aprovado"}, "name")
		self.first = frappe.db.get_value("CRM Deal Status", {"pipeline": self.pipeline, "deal_status": "Análise"}, "name")
		doc = frappe.get_doc(erp_settings.SETTINGS)
		doc.update({
			"erp_enabled": 1, "erp_provider": "Smart Securities", "smart_client_id": "cid", "smart_client_secret": "x",
			"cedente_send_enabled": 1, "cedente_trigger": cedente.STAGE, "cedente_pipeline": self.pipeline,
			"cedente_stage": self.stage, "default_manager": "NF SECURITIZADORA SA",
		})
		doc.save(ignore_permissions=True)
		self.org = frappe.get_doc({
			"doctype": "CRM Organization", "organization_name": "CredTech 367", "cnpj": "60.098.673/0001-58",
			"legal_name": "CREDTECH INTELIGENCIA DE NEGOCIOS LTDA", "trade_name": "CREDTECH",
			"registry_email": "financeiro@credtech.example", "registry_phone": "(48) 96302275",
			"main_cnae": "6204000 - Consultoria em tecnologia da informação", "neighborhood": "ITACORUBI",
			"city": "FLORIANOPOLIS", "state": "SC", "address_street_type": "RODOVIA",
			"address_street": "AMARO ANTONIO VIEIRA", "address_number": "2489", "address_complement": "APT 106",
			"ibge_code": "4205407", "tax_regime": "Simples Nacional",
		})
		self.org.flags.cred_hub_enriching = True  # no Receita call from the test
		self.org.insert(ignore_permissions=True)
		address = frappe.get_doc({
			"doctype": "Address", "address_title": "CredTech 367 (Receita)", "address_type": "Office",
			"address_line1": "RODOVIA AMARO ANTONIO VIEIRA, 2489", "address_line2": "APT 106, ITACORUBI",
			"city": "FLORIANOPOLIS", "state": "SC", "pincode": "88034-102", "country": "Brazil",
			"links": [{"link_doctype": "CRM Organization", "link_name": self.org.name}],
		}).insert(ignore_permissions=True)
		frappe.db.set_value("CRM Organization", self.org.name, "address", address.name)
		self.deal = frappe.get_doc({
			"doctype": "CRM Deal", "pipeline": self.pipeline, "status": self.first, "organization": self.org.name,
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def comments(self, doctype, name):
		return frappe.get_all("Comment", {"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"}, pluck="content")

	def send(self, fake):
		with patch("cred_hub.erp.settings.client", return_value=fake), patch.object(cedente, "ensure_enriched"):
			return cedente.send_for_deal(self.deal.name)

	def test_entering_the_stage_queues_the_registration(self):
		with patch.object(frappe, "enqueue") as enqueue:
			self.deal.status = self.stage
			self.deal.save(ignore_permissions=True)
		calls = [c for c in enqueue.call_args_list if c.args and c.args[0] == "cred_hub.erp.cedente.send_for_deal"]
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0].kwargs["deal"], self.deal.name)

	def test_another_stage_or_switched_off_does_nothing(self):
		frappe.db.set_single_value(erp_settings.SETTINGS, "cedente_send_enabled", 0)
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		with patch.object(frappe, "enqueue") as enqueue:
			self.deal.status = self.stage
			self.deal.save(ignore_permissions=True)
		self.assertFalse([c for c in enqueue.call_args_list if c.args and c.args[0] == "cred_hub.erp.cedente.send_for_deal"])

	def test_registered_with_enriched_data_and_note_with_the_manager(self):
		fake = FakeSmart()
		outcome = self.send(fake)
		self.assertEqual(outcome["status"], "created")
		payload = fake.sent[0]
		self.assertEqual(payload["idSetor"], 282)  # Serviço de TI, from the CNAE
		self.assertEqual(payload["idGerente"], 1060026)  # default manager: the card owner is not a Smart manager
		self.assertEqual(payload["idRegimeTributario"], "S")
		self.assertEqual(payload["codigoMunicipioIbgeEnderecoPrincipal"], 4205407)
		self.assertEqual(payload["complementoEnderecoPrincipal"], "APT 106")  # never the district
		self.assertEqual(payload["numeroContratoMae"], 4001125)
		for doctype, name in (("CRM Deal", self.deal.name), ("CRM Organization", self.org.name)):
			note = " ".join(self.comments(doctype, name))
			self.assertIn("2513", note)
			self.assertIn("NF SECURITIZADORA SA", note)

	def test_already_registered_is_never_resent(self):
		fake = FakeSmart(existing={"ID_CEDENTE": "1966"})
		self.assertEqual(self.send(fake)["status"], "already")
		self.assertEqual(fake.sent, [])
		self.assertIn("1966", " ".join(self.comments("CRM Deal", self.deal.name)))

	def test_missing_manager_is_noted_and_nothing_is_sent(self):
		frappe.db.set_single_value(erp_settings.SETTINGS, "default_manager", "")
		frappe.clear_document_cache(erp_settings.SETTINGS, erp_settings.SETTINGS)
		fake = FakeSmart()
		outcome = self.send(fake)
		self.assertFalse(outcome["ok"])
		self.assertEqual(fake.sent, [])
		self.assertTrue(self.comments("CRM Deal", self.deal.name))

	def test_stage_must_belong_to_the_funnel(self):
		doc = frappe.get_doc(erp_settings.SETTINGS)
		other = frappe.db.get_value("CRM Deal Status", {"pipeline": ("!=", self.pipeline)}, "name")
		if other:
			doc.cedente_stage = other
			with self.assertRaises(frappe.ValidationError):
				doc.save(ignore_permissions=True)
