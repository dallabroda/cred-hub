"""Painel do Cred Hub dentro da conversa do Cred Chat (credtech#45).

Decisões do dono (26/10/2026):
- o painel usa o login do Cred Hub no mesmo navegador e as permissões do próprio usuário (Cred Chat e Cred Hub são
  o mesmo site, credtechdigital.com.br, então o cookie de sessão vale dentro do iframe); sem login, mostra o botão
  para entrar;
- é instalado sozinho pela conexão do Cred Chat que o admin já faz para a boas-vindas (#53, credtech#42): vira um
  Dashboard App "Cred Hub" na conta do Cred Chat;
- telefone sem card só avisa (criar lead pela conversa é a credtech#43, congelada).

O card é achado pelo telefone (mesma regra da entrada de leads: número nacional, sem o 55) ou pelo e-mail do
contato, no próprio card ou nos contatos ligados a ele. O painel mostra e altera etapa e valor.
"""

import frappe
from crm.fcrm.doctype.crm_lead.crm_lead import national_number
from frappe import _
from frappe.utils import cint, flt, get_url

ROUTE = "/chat-panel"
TITLE = "Cred Hub"
CARD_LIMIT = 20


def panel_url():
	"""Endereço público do painel. Fora de uma requisição (job), o Frappe pode montar http: o Cred Chat é https e
	não abre iframe http."""
	url = get_url(ROUTE)
	if url.startswith("http://") and "localhost" not in url:
		url = "https://" + url[len("http://") :]
	return url


# Instalação no Cred Chat (Dashboard App)


def install(doc, token):
	"""Cria o Dashboard App do painel na conta conectada, ou reaproveita o que já aponta para cá. Falha não
	desfaz a conexão: o motivo fica em `panel_error` para a tela mostrar. Não salva `doc`."""
	from cred_hub.welcome import ChatError, _call

	account_id = cint(doc.account_id)
	url = panel_url()
	try:
		profile = _call("GET", "/api/v1/profile", token)
		account = next((a for a in profile.get("accounts") or [] if cint(a.get("id")) == account_id), {})
		if account.get("role") != "administrator":
			raise ChatError(_("To show the Cred Hub panel in conversations, the access key must belong to a Cred Chat administrator."))
		base = f"/api/v1/accounts/{account_id}/dashboard_apps"
		current = next(
			(a for a in _call("GET", base, token) or [] if any(c.get("url") == url for c in a.get("content") or [])),
			None,
		)
		if not current:
			current = _call("POST", base, token, json={"dashboard_app": {"title": TITLE, "content": [{"type": "frame", "url": url}]}})
		doc.panel_app_id, doc.panel_error = cint(current.get("id")), None
	except ChatError as e:
		doc.panel_app_id, doc.panel_error = None, str(e)


def uninstall(doc, token):
	"""Tira o painel do Cred Chat ao desconectar; se não der (chave já revogada), fica lá sem efeito."""
	from cred_hub.welcome import ChatError, _call

	if doc.panel_app_id and token:
		try:
			_call("DELETE", f"/api/v1/accounts/{cint(doc.account_id)}/dashboard_apps/{cint(doc.panel_app_id)}", token)
		except ChatError:
			pass
	doc.panel_app_id, doc.panel_error = None, None


def install_pending():
	"""Conexões feitas antes do painel existir: instala no próximo migrate, em segundo plano (chama o Cred Chat)."""
	from cred_hub.welcome import CONNECTION

	doc = frappe.get_single(CONNECTION)
	if doc.account_id and not doc.panel_app_id and not doc.panel_error:
		frappe.enqueue("cred_hub.chat_panel.install_job", queue="short", enqueue_after_commit=True)


def install_job():
	from cred_hub.welcome import _connection

	doc, token = _connection()
	if not doc or not token:
		return
	install(doc, token)
	doc.save(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def install_chat_panel():
	"""Tentar de novo pela tela (ex.: depois de virar administrador no Cred Chat)."""
	from cred_hub.welcome import _connection, _only_managers, get_chat_connection

	_only_managers()
	doc, token = _connection()
	if not doc:
		frappe.throw(_("Connect Cred Chat first."))
	install(doc, token)
	doc.save(ignore_permissions=True)
	return get_chat_connection()


# Cards do contato da conversa


def _digits_sql(column):
	"""Mesma regra de `national_number` em SQL: só dígitos, sem o 55 quando o número tem mais de 11 dígitos."""
	digits = f"regexp_replace(ifnull({column}, ''), '[^0-9]', '')"
	return f"(case when {digits} like '55%%' and length({digits}) > 11 then substring({digits}, 3) else {digits} end)"


def _matching_cards(digits, email):
	"""Cards com o telefone ou e-mail no próprio card ou num contato ligado a ele; abertos primeiro."""
	matches, phone_contacts = [], []
	if len(digits) >= 10:
		phone_contacts.append(
			f"select parent from `tabContact Phone` where parenttype = 'Contact' and {_digits_sql('phone')} = %(digits)s"
		)
		matches += [f"{_digits_sql('d.mobile_no')} = %(digits)s", f"{_digits_sql('d.phone')} = %(digits)s"]
	if email:
		phone_contacts.append("select parent from `tabContact Email` where parenttype = 'Contact' and lower(email_id) = %(email)s")
		matches.append("lower(d.email) = %(email)s")
	if not matches:
		return []
	contacts = " union ".join(phone_contacts)
	matches += [
		f"d.contact in ({contacts})",
		f"d.name in (select parent from `tabCRM Contacts` where parenttype = 'CRM Deal' and contact in ({contacts}))",
	]
	rows = frappe.db.sql(
		f"""select d.name from `tabCRM Deal` d
		left join `tabCRM Deal Status` s on s.name = d.status
		where {" or ".join(matches)}
		order by ifnull(s.type, '') in ('Won', 'Lost'), d.modified desc
		limit {CARD_LIMIT}""",
		{"digits": digits, "email": email},
	)
	return [r[0] for r in rows]


def _stages(pipeline):
	return [
		{"value": s.name, "label": _(s.deal_status), "type": s.type, "color": s.color}
		for s in frappe.get_all(
			"CRM Deal Status", {"pipeline": pipeline}, ["name", "deal_status", "type", "color"], order_by="position asc"
		)
	]


def _card(row):
	stage = frappe.db.get_value("CRM Deal Status", row.status, ["deal_status", "type", "color"], as_dict=True) or {}
	return {
		"name": row.name,
		"title": row.lead_name or row.organization or row.name,
		"pipeline": row.pipeline,
		"pipeline_name": frappe.db.get_value("CRM Pipeline", row.pipeline, "pipeline_name") or row.pipeline,
		"status": row.status,
		"status_label": _(stage.get("deal_status") or row.status or ""),
		"status_type": stage.get("type"),
		"status_color": stage.get("color"),
		"deal_value": row.deal_value,
		# Com produtos, o CRM regrava o valor com o total deles: o painel mostra sem deixar editar
		"value_from_products": bool(frappe.db.exists("CRM Products", {"parenttype": "CRM Deal", "parent": row.name})),
		"currency": row.currency,
		"owner": frappe.utils.get_fullname(row.deal_owner) if row.deal_owner else None,
		"url": get_url(f"/crm/deals/{row.name}"),
		"can_write": bool(frappe.has_permission("CRM Deal", "write", row.name)),
		"stages": _stages(row.pipeline),
	}


_FIELDS = ["name", "lead_name", "organization", "pipeline", "status", "deal_value", "currency", "deal_owner"]


@frappe.whitelist(methods=["GET"])
def find_cards(phone=None, email=None):
	"""Cards que o usuário logado pode ver, do contato da conversa aberta no Cred Chat."""
	names = _matching_cards(national_number(phone), (email or "").strip().lower())
	if not names:
		return []
	# get_list aplica as permissões do usuário (funis a que tem acesso, só os próprios cards etc.)
	rows = {r.name: r for r in frappe.get_list("CRM Deal", filters={"name": ("in", names)}, fields=_FIELDS)}
	return [_card(rows[n]) for n in names if n in rows]


@frappe.whitelist(methods=["POST"])
def update_card(name, status=None, deal_value=None):
	"""Muda etapa e/ou valor com as validações do card (campos obrigatórios da etapa, motivo da perda etc.)."""
	doc = frappe.get_doc("CRM Deal", name)
	doc.check_permission("write")
	if status:
		doc.status = status
	if deal_value not in (None, "") and not doc.get("products"):
		doc.deal_value = flt(deal_value)
	doc.save()
	row = frappe.get_list("CRM Deal", filters={"name": doc.name}, fields=_FIELDS)
	return _card(row[0]) if row else None
