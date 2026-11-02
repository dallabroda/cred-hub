"""Importação de títulos por planilha no funil de Cobrança (credtech#315). Roda no bench:
`bench --site <site> run-tests --module cred_hub.tests.test_collection_import`."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from cred_hub.collection import importer
from cred_hub.integrations import SETTINGS, protest
from cred_hub.party.document import format_cpf

CPF_DEBTOR = "52998224725"
CNPJ_DEBTOR = "11222333000181"
CNPJ_CEDENTE_A = "45723174000110"
CNPJ_CEDENTE_B = "33445566000186"
HEADER = "cpf_cnpj_sacado;sacado;cpf_cnpj_cedente;cedente;documento;nosso_numero;tipo_titulo;data_emissao;data_vencimento;valor_face;valor_em_aberto"


def br(days):
	return getdate(add_days(today(), days)).strftime("%d/%m/%Y")


class TestCollectionImport(IntegrationTestCase):
	def setUp(self):
		self.enqueue = patch("frappe.enqueue")
		self.enqueue.start()
		# a importação grava (commit) a cada 50 cards; no teste, tudo volta no rollback
		self.commit = patch.object(frappe.db, "commit")
		self.commit.start()
		settings = frappe.get_single(SETTINGS)
		settings.update({"cenprot_enabled": 1, "collection_pipeline": None, "collection_grouping": importer.BY_PAIR})
		settings.save()
		self.settings = frappe.get_single(SETTINGS)
		self.pipeline = self.settings.collection_pipeline

	def tearDown(self):
		self.enqueue.stop()
		self.commit.stop()
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def sheet(self, *lines, name="carteira-315.csv"):
		content = "\n".join((HEADER, *lines)).encode("utf-8")
		return frappe.get_doc({"doctype": "File", "file_name": name, "content": content, "is_private": 1}).insert().file_url

	def mapping(self, file_url):
		return importer.columns(file_url)["suggested"]

	def import_sheet(self, file_url):
		return importer.run_import(file_url, self.mapping(file_url), frappe.session.user)

	def cards(self):
		return frappe.get_all("CRM Deal", {"pipeline": self.pipeline}, ["name", "organization", "second_organization"])

	def test_headers_are_recognized(self):
		file_url = self.sheet(f"{CNPJ_DEBTOR};Devedora;{CNPJ_CEDENTE_A};Cedente A;NF-1;;DMI;{br(-60)};{br(-10)};1.000,50;1.200,00")
		suggested = self.mapping(file_url)
		self.assertEqual(
			{key: suggested.get(key) for key in ("debtor_document", "cedente_name", "title_number", "due_date", "updated_amount")},
			{"debtor_document": 0, "cedente_name": 3, "title_number": 4, "due_date": 8, "updated_amount": 10},
		)

	def test_preview_then_import_by_pair(self):
		file_url = self.sheet(
			f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-1;NN1;DMI;{br(-60)};{br(-10)};1.000,50;1.200,00",
			f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-2;;Duplicata Mercantil;{br(-50)};{br(-5)};500;",
			f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_B};CEDENTE B LTDA;NF-3;;DSI;{br(-40)};{br(-3)};300;300",
			f"{CPF_DEBTOR};MARIA DA SILVA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-4;;DMI;;{br(-20)};250;260",
			f"123;SEM DOCUMENTO;;;NF-5;;DMI;{br(-30)};{br(-2)};100;100",
		)
		mapping = self.mapping(file_url)
		preview = importer.preview(file_url, mapping)
		self.assertEqual((preview["rows"], preview["valid"], preview["problem_count"]), (5, 4, 1))
		self.assertEqual(preview["problems"][0]["line"], 6)
		# por par: devedora+A, devedora+B e Maria+A
		self.assertEqual((preview["cards_new"], preview["titles_new"]), (3, 4))
		known = sum(bool(importer._existing_org(c)) for c in (CNPJ_DEBTOR, CNPJ_CEDENTE_A, CNPJ_CEDENTE_B))
		self.assertEqual((preview["people_new"], preview["companies_new"]), (1, 3 - known))
		self.assertFalse(self.cards(), "a prévia não grava nada")

		stats = importer.run_import(file_url, mapping, frappe.session.user)
		self.assertEqual((stats["cards_new"], stats["titles_new"], stats["failed"]), (3, 4, 0))
		cards = self.cards()
		self.assertEqual(len(cards), 3)
		# pessoa física vira Contato (o card fica sem organização), empresa vira Organização
		person = next(c for c in cards if not c.organization)
		contact = frappe.get_doc("CRM Deal", person.name).contacts[0].contact
		self.assertEqual(frappe.db.get_value("Contact", contact, "cpf"), format_cpf(CPF_DEBTOR))
		self.assertFalse(frappe.db.exists("CRM Organization", {"cnpj": ("in", (format_cpf(CPF_DEBTOR), CPF_DEBTOR))}))
		deal = frappe.get_doc("CRM Deal", next(c.name for c in cards if c.organization and len(frappe.get_doc("CRM Deal", c.name).titles) == 2))
		rows = {row.title_number: row for row in deal.titles}
		self.assertEqual(rows["NF-1"].species, "DMI")
		self.assertEqual(rows["NF-1"].updated_amount, 1200)
		# "Duplicata Mercantil" não é código de cartório: entra sem espécie (nunca há padrão)
		self.assertFalse(rows["NF-2"].species)
		self.assertEqual(rows["NF-2"].updated_amount, 500)
		self.assertEqual(rows["NF-1"].cedente, deal.second_organization)

	def test_second_import_updates_and_never_duplicates(self):
		line = f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-1;NN1;;{br(-60)};{br(-10)};1000;1100"
		self.import_sheet(self.sheet(line))
		deal = frappe.get_doc("CRM Deal", self.cards()[0].name)
		# o operador escolhe a espécie; um envio já feito fica travado
		deal.titles[0].species = "DMI"
		deal.save()
		stats = self.import_sheet(self.sheet(line.replace(";1100", ";1500"), f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-9;;;{br(-30)};{br(-1)};90;90"))
		self.assertEqual((stats["cards_new"], stats["cards_update"], stats["titles_new"], stats["titles_update"]), (0, 1, 1, 1))
		deal.reload()
		self.assertEqual(len(self.cards()), 1)
		row = next(r for r in deal.titles if r.title_number == "NF-1")
		self.assertEqual((row.updated_amount, row.species), (1500, "DMI"))
		frappe.db.set_value("CRM Deal Title", row.name, {"protest_status": "COLETADO", "sent_at": frappe.utils.now()})
		stats = self.import_sheet(self.sheet(line.replace(";1100", ";9999")))
		self.assertEqual(stats["titles_locked"], 1)
		self.assertEqual(frappe.db.get_value("CRM Deal Title", row.name, "updated_amount"), 1500)

	def test_grouping_by_debtor_keeps_the_cedente_on_each_title(self):
		frappe.db.set_single_value(SETTINGS, "collection_grouping", importer.BY_DEBTOR)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.import_sheet(
			self.sheet(
				f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_A};CEDENTE A LTDA;NF-1;;DMI;{br(-60)};{br(-10)};100;100",
				f"{CNPJ_DEBTOR};DEVEDORA TESTE LTDA;{CNPJ_CEDENTE_B};CEDENTE B LTDA;NF-2;;DMI;{br(-60)};{br(-10)};200;200",
			)
		)
		cards = self.cards()
		self.assertEqual(len(cards), 1)
		self.assertFalse(cards[0].second_organization)
		deal = frappe.get_doc("CRM Deal", cards[0].name)
		cedentes = {frappe.db.get_value("CRM Organization", row.cedente, "cnpj") for row in deal.titles}
		self.assertEqual(cedentes, {"45.723.174/0001-10", "33.445.566/0001-86"})
		# o sacador do protesto é o cedente da própria linha
		self.assertEqual(protest.drawer_of(deal, deal.titles[1])["documento"], CNPJ_CEDENTE_B)

	def test_missing_required_column_stops_before_importing(self):
		file_url = self.sheet(f"{CNPJ_DEBTOR};D;;;NF-1;;DMI;{br(-60)};{br(-10)};100;100")
		mapping = self.mapping(file_url)
		mapping.pop("due_date")
		with self.assertRaises(frappe.ValidationError):
			importer.preview(file_url, mapping)

	def test_only_own_files(self):
		file_url = self.sheet(f"{CNPJ_DEBTOR};D;;;NF-1;;DMI;{br(-60)};{br(-10)};100;100")
		frappe.db.set_value("File", {"file_url": file_url}, "owner", "someone.else@example.com")
		with patch("frappe.get_roles", return_value=["Sales User"]), self.assertRaises(frappe.PermissionError):
			importer.read_table(file_url)
