"""CENPROT e o funil de Cobrança (credtech#290, módulo 2). O WebService SOAP é simulado com as respostas do manual
v2.2 (`api documentation/cenprot/`); nada sai do servidor. Roda no bench: `bench --site <site> run-tests --module
cred_hub.tests.test_cenprot`."""

import base64
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

	def test_drawer_and_document_follow_the_manual(self):
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		drawer = {**TITLE["cedente"], "codigo": "777", "nome": "Cedente Emitente Ltda", "documento": "45723174000110"}
		document = {"extensao": "pdf", "base64": "JVBERi0="}
		title = {**TITLE, "sacador": drawer, "divida": {**TITLE["divida"], "documento": document}}
		CenprotClient("user", "pass", session=session).send_title(title)
		body = ET.fromstring(session.sent[-1][1])[0][0][1]
		tags = [child.tag for child in body]
		# ordem do manual: cedente, sacador, devedor, divida
		self.assertEqual(tags.index("sacador"), tags.index("cedente") + 1)
		self.assertLess(tags.index("sacador"), tags.index("devedor"))
		self.assertEqual(body.findtext("sacador/documento"), "45723174000110")
		self.assertIsNone(body.find("sacador/codigo"))
		self.assertEqual(body.findtext("divida/documento/extensao"), "pdf")
		self.assertEqual(body.findtext("divida/documento/documentoBase64"), "JVBERi0=")
		debt = [child.tag for child in body.find("divida")]
		self.assertEqual(debt.index("documento"), debt.index("vencimento") + 1)

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

	def pick(self, setting, label):
		"""A client may still choose a stage for the optional automations (send, agreement...)."""
		name = frappe.db.get_value(
			"CRM Deal Status", {"pipeline": self.settings.collection_pipeline, "deal_status": label}, "name"
		)
		frappe.db.set_single_value(SETTINGS, setting, name)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.settings = frappe.get_single(SETTINGS)
		return name

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
			"protest_species": "DMI",
			**fields,
		}
		return frappe.get_doc(values).insert()

	def test_enabling_creates_the_collection_pipeline(self):
		pipeline = frappe.get_doc("CRM Pipeline", self.settings.collection_pipeline)
		self.assertEqual(pipeline.pipeline_name, "Debt Collection")
		stages = frappe.get_all(
			"CRM Deal Status", {"pipeline": pipeline.name}, ["name", "deal_status", "type"], order_by="position asc"
		)
		# only the 6 approved stages; Won and Lost are the card status, never stages
		self.assertEqual(
			[stage.deal_status for stage in stages],
			["Backlog", "In negotiation", "Registry Alert", "In the Registry", "Protested", "Legal / Lawsuit"],
		)
		self.assertEqual(stages[0].type, "Open")
		self.assertFalse([stage for stage in stages if stage.type in ("Won", "Lost")])
		self.assertEqual(self.stage("stage_at_registry"), stages[3].name)
		self.assertEqual(self.stage("stage_protested"), stages[4].name)
		for setting in ("stage_send", "stage_notified", "stage_paid", "stage_agreement"):
			self.assertFalse(self.stage(setting), setting)
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

	def test_title_species_has_no_default(self):
		# dono (02/11/2026): sem espécie não há protesto, e o sistema nunca escolhe uma
		with self.assertRaises(protest.TitleIncomplete) as error:
			protest.build_title(self.deal(protest_species=""), self.settings)
		self.assertIn("no default", str(error.exception))

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
		first_stage = deal.status  # no send stage in the new template: the card waits where it is
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
			self.assertEqual((deal.protest_status, deal.status), ("COLETADO", first_stage))
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

		self.pick("stage_send", "Registry Alert")
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

	def test_agreement_stage_withdraws_only_when_the_client_turned_it_on(self):
		self.pick("stage_agreement", "Legal / Lawsuit")
		deal = self.deal()
		frappe.db.set_value("CRM Deal", deal.name, {"protest_status": "CONFIRMADO", "protest_sent_at": frappe.utils.now()})
		deal.reload()

		def jobs(enqueue):
			return [c.args[0] for c in enqueue.call_args_list if c.args and "protest" in c.args[0]]

		with patch("frappe.enqueue") as enqueue:
			deal.status = self.stage("stage_agreement")
			deal.save()
		self.assertEqual(jobs(enqueue), [])

		frappe.db.set_single_value(SETTINGS, "auto_withdraw_on_stage", 1)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		deal2 = self.deal()
		frappe.db.set_value("CRM Deal", deal2.name, {"protest_status": "CONFIRMADO", "protest_sent_at": frappe.utils.now()})
		deal2.reload()
		with patch("frappe.enqueue") as enqueue:
			deal2.status = self.stage("stage_agreement")
			deal2.save()
		self.assertEqual(jobs(enqueue), ["cred_hub.integrations.protest.withdraw_in_background"])

	def test_paid_automations_start_off(self):
		meta = frappe.get_meta(SETTINGS)
		for field in ("assertiva_enabled", "assertiva_partner_lookup", "cenprot_enabled", "auto_send_on_stage", "auto_withdraw_on_stage"):
			self.assertEqual(str(meta.get_field(field).default or "0"), "0", field)


class TestCardTitles(IntegrationTestCase):
	"""Aba Títulos do card (CRM Deal Title, credtech#315): cada linha é um título na CENPROT. Usa o mesmo
	preparo do funil de Cobrança, sem repetir os testes dele."""

	tearDown = TestCollectionPipeline.tearDown
	deal = TestCollectionPipeline.deal
	stage = TestCollectionPipeline.stage

	def setUp(self):
		if not frappe.db.exists("DocType", "CRM Deal Title"):
			self.skipTest("crm sem a tabela CRM Deal Title")
		TestCollectionPipeline.setUp(self)
		self.cedente = frappe.get_doc(
			{
				"doctype": "CRM Organization",
				"organization_name": "Cedente Emitente Teste",
				"cnpj": "45.723.174/0001-10",
				"neighborhood": "Centro",
				"city": "Sorocaba",
				"state": "SP",
			}
		).insert()
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": "Cedente Emitente Teste",
				"address_type": "Office",
				"address_line1": "Rua do Comércio, 10",
				"city": "Sorocaba",
				"state": "SP",
				"pincode": "18010-000",
				"country": "Brazil",
				"links": [{"link_doctype": "CRM Organization", "link_name": self.cedente.name}],
			}
		).insert()
		frappe.db.set_value("CRM Organization", self.cedente.name, "address", address.name)

	def card(self, **row):
		values = {
			"title_number": "NF-1",
			"species": "DMI",
			"issue_date": add_days(today(), -60),
			"due_date": add_days(today(), -10),
			"original_amount": 1000,
			"updated_amount": 1200,
			"protest_status": "Pendente",
			**row,
		}
		return self.deal(second_organization=self.cedente.name, titles=[values])

	def test_paid_title_is_not_sent_and_a_partly_paid_one_goes_with_its_balance(self):
		deal = self.card()
		row = deal.titles[0]
		# 500 of the 1200 paid: the registry gets what is still owed
		frappe.db.set_value("CRM Deal Title", row.name, {"updated_amount": 700, "paid_amount": 500})
		deal.reload()
		self.assertEqual(protest._row_keys(deal, deal.titles[0])["balance"], 700)
		self.assertFalse(
			[p for p in protest.preview_row(deal.name, row.name)["problems"] if "already paid" in p]
		)
		# everything paid: the balance stays 0 (it must not fall back to the original amount) and the send is blocked
		frappe.db.set_value("CRM Deal Title", row.name, {"updated_amount": 0, "paid_amount": 1200})
		deal.reload()
		self.assertEqual(protest._row_keys(deal, deal.titles[0])["balance"], 0)
		problems = protest.preview_row(deal.name, row.name)["problems"]
		self.assertTrue(any("already paid" in p for p in problems), problems)
		with patch("cred_hub.integrations.cenprot.requests.Session") as session:
			with self.assertRaisesRegex(frappe.ValidationError, "already paid"):
				protest.send_row(deal.name, row.name)
		session.assert_not_called()

	def test_imported_pending_title_is_sent_with_the_cedente_as_drawer(self):
		deal = self.card()
		row = deal.titles[0]
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.send_row(deal.name, row.name)
		self.assertTrue(result["ok"], result)
		saved = frappe.db.get_value(
			"CRM Deal Title", row.name, ["protest_status", "our_number", "sent_at"], as_dict=True
		)
		self.assertEqual(saved.protest_status, "COLETADO")
		self.assertTrue(saved.sent_at)
		# sem nosso número: gerado com os dígitos do card + a posição da linha, e gravado
		self.assertEqual(saved.our_number, "".join(c for c in deal.name if c.isdigit())[-12:] + "001")
		body = ET.fromstring(session.sent[-1][1])[0][0][1]
		self.assertEqual(body.findtext("sacador/documento"), "45723174000110")
		self.assertEqual(body.findtext("devedor/documento"), "11222333000181")
		self.assertEqual(body.findtext("divida/saldo"), "1200.00")
		self.assertEqual(body.findtext("divida/declaracaoPortador"), "D")
		# já está na CENPROT: não sai de novo
		deal.reload()
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			self.assertFalse(protest.send_row(deal.name, row.name)["ok"])

	def test_row_needs_species_number_and_issue_date(self):
		deal = self.card(species="", title_number="S/N", issue_date=None)
		with self.assertRaises(protest.TitleIncomplete) as error:
			protest.build_title_from_row(deal, deal.titles[0], self.settings)
		message = str(error.exception)
		self.assertIn("no default", message)
		self.assertIn("title number", message)
		self.assertIn("issue date", message)

	def test_species_with_document_sends_the_attachment(self):
		deal = self.card(species="CCB")
		with self.assertRaises(protest.TitleIncomplete):
			protest.build_title_from_row(deal, deal.titles[0], self.settings)
		# PNG 1x1 válido (o Frappe confere o conteúdo de PDF ao gravar)
		png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "ccb-teste-315.png", "content": png, "is_private": 1}
		).insert()
		frappe.db.set_value("CRM Deal Title", deal.titles[0].name, "attachment", file.file_url)
		deal.reload()
		title, _keys = protest.build_title_from_row(deal, deal.titles[0], self.settings)
		self.assertEqual(title["divida"]["documento"]["extensao"], "png")
		self.assertEqual(title["divida"]["declaracaoPortador"], "I")

	def test_cancel_before_the_registry_removes_it_at_cenprot(self):
		deal = self.card(our_number="NN-1")
		row = deal.titles[0]
		frappe.db.set_value("CRM Deal Title", row.name, {"protest_status": "COLETADO", "sent_at": frappe.utils.now()})
		session = FakeCenprot(OperacaoTitulo=[answer("OperacaoTitulo", "WS_SUC_103", "Título Removido com Sucesso")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.withdraw_row(deal.name, row.name, "Cliente pagou")
		self.assertTrue(result["ok"], result)
		body = ET.fromstring(session.sent[-1][1])[0][0]
		self.assertEqual(body.findtext("titulo/operacao"), "REMOCAO")
		self.assertEqual(body.findtext("titulo/divida/nossoNumero"), "NN-1")
		saved = frappe.db.get_value("CRM Deal Title", row.name, ["protest_status", "sent_at"], as_dict=True)
		self.assertEqual((saved.protest_status or "", saved.sent_at), ("", None))

	def test_daily_sync_updates_each_title_and_notifies(self):
		deal = self.card(our_number="NN-2")
		row = deal.titles[0]
		frappe.db.set_value("CRM Deal Title", row.name, {"protest_status": "COLETADO", "sent_at": frappe.utils.now()})
		session = FakeCenprot(
			ConsultarTitulo=[consult_answer(("26/09/2026 09:15:00", "CONFIRMADO", "01/23900594-4"))]
		)
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session), patch.object(
			frappe.db, "commit"
		):
			protest.sync_all()
		saved = frappe.db.get_value(
			"CRM Deal Title", row.name, ["protest_status", "protest_protocol"], as_dict=True
		)
		self.assertEqual((saved.protest_status, saved.protest_protocol), ("CONFIRMADO", "23901494-4"))
		consult = next(body for action, body in session.sent if action == "ConsultarTitulo")
		self.assertIn("<nossoNumero>NN-2</nossoNumero>", consult)
		notes = frappe.get_all("CRM Notification", {"reference_name": deal.name}, pluck="notification_text")
		self.assertTrue(any("CONFIRMADO" in note for note in notes))

	# Codevedores, avisos e erros (credtech#315)

	def partner(self, first_name, cpf=None, address=True):
		contact = frappe.get_doc(
			{"doctype": "Contact", "first_name": first_name, "company_name": self.org.name, "cpf": cpf}
		).insert()
		if address:
			frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": first_name,
					"address_type": "Personal",
					"address_line1": "Rua das Flores, 12",
					"address_line2": "Centro",
					"city": "Campinas",
					"state": "SP",
					"pincode": "13010-000",
					"country": "Brazil",
					"links": [{"link_doctype": "Contact", "link_name": contact.name}],
				}
			).insert()
		return contact

	def sent_devedores(self, session):
		body = ET.fromstring(next(b for a, b in session.sent if a == "EnviarTitulo"))[0][0][1]
		return body.findall("devedor")

	def test_every_contact_of_the_company_is_a_candidate_with_what_is_missing(self):
		complete = self.partner("Socio Completo", "529.982.247-25")
		no_cpf = self.partner("Socio Sem Cpf")
		no_address = self.partner("Socio Sem Endereco", "111.444.777-35", address=False)
		deal = self.card()
		preview = protest.preview_row(deal.name, deal.titles[0].name)
		by_name = {c["name"]: c for c in preview["codebtors"]}
		self.assertEqual(by_name["Socio Completo"]["missing"], [])
		self.assertEqual(by_name["Socio Completo"]["address_from"], "contact")
		self.assertIn("valid CPF", by_name["Socio Sem Cpf"]["missing"])
		# sem endereço próprio, o sócio vai com o da empresa (prática da produção): nada falta além do CPF
		self.assertEqual(by_name["Socio Sem Endereco"]["missing"], [])
		self.assertEqual(by_name["Socio Sem Endereco"]["address_from"], "company")
		self.assertEqual(by_name["Socio Completo"]["cpf"], "529.982.247-25")
		self.assertEqual({complete.name, no_cpf.name, no_address.name}, {c["contact"] for c in preview["codebtors"]})
		# a prévia não envia nada e o título em si está pronto
		self.assertEqual(preview["problems"], [])

	def test_codebtors_only_go_when_the_operator_marks_them(self):
		partner = self.partner("Socio Completo", "529.982.247-25")
		deal = self.card()
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			self.assertTrue(protest.send_row(deal.name, deal.titles[0].name)["ok"])
		self.assertEqual(len(self.sent_devedores(session)), 1)

		deal = self.card(title_number="NF-2", our_number="NN-77")
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.send_row(deal.name, deal.titles[0].name, [partner.name])
		self.assertTrue(result["ok"], result)
		devedores = self.sent_devedores(session)
		self.assertEqual([d.findtext("documento") for d in devedores], ["11222333000181", "52998224725"])
		self.assertEqual([d.findtext("documentoTipo") for d in devedores], ["2", "1"])
		self.assertEqual([d.findtext("principal") for d in devedores], ["S", "N"])
		self.assertEqual(devedores[1].findtext("cep"), "13010000")
		notes = frappe.get_all("Comment", {"reference_name": deal.name, "comment_type": "Comment"}, pluck="content")
		self.assertTrue(any("co-debtor" in note.lower() and "Socio Completo" in note for note in notes))

	def test_codebtor_without_own_address_goes_with_the_company_address(self):
		partner = self.partner("Socio Sem Endereco", "111.444.777-35", address=False)
		deal = self.card()
		session = FakeCenprot(EnviarTitulo=[answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso")])
		with patch("cred_hub.integrations.cenprot.requests.Session", return_value=session):
			result = protest.send_row(deal.name, deal.titles[0].name, [partner.name])
		self.assertTrue(result["ok"], result)
		devedores = self.sent_devedores(session)
		self.assertEqual([d.findtext("documento") for d in devedores], ["11222333000181", "11144477735"])
		self.assertEqual(devedores[1].findtext("cep"), devedores[0].findtext("cep"))
		self.assertEqual(devedores[1].findtext("endereco"), devedores[0].findtext("endereco"))

	def test_a_title_takes_at_most_eight_codebtors(self):
		def make_cpf(base):
			digits = [int(char) for char in base]
			for size in (9, 10):
				total = sum(digit * (size + 1 - index) for index, digit in enumerate(digits[:size]))
				digits.append((total * 10 % 11) % 10)
			return "".join(str(digit) for digit in digits)

		names = [
			self.partner(f"Socio Teste {index}", make_cpf(f"10000000{index}"), address=False).name
			for index in range(9)
		]
		deal = self.card()
		preview = protest.preview_row(deal.name, deal.titles[0].name, names)
		self.assertTrue(any("up to 8 co-debtors" in problem for problem in preview["problems"]), preview["problems"])
		preview = protest.preview_row(deal.name, deal.titles[0].name, names[:8])
		self.assertFalse(any("co-debtors" in problem for problem in preview["problems"]), preview["problems"])

	def test_marked_codebtor_with_missing_data_blocks_the_send(self):
		incomplete = self.partner("Socio Sem Cpf")
		deal = self.card()
		with self.assertRaises(protest.TitleIncomplete) as error:
			protest.build_title_from_row(deal, deal.titles[0], self.settings, [incomplete.name])
		self.assertIn("Socio Sem Cpf", str(error.exception))
		self.assertEqual(len(error.exception.problems), 1)

	def test_contact_of_another_company_never_goes(self):
		other = frappe.get_doc(
			{"doctype": "Contact", "first_name": "Estranho", "company_name": "Outra Empresa", "cpf": "529.982.247-25"}
		).insert()
		deal = self.card()
		title, _keys = protest.build_title_from_row(deal, deal.titles[0], self.settings, [other.name])
		self.assertEqual(len(title["devedores"]), 1)

	def test_preview_warns_about_cuts_and_blocks_a_repeated_cenprot_key(self):
		deal = self.card(title_number="NF-123456789012", our_number="NN-5")
		preview = protest.preview_row(deal.name, deal.titles[0].name)
		self.assertTrue(any("NF-12345678" in note for note in preview["notes"]), preview["notes"])
		twin = self.deal(
			second_organization=self.cedente.name,
			titles=[
				{"title_number": "NF-123456789012", "our_number": "NN-5", "species": "DMI", "due_date": add_days(today(), -10),
				 "issue_date": add_days(today(), -60), "original_amount": 10},
				{"title_number": "NF-123456789099", "our_number": "NN-5", "species": "DMI", "due_date": add_days(today(), -10),
				 "issue_date": add_days(today(), -60), "original_amount": 10},
			],
		)
		preview = protest.preview_row(twin.name, twin.titles[0].name)
		self.assertTrue(any("same number" in problem for problem in preview["problems"]), preview["problems"])

	def test_preview_lists_a_missing_cenprot_setup_with_the_other_problems(self):
		deal = self.card(species="")
		frappe.db.set_single_value(SETTINGS, "cenprot_enabled", 0)
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		preview = protest.preview_row(deal.name, deal.titles[0].name)
		self.assertIn("CENPROT is off", preview["problems"][0])
		self.assertTrue(any("no default" in problem for problem in preview["problems"]), preview["problems"])
		frappe.db.set_single_value(SETTINGS, "cenprot_enabled", 1)
		frappe.db.set_single_value(SETTINGS, "cenprot_username", "")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		preview = protest.preview_row(deal.name, deal.titles[0].name)
		self.assertIn("username and password", preview["problems"][0])

	def test_errors_are_explained_and_keep_the_original_text(self):
		text = cenprot.describe({"code": "WS_ERR_102", "message": "Host de Origem nao Permitido", "ok": False})
		self.assertIn("WS_ERR_102", text)
		self.assertIn("IP", text)
		self.assertIn("Host de Origem", text)
		self.assertEqual(cenprot.describe({"code": "WS_ERR_999", "message": "novo", "ok": False}), "WS_ERR_999 novo")
		self.assertEqual(cenprot.describe({"code": "WS_SUC_201", "message": "Ok", "ok": True}), "WS_SUC_201 Ok")

	def test_central_failure_is_retried_once(self):
		session = FakeCenprot(
			EnviarTitulo=[
				answer("EnviarTitulo", "WS_ERR_306", "Falha de Comunicação com a CENPROT"),
				answer("EnviarTitulo", "WS_SUC_201", "Título Coletado com Sucesso"),
			]
		)
		with patch("cred_hub.integrations.cenprot.time.sleep") as sleep:
			result = CenprotClient("user", "pass", session=session).send_title(TITLE)
		self.assertTrue(result["ok"])
		self.assertEqual([a for a, _ in session.sent].count("EnviarTitulo"), 2)
		sleep.assert_called_once()
