"""Dossiê de crédito, PDF 1 (credtech#316): todos os campos, nomes em português, sem recomendação, versões novas a
cada geração. Roda no bench: `bench --site <site> run-tests --module cred_hub.tests.test_credit_dossier`.
Os retornos dos provedores são fictícios, no formato das páginas de catálogo e do retorno real da Serasa."""

import json
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from cred_hub.credit_check import api, dossier

FIXTURES = Path(__file__).parent / "fixtures"
SERASA = json.loads((FIXTURES / "serasa_advanced_pj.json").read_text(encoding="utf-8"))
SCR = json.loads((FIXTURES / "directd_scr_bacen.json").read_text(encoding="utf-8"))
CNPJ = "11.222.333/0001-81"
CPF = "529.982.247-25"


def _leaves(node):
	if isinstance(node, dict):
		return sum((_leaves(value) for value in node.values()), 0) if node else 1
	if isinstance(node, list):
		return sum((_leaves(value) for value in node), 0) if node else 1
	return 1


class TestRows(IntegrationTestCase):
	def test_every_field_of_the_return_appears_as_a_row(self):
		for payload in (SERASA, SCR):
			rows = dossier.rows_of(payload)
			values = [row for row in rows if not row.get("group")]
			# cada valor do retorno vira uma linha, nada é omitido (listas e objetos vazios viram "(vazio)")
			self.assertEqual(len(values), _leaves(payload))

	def test_values_are_exactly_as_returned_and_booleans_are_readable(self):
		rows = {}
		for row in dossier.rows_of(SERASA):
			if not row.get("group"):
				rows.setdefault(row["key"], row)  # o mesmo campo aparece em mais de um lugar: vale o primeiro
		self.assertEqual(rows["defaultRate"]["value"], "00291")  # zeros à esquerda ficam
		self.assertEqual(rows["message"]["value"], "PROBABILIDADE DE INADIMPLENCIA:  2,91%")  # sem arrumar
		self.assertEqual(rows["socialCapitalValue"]["value"], "50000.0")
		self.assertEqual(rows["companyUrl"]["value"], frappe._("(empty)"))
		self.assertIn(rows["restrictionSign"]["value"], (frappe._("No"), "No"))

	def test_field_names_are_in_portuguese_and_untranslated_ones_show_the_original(self):
		html = dossier.build_html(
			{
				"organization": "Empresa X",
				"organization_name": "Empresa X",
				"cnpj": CNPJ,
				"partners": [],
				"sections": [
					{
						"provider": "Teste",
						"product": "Produto",
						"subject": "Empresa X",
						"document": CNPJ,
						"when": "2026-11-02",
						"payload": {"documentNumber": "1", "campoNovoDoProvedor": "2"},
						"summary_only": False,
					}
				],
				"failures": [],
				"complete": True,
			},
			"Fulano",
			"02/11/2026",
		)
		self.assertIn("CNPJ", html)
		self.assertNotIn("documentNumber", html)  # traduzido: sem o nome original
		self.assertIn("Campo novo do provedor", html)
		self.assertIn("(campoNovoDoProvedor)", html)  # sem tradução: mostra o original

	def test_provider_text_is_escaped_in_the_html(self):
		html = dossier.build_html(
			{
				"organization": "A <b>B</b>",
				"organization_name": "x",
				"cnpj": CNPJ,
				"partners": [],
				"sections": [
					{
						"provider": "P",
						"product": "Q",
						"subject": "S",
						"document": "D",
						"when": "W",
						"payload": {"nome": "<script>alert(1)</script>"},
						"summary_only": False,
					}
				],
				"failures": ["<img src=x>"],
				"complete": True,
			},
			"Fulano",
			"02/11/2026",
		)
		self.assertNotIn("<script>", html)
		self.assertNotIn("<img", html)
		self.assertIn("&lt;script&gt;", html)


class TestGenerate(IntegrationTestCase):
	def setUp(self):
		self.org = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Exemplo Dossiê Teste", "cnpj": CNPJ}
		).insert()
		self.partner = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Maria Exemplo da Silva", "company_name": self.org.name, "cpf": CPF}
		)
		self.partner.flags.ignore_mandatory = True
		self.partner.insert()
		report = frappe.get_doc(
			{
				"doctype": "Cred Hub Credit Report",
				"organization": self.org.name,
				"cnpj": CNPJ,
				"checked_on": frappe.utils.today(),
				"valid_until": frappe.utils.today(),
				"verdict": "Warning",
				"raw_details_json": json.dumps(
					{
						"failures": [],
						"raw_responses": [
							{"provider": "DirectD", "endpoint": "SCRBacen", "payload": SCR},
							{
								"provider": "DirectD",
								"endpoint": "Score",
								"payload": {"retorno": {"pessoaJuridica": {"score": 740}, "campoQueOResumoDescarta": 1}},
							},
						],
					}
				),
			}
		).insert(ignore_permissions=True)
		self.org.db_set("credit_report", report.name)
		frappe.get_doc(
			{
				"doctype": "Cred Hub Premium Bureau Query",
				"bureau": "serasa",
				"organization": self.org.name,
				"cnpj": CNPJ,
				"document": CNPJ,
				"requested_by": "Administrator",
				"requested_on": frappe.utils.now_datetime(),
				"status": "Concluído",
				"raw_response_json": json.dumps(SERASA),
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Cred Hub Premium Bureau Query",
				"bureau": "scr_bacen",
				"organization": self.org.name,
				"cnpj": CNPJ,
				"subject_type": "Partner",
				"contact": self.partner.name,
				"document": "52998224725",
				"requested_by": "Administrator",
				"requested_on": frappe.utils.now_datetime(),
				"status": "Concluído",
				"raw_response_json": json.dumps(SCR),
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def test_collect_gathers_automatic_returns_and_bureau_queries_for_company_and_partner(self):
		ctx = dossier.collect(self.org.name)
		products = sorted((section["product"], section["subject"]) for section in ctx["sections"])
		self.assertEqual(len(ctx["sections"]), 4)
		self.assertIn(("Score de crédito (QUOD)", "Exemplo Dossiê Teste"), products)
		self.assertIn(("SCR Analítico - Resumo BACEN", "Maria Exemplo da Silva"), products)
		self.assertTrue(ctx["complete"])
		self.assertEqual([p["name"] for p in ctx["partners"]], ["Maria Exemplo da Silva"])
		# o retorno inteiro do score (inclusive o campo que o resumo descarta) está lá
		score = next(s for s in ctx["sections"] if s["product"] == "Score de crédito (QUOD)")
		self.assertEqual(score["payload"]["retorno"]["campoQueOResumoDescarta"], 1)

	def test_old_report_without_the_complete_return_says_it_is_only_a_summary(self):
		report = frappe.get_doc("Cred Hub Credit Report", self.org.credit_report)
		report.db_set("raw_details_json", json.dumps({"directd_score": {"score": 740}}))
		ctx = dossier.collect(self.org.name)
		self.assertFalse(ctx["complete"])
		self.assertTrue(any(section["summary_only"] for section in ctx["sections"]))

	def test_generate_creates_a_pdf_and_a_json_attached_to_the_organization(self):
		result = api.generate_credit_dossier(self.org.name)
		self.assertEqual(result["queries"], 4)

		pdf = frappe.get_doc("File", {"file_url": result["pdf_url"]})
		self.assertTrue(pdf.get_content().startswith(b"%PDF"))
		self.assertEqual((pdf.attached_to_doctype, pdf.attached_to_name), ("CRM Organization", self.org.name))

		bundle = json.loads(frappe.get_doc("File", {"file_url": result["json_url"]}).get_content())
		self.assertEqual(len(bundle["queries"]), 4)
		serasa = next(q for q in bundle["queries"] if q["product"].startswith("Serasa"))
		self.assertEqual(serasa["payload"], SERASA)  # o JSON original, sem mudar

		comment = frappe.get_last_doc(
			"Comment", filters={"reference_doctype": "CRM Organization", "reference_name": self.org.name}
		)
		self.assertIn("4", comment.content)

	def test_each_generation_creates_new_files_and_listing_is_newest_first(self):
		first = api.generate_credit_dossier(self.org.name)
		frappe.utils.now_datetime()  # os nomes levam segundos: força nome diferente
		import time

		time.sleep(1.1)
		second = api.generate_credit_dossier(self.org.name)
		self.assertNotEqual(first["pdf_name"], second["pdf_name"])
		files = api.list_credit_dossiers(self.org.name)
		self.assertEqual(len(files), 4)  # 2 PDFs + 2 JSONs, nada sobrescrito
		self.assertEqual(files[0]["file_name"][:14], "Dossie-Retorno")

	def test_without_any_query_there_is_nothing_to_generate(self):
		empty = frappe.get_doc(
			{"doctype": "CRM Organization", "organization_name": "Sem Consulta Teste", "cnpj": "22.333.444/0001-81"}
		).insert()
		with self.assertRaises(frappe.ValidationError):
			api.generate_credit_dossier(empty.name)
