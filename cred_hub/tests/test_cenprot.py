"""CENPROT e o funil de Cobrança (credtech#290, módulo 2). O WebService SOAP é simulado com as respostas do manual
v2.2 (`api documentation/cenprot/`); nada sai do servidor. Roda no bench: `bench --site <site> run-tests --module
cred_hub.tests.test_cenprot`."""

from unittest.mock import patch
from xml.etree import ElementTree as ET

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from cred_hub.integrations import SETTINGS, bureau_log, cenprot, protest
from cred_hub.integrations.cenprot import CenprotClient, CenprotError

NS = "https://wspub.cenprotnacional.org.br/ieptb-cenprot-empresas-arquivos-hml/services"
AUTH_OK = f"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<ns2:AutenticarResponse xmlns:ns2="{NS}"><credenciais><resposta><codigo>WS_SUC_101</codigo>
<mensagem>Autenticado com Sucesso</mensagem><status>true</status></resposta>
<ns2:token>tok-1</ns2:token></credenciais></ns2:AutenticarResponse></soap:Body></soap:Envelope>"""
AUTH_ERROR = f"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<ns2:AutenticarResponse xmlns:ns2="{NS}"><credenciais><resposta><codigo>WS_ERR_101</codigo>
<mensagem>Erro ao Autenticar Usuario: User ou Senha Inválido!</mensagem><status>false</status></resposta>
<token></token></credenciais></ns2:AutenticarResponse></soap:Body></soap:Envelope>"""


def answer(action, code, message="", extra=""):
	return (
		'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
		f'<ns2:{action}Response xmlns:ns2="{NS}"><retorno><resposta><codigo>{code}</codigo>'
		f"<mensagem>{message}</mensagem><status>{'true' if code.startswith('WS_SUC') else 'false'}</status></resposta>"
		f"{extra}</retorno></ns2:{action}Response></soap:Body></soap:Envelope>"
	)


def consult_answer(*events, code=None, message=""):
	occurrences = "".join(
		f"<ocorrencia><dataHora>{at}</dataHora><status>{status}</status><mensagem>{text}</mensagem></ocorrencia>"
		for at, status, text in events
	)
	answer_tag = f"<resposta><codigo>{code}</codigo><mensagem>{message}</mensagem><status>true</status></resposta>" if code else ""
	return (
		'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
		f'<ns2:ConsultarTituloResponse xmlns:ns2="{NS}"><titulo><devedor><documento>11222333000181</documento></devedor>'
		"<cartorio><data>22/05/2020</data><comarca>4210100</comarca><cartorio>02</cartorio>"
		f"<protocolo>23901494-4</protocolo></cartorio>{occurrences}{answer_tag}</titulo>"
		"</ns2:ConsultarTituloResponse></soap:Body></soap:Envelope>"
	)


class FakeResponse:
	def __init__(self, text, status=200):
		self.content = text.encode("utf-8")
		self.status_code = status


class FakeCenprot:
	"""Responde por SOAPAction e guarda os envelopes enviados."""

	def __init__(self, **answers):
		self.answers = {"Autenticar": [AUTH_OK], **{k: list(v) for k, v in answers.items()}}
		self.sent = []

	def post(self, url, data=None, headers=None, timeout=None):
		action = headers["SOAPAction"]
		self.sent.append((action, data.decode("ascii")))
		queue = self.answers[action]
		return FakeResponse(queue.pop(0) if len(queue) > 1 else queue[0])


TITLE = {
	"cedente": {
		"codigo": "999", "nome": "Credora Teste Ação Ltda", "documento": "11.222.333/0001-81",
		"endereco": "Avenida Paulista", "numero": "1000", "complemento": "Andar 10", "bairro": "Bela Vista",
		"municipio": "São Paulo", "uf": "SP", "cep": "01310-100",
	},
	"devedores": [
		{
			"nome": "Empresa Devedora Exemplo Com Nome Muito Comprido Ltda", "documento": "12345678000199",
			"endereco": "Rua das Indústrias", "numero": "500", "complemento": "Galpão 2", "bairro": "Distrito Industrial",
			"municipio": "Campinas", "uf": "sp", "cep": "13050000", "fone": "(19) 3210-4455", "email": "fin@x.com",
		}
	],
	"divida": {
		"especie": "DMI", "numero": "NF10492", "nossoNumero": "202610001234", "valor": 15420.5, "saldo": 15420.5,
		"aceite": "N", "declaracaoPortador": "D", "emissao": "15/08/2026", "vencimento": "25/09/2026",
	},
}


class TestCenprotClient(IntegrationTestCase):
	def test_send_envelope_follows_the_manual(self):
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		client = CenprotClient("user", "pass", "Homologation", session=session)
		result = client.send_title(TITLE)
		self.assertTrue(result["ok"])
		self.assertFalse(result["duplicate"])
		action, envelope = session.sent[-1]
		self.assertEqual(action, "EnviarTitulo")
		root = ET.fromstring(envelope)
		body = root[0][0]
		self.assertEqual(body.tag, f"{{{NS}}}EnviarTitulo")
		# token fora do <titulo> (formato aceito em produção, README)
		self.assertEqual([child.tag for child in body], ["token", "titulo"])
		self.assertEqual(body[0].text, "tok-1")
		title = body[1]
		self.assertEqual(title.findtext("cedente/municipio"), "SAO PAULO")
		self.assertEqual(title.findtext("cedente/documentoTipo"), "2")
		self.assertEqual(title.findtext("cedente/cep"), "01310100")
		debtor = title.find("devedor")
		self.assertEqual(len(debtor.findtext("nome")), 45)
		self.assertEqual(debtor.findtext("endereco"), "RUA DAS INDUSTRIAS")
		self.assertEqual(debtor.findtext("uf"), "SP")
		self.assertEqual(debtor.findtext("fone"), "1932104455")
		self.assertIsNone(debtor.find("principal"))
		self.assertEqual(title.findtext("divida/valor"), "15420.50")
		self.assertEqual(title.findtext("divida/finsFalimentares"), "N")
		self.assertEqual(title.findtext("alteracao"), "N")

	def test_duplicate_title_and_invalid_token_renews_once(self):
		session = FakeCenprot(
			Autenticar=[AUTH_OK, AUTH_OK],
			EnviarTitulo=[answer("EnviarTitulo", "WS_ERR_002", "Token inválido"), answer("EnviarTitulo", "WS_ERR_300", "Título já estava cadastrado!")],
		)
		result = CenprotClient("user", "pass", session=session, token="velho").send_title(TITLE)
		self.assertTrue(result["duplicate"])
		self.assertEqual([action for action, _ in session.sent], ["EnviarTitulo", "Autenticar", "EnviarTitulo"])

	def test_login_error_message(self):
		client = CenprotClient("user", "errada", session=FakeCenprot(Autenticar=[AUTH_ERROR]))
		with self.assertRaises(CenprotError) as error:
			client.authenticate()
		self.assertIn("Senha", str(error.exception))
		self.assertEqual(error.exception.code, "WS_ERR_101")

	def test_consult_reads_latest_status_like_the_manual(self):
		root = ET.fromstring(
			consult_answer(
				("22/05/2020 15:20:00", "CONFIRMADO", "01/23900594-4"),
				("21/05/2020 13:01:15", "GERADO", "IMPORTADO POR ARQUIVO"),
				("01/06/2020 15:23:00", "PROTESTADO", "02/23900594-4"),
				code="PROTESTADO",
				message="02/23900594-4 CRA: Protestado",
			)
		)
		found = cenprot.parse_consult(root)
		self.assertEqual(found["status"], "PROTESTADO")
		self.assertEqual(found["protocol"], "23901494-4")
		self.assertEqual(found["registry"], "4210100 02")
		self.assertEqual([e["status"] for e in found["events"]], ["GERADO", "CONFIRMADO", "PROTESTADO"])
		# sem <resposta>, vale a última ocorrência
		found = cenprot.parse_consult(ET.fromstring(consult_answer(("26/09/2026 09:15:00", "confirmado", "x"))))
		self.assertEqual(found["status"], "CONFIRMADO")

	def test_operation_for_each_status(self):
		self.assertEqual(cenprot.OPERATION_FOR_STATUS["COLETADO"], "REMOCAO")
		self.assertEqual(cenprot.OPERATION_FOR_STATUS["CONFIRMADO"], "DESISTENCIA")
		self.assertEqual(cenprot.OPERATION_FOR_STATUS["PROTESTADO"], "CANCELAMENTO")
		self.assertNotIn("PAGO", cenprot.OPERATION_FOR_STATUS)


class TestCollectionPipeline(IntegrationTestCase):
	def setUp(self):
		protest.forget_token()
		self.settings = frappe.get_single(SETTINGS)
		self.settings.update(
			{
				"assertiva_enabled": 0,
				"cenprot_enabled": 1,
				"cenprot_environment": "Homologation",
				"cenprot_username": "user",
				"cenprot_password": "pass",
				"creditor_name": "Credora Teste Ltda",
				"creditor_document": "11.444.777/0001-61",
				"creditor_address": "Avenida Paulista",
				"creditor_number": "1000",
				"creditor_district": "Bela Vista",
				"creditor_city": "São Paulo",
				"creditor_state": "sp",
				"creditor_zip": "01310-100",
				"default_species": "DMI",
				"auto_send_on_stage": 1,
				"collection_pipeline": None,
			}
		)
		self.settings.save()
		self.org = frappe.get_doc(
			{
				"doctype": "CRM Organization",
				"organization_name": "Devedora Protesto Teste",
				"cnpj": "11.222.333/0001-81",
				"neighborhood": "Distrito Industrial",
				"city": "Campinas",
				"state": "SP",
				"registry_email": "fin@devedora.com.br",
			}
		).insert()
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": "Devedora Protesto Teste (Receita)",
				"address_type": "Office",
				"address_line1": "Rua das Indústrias, 500",
				"address_line2": "Galpão 2, Distrito Industrial",
				"city": "Campinas",
				"state": "SP",
				"pincode": "13050-000",
				"country": "Brazil",
				"links": [{"link_doctype": "CRM Organization", "link_name": self.org.name}],
			}
		).insert()
		frappe.db.set_value("CRM Organization", self.org.name, "address", address.name)

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		protest.forget_token()

	def stage(self, setting):
		return self.settings.get(setting)

	def deal(self, **fields):
		first = frappe.get_all(
			"CRM Deal Status", {"pipeline": self.settings.collection_pipeline}, pluck="name", order_by="position asc"
		)[0]
		values = {
			"doctype": "CRM Deal",
			"pipeline": self.settings.collection_pipeline,
			"status": first,
			"first_name": "Devedor",
			"organization": self.org.name,
			"deal_owner": "Administrator",
			"protest_issue_date": add_days(today(), -60),
			"protest_due_date": add_days(today(), -10),
			"protest_amount": 1500.25,
			**fields,
		}
		return frappe.get_doc(values).insert()

	def test_enabling_creates_the_collection_pipeline(self):
		pipeline = frappe.get_doc("CRM Pipeline", self.settings.collection_pipeline)
		self.assertEqual(pipeline.pipeline_name, "Debt Collection")
		stages = frappe.get_all(
			"CRM Deal Status", {"pipeline": pipeline.name}, ["name", "deal_status", "type"], order_by="position asc"
		)
		self.assertEqual(len(stages), 8)
		self.assertEqual(stages[2].name, self.stage("stage_send"))
		self.assertEqual(stages[5].deal_status, "Protested")
		self.assertEqual(stages[6].type, "Won")
		self.assertIn("protest_due_date", [row.fieldname for row in pipeline.fields])
		self.assertEqual(self.settings.creditor_state, "SP")
		self.assertEqual(self.settings.creditor_document, "11444777000161")

	def test_title_needs_overdue_due_date_and_debtor_address(self):
		# a CENPROT recusa título que ainda não venceu
		self.assertRaises(protest.TitleIncomplete, protest.build_title, self.deal(protest_due_date=today()), self.settings)
		deal = self.deal()
		protest.build_title(deal, self.settings)
		frappe.db.set_value("Address", frappe.db.get_value("CRM Organization", self.org.name, "address"), "pincode", "")
		self.assertRaises(protest.TitleIncomplete, protest.build_title, deal, self.settings)

	def test_title_from_the_card(self):
		deal = self.deal()
		title, keys = protest.build_title(deal, self.settings)
		debtor = title["devedores"][0]
		self.assertEqual(
			(debtor["documento"], debtor["endereco"], debtor["numero"], debtor["complemento"], debtor["bairro"], debtor["cep"]),
			("11222333000181", "Rua das Indústrias", "500", "Galpão 2", "Distrito Industrial", "13050000"),
		)
		code = "".join(ch for ch in deal.name if ch.isdigit())
		self.assertEqual(keys["protest_title_number"], code[-11:])
		self.assertEqual(keys["protest_declaration"], "D")
		self.assertEqual(title["divida"]["valor"], 1500.25)

	def test_send_then_daily_check_moves_the_card_and_notifies(self):
		deal = self.deal()
		session = FakeCenprot(
			EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")],
			ConsultarTitulo=[
				consult_answer(("26/09/2026 09:15:00", "CONFIRMADO", "01/23900594-4")),
				consult_answer(("26/09/2026 09:15:00", "CONFIRMADO", "x"), ("03/10/2026 17:00:10", "PROTESTADO", "y"), code="PROTESTADO", message="CRA: Protestado"),
			],
		)
		# sync_all grava (commit) a cada card; no teste, tudo volta no rollback
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session), patch.object(frappe.db, "commit"):
			result = protest.send(deal.name)
			self.assertTrue(result["ok"], result)
			deal.reload()
			self.assertEqual((deal.protest_status, deal.status), ("COLETADO", self.stage("stage_send")))
			self.assertEqual(deal.protest_debtor_document, "11222333000181")
			self.assertTrue(deal.protest_sent_at)
			protest.sync_all()
			deal.reload()
			self.assertEqual((deal.protest_status, deal.status), ("CONFIRMADO", self.stage("stage_at_registry")))
			self.assertEqual(deal.protest_registry_protocol, "23901494-4")
			protest.sync_all()
			deal.reload()
		self.assertEqual((deal.protest_status, deal.status), ("PROTESTADO", self.stage("stage_protested")))
		self.assertEqual(deal.protest_message, "CRA: Protestado")
		# token guardado: um só Autenticar para as três chamadas
		self.assertEqual([a for a, _ in session.sent].count("Autenticar"), 1)
		consult = next(body for action, body in session.sent if action == "ConsultarTitulo")
		self.assertIn(f"<nossoNumero>{deal.protest_our_number}</nossoNumero>", consult)
		notes = frappe.get_all("CRM Notification", {"reference_name": deal.name}, pluck="notification_text")
		self.assertEqual(len(notes), 2)
		self.assertIn("PROTESTADO", notes[0] + notes[1])
		logs = frappe.get_all(bureau_log.QUERY, {"reference_name": deal.name}, ["operation", "status"])
		self.assertEqual(len(logs), 3)
		self.assertTrue(all(log.status == "Success" for log in logs))

	def test_refused_title_is_recorded_without_error_page(self):
		deal = self.deal()
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_ERR_102", "CEP divergente da comarca")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.send(deal.name)
		self.assertFalse(result["ok"])
		self.assertIn("WS_ERR_102", result["message"])
		deal.reload()
		self.assertFalse(deal.protest_sent_at)
		self.assertIn("CEP divergente", deal.protest_message)
		log = frappe.get_all(bureau_log.QUERY, {"reference_name": deal.name}, ["status", "result_code"])
		self.assertEqual((log[0].status, log[0].result_code), ("Error", "WS_ERR_102"))

	def test_entering_the_send_stage_queues_the_title(self):
		def protest_jobs(enqueue):
			return [c for c in enqueue.call_args_list if c.args and c.args[0].startswith("cred_hub.integrations.protest")]

		deal = self.deal()
		with patch("frappe.enqueue") as enqueue:
			deal.status = self.stage("stage_send")
			deal.save()
		jobs = protest_jobs(enqueue)
		self.assertEqual([(c.args[0], c.kwargs["deal"]) for c in jobs], [("cred_hub.integrations.protest.send_in_background", deal.name)])
		frappe.db.set_single_value(SETTINGS, "auto_send_on_stage", 0)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		deal.reload()
		deal.status = frappe.get_all("CRM Deal Status", {"pipeline": deal.pipeline}, pluck="name", order_by="position asc")[0]
		deal.save()
		with patch("frappe.enqueue") as enqueue:
			deal.status = self.stage("stage_send")
			deal.save()
		self.assertFalse(protest_jobs(enqueue))

	def test_withdraw_uses_the_operation_of_the_status(self):
		deal = self.deal()
		frappe.db.set_value(
			"CRM Deal",
			deal.name,
			{"protest_status": "PROTESTADO", "protest_sent_at": frappe.utils.now(), "protest_debtor_document": "11222333000181"},
		)
		session = FakeCenprot(OperacaoTitulo=[answer("OperacaoTitulo", "WS_SUC_102", "Aceito Para Operação")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.withdraw(deal.name, "Acordo")
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["operation"], "CANCELAMENTO")
		body = ET.fromstring(session.sent[-1][1])[0][0]
		self.assertEqual(body.findtext("titulo/operacao"), "CANCELAMENTO")
		self.assertEqual(body.findtext("titulo/autoriza"), "S")
		frappe.db.set_value("CRM Deal", deal.name, "protest_status", "PAGO")
		self.assertFalse(protest.withdraw(deal.name)["ok"])
