"""Templates of the generic REST connector (ADR 0007 §8, credtech#375): export a working configuration without the
client's address and secrets, and load a template into a new configuration. System Manager only.

A template carries the endpoints, pagination, item paths, field paths, outcome values and suggested de-para of a
system. It never carries the API address (`base_url`) nor any secret value: the configuration only has secret NAMES
(`{secret:token}`), the values stay in the encrypted `rest_secrets` table, which is not exported. A template only
becomes public after CredTech reviews it (curation is a process, not code).

A template whose OAuth2 token comes from another host than the API (a vendor's central login) is loaded only after
the admin confirms that host (`confirm_token_url`): the client's secrets are sent to it. A token URL on the client's
own API host travels as a path only.
"""

import json

import frappe
from frappe import _

from cred_hub.billing import CREDIT_AND_COLLECTION, requires_feature

from .connectors.rest import GENERIC
from .rest import ConfigError, RestConfig, TokenHostNotConfirmed

SETTINGS = "Cred Hub ERP Settings"


@frappe.whitelist(methods=["GET", "POST"])
def export_template() -> dict:
	"""The saved generic REST configuration as a template (JSON object)."""
	frappe.only_for("System Manager")
	raw = frappe.db.get_single_value(SETTINGS, "rest_config")
	if not raw:
		frappe.throw(_("There is no REST configuration to export."))
	try:
		return RestConfig.parse(raw).template()
	except ConfigError as e:
		frappe.throw(_("REST configuration: {0}").format(str(e)))


@frappe.whitelist(methods=["POST"])
@requires_feature(CREDIT_AND_COLLECTION)
def import_template(template, base_url: str = "", confirm_token_url: str = "") -> dict:
	"""Loads a template as the site's generic REST configuration. The API address is the one given (or the one
	already saved); secrets are filled afterwards in the REST secrets table. Returns the secret names to fill, or
	{"ok": False, "confirm_token_url": <host>} (nothing saved) when the token host needs the admin's confirmation."""
	frappe.only_for("System Manager")
	settings = frappe.get_doc(SETTINGS)
	if not base_url and settings.get("rest_config"):
		try:
			base_url = json.loads(settings.rest_config).get("base_url") or ""
		except ValueError:
			base_url = ""
	if not (base_url or "").strip():
		frappe.throw(_("Inform the API URL of this client to load the template."))
	try:
		config = RestConfig.from_template(template, base_url, confirm_token_host=confirm_token_url)
	except TokenHostNotConfirmed as e:
		return {"ok": False, "confirm_token_url": e.host, "message": _(
			"This template gets the access token from {0}, not from the API address. Your secrets will be sent "
			"there: load it again confirming this host."
		).format(e.host)}
	except ConfigError as e:
		frappe.throw(_("Template: {0}").format(str(e)))
	if settings.erp_provider != GENERIC:
		settings.erp_provider = GENERIC
	settings.rest_config = config.as_json()
	settings.save()
	filled = {(row.secret_name or "").strip() for row in settings.get("rest_secrets") or []}
	return {"ok": True, "system": config.system, "secrets_to_fill": [n for n in config.secret_names() if n not in filled]}


CURATED_TEMPLATES = [
	{
		"id": "asaas",
		"name": "Asaas (Cobranças & Liquidações)",
		"category": "Fintech / Cobrança",
		"description": "Sincroniza cobranças vencidas, liquidações via Pix/Boleto e cadastro de clientes do Asaas diretamente com o funil de Cobrança do HubTech.",
		"default_base_url": "https://api.asaas.com/v3",
		"secrets_required": ["token"],
		"template": {
			"version": 1,
			"system": "Asaas",
			"auth": {"type": "header", "header": "access_token", "value": "{secret:token}"},
			"headers": {"Accept": "application/json"},
			"pace": {"min_interval": 1, "report_cooldown": 300},
			"endpoints": {
				"ping": {
					"method": "GET",
					"path": "/finance/balance",
				},
				"overdue_titles": {
					"method": "GET",
					"path": "/payments",
					"query": {"status": "OVERDUE", "dueDate[le]": "{until}"},
					"date_format": "iso",
					"items": "data",
					"pagination": {"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 100, "total_path": "totalCount"},
					"fields": {
						"erp_id": "id",
						"number": "invoiceNumber",
						"due_date": "dueDate",
						"amount": "value",
						"sacado_document": "customerCpfCnpj",
						"sacado_name": "customerName",
						"digitable_line": "identificationField",
						"situation": "status",
					},
					"outcomes": {"paid": ["RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"]},
					"default_outcome": "open",
				},
				"settled_titles": {
					"method": "GET",
					"path": "/payments",
					"query": {"paymentDate[ge]": "{since}", "paymentDate[le]": "{until}"},
					"date_format": "iso",
					"items": "data",
					"pagination": {"type": "offset", "offset_param": "offset", "limit_param": "limit", "limit": 100, "total_path": "totalCount"},
					"fields": {
						"erp_id": "id",
						"due_date": "dueDate",
						"paid_date": "paymentDate",
						"paid_amount": "value",
						"situation": "status",
					},
					"outcomes": {"paid": ["RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"]},
					"default_outcome": "paid",
				},
				"register_cedente": {
					"method": "POST",
					"path": "/customers",
					"body": {"cpfCnpj": "{document}", "name": "{{payload.name}}", "email": "{{payload.email}}"},
					"id_path": "id",
					"already": {"status": [409], "contains": ["já cadastrado", "already exists"]},
					"lookup": {
						"path": "/customers",
						"query": {"cpfCnpj": "{document}"},
						"items": "data",
						"id_path": "id",
						"document": "cpfCnpj",
					},
					"fields": [
						{"erp_field": "name", "label": "Legal name", "transform": "trim", "required": True, "source": "organization.legal_name|organization.organization_name"},
						{"erp_field": "email", "label": "E-mail", "transform": "lower", "required": False, "source": "contact.email_id|organization.email"},
					],
				},
			},
			"template": True,
		},
	},
	{
		"id": "bling_v3",
		"name": "Bling ERP (API v3)",
		"category": "ERP B2B / Varejo",
		"description": "Conexão REST API v3 com o Bling ERP: títulos a receber vencidos, liquidações e cadastro de contatos/empresas (Nota: para Omie ERP, utilize os endpoints Push da Porta B).",
		"default_base_url": "https://api.bling.com.br/Api/v3",
		"secrets_required": ["token"],
		"template": {
			"version": 1,
			"system": "Bling ERP",
			"auth": {"type": "bearer", "token": "{secret:token}"},
			"headers": {"Accept": "application/json"},
			"pace": {"min_interval": 1, "report_cooldown": 300},
			"endpoints": {
				"ping": {
					"method": "GET",
					"path": "/empresas/me/dados-basicos",
				},
				"overdue_titles": {
					"method": "GET",
					"path": "/contas/receber",
					"query": {"situacoes[]": "1", "dataFinal": "{until}", "tipoFiltroData": "V"},
					"date_format": "iso",
					"items": "data",
					"pagination": {"type": "page", "page_param": "pagina", "size_param": "limite", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"number": "numeroDocumento",
						"due_date": "vencimento",
						"amount": "valor",
						"sacado_document": "contato.numeroDocumento",
						"sacado_name": "contato.nome",
						"situation": "situacao",
					},
					"outcomes": {"paid": ["2", "recebido"]},
					"default_outcome": "open",
				},
				"settled_titles": {
					"method": "GET",
					"path": "/contas/receber",
					"query": {"situacoes[]": "2", "dataInicial": "{since}", "dataFinal": "{until}", "tipoFiltroData": "P"},
					"date_format": "iso",
					"items": "data",
					"pagination": {"type": "page", "page_param": "pagina", "size_param": "limite", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"due_date": "vencimento",
						"paid_date": "dataRecebimento",
						"paid_amount": "valor",
						"situation": "situacao",
					},
					"outcomes": {"paid": ["2", "recebido"]},
					"default_outcome": "paid",
				},
			},
			"template": True,
		},
	},
	{
		"id": "conta_azul",
		"name": "Conta Azul",
		"category": "ERP PME",
		"description": "Conexão direta com a API REST da Conta Azul para conciliação de contas a receber vencidas e baixadas.",
		"default_base_url": "https://api.contaazul.com/v1",
		"secrets_required": ["token"],
		"template": {
			"version": 1,
			"system": "Conta Azul",
			"auth": {"type": "bearer", "token": "{secret:token}"},
			"headers": {"Accept": "application/json"},
			"pace": {"min_interval": 1, "report_cooldown": 300},
			"endpoints": {
				"ping": {
					"method": "GET",
					"path": "/profile",
				},
				"overdue_titles": {
					"method": "GET",
					"path": "/finance/receivables",
					"query": {"status": "OVERDUE", "due_date_to": "{until}"},
					"date_format": "iso",
					"items": "items",
					"pagination": {"type": "page", "page_param": "page", "size_param": "size", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"number": "document_number",
						"due_date": "due_date",
						"amount": "total",
						"sacado_document": "customer.document",
						"sacado_name": "customer.name",
						"situation": "status",
					},
					"outcomes": {"paid": ["ACQUITTED", "RECEIVED"]},
					"default_outcome": "open",
				},
				"settled_titles": {
					"method": "GET",
					"path": "/finance/receivables",
					"query": {"status": "ACQUITTED", "payment_date_from": "{since}", "payment_date_to": "{until}"},
					"date_format": "iso",
					"items": "items",
					"pagination": {"type": "page", "page_param": "page", "size_param": "size", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"due_date": "due_date",
						"paid_date": "payment_date",
						"paid_amount": "paid_value",
						"situation": "status",
					},
					"outcomes": {"paid": ["ACQUITTED", "RECEIVED"]},
					"default_outcome": "paid",
				},
			},
			"template": True,
		},
	},
	{
		"id": "protheus",
		"name": "TOTVS Protheus (REST TLPP)",
		"category": "ERP Enterprise",
		"description": "Conexão REST TLPP com TOTVS Protheus (tabela SE1 - Contas a Receber e Liquidações).",
		"default_base_url": "https://protheus.empresa.com.br/rest/api/v1",
		"secrets_required": ["token"],
		"template": {
			"version": 1,
			"system": "TOTVS Protheus",
			"auth": {"type": "bearer", "token": "{secret:token}"},
			"headers": {"Accept": "application/json"},
			"pace": {"min_interval": 1, "report_cooldown": 300},
			"endpoints": {
				"ping": {
					"method": "GET",
					"path": "/healthcheck",
				},
				"overdue_titles": {
					"method": "GET",
					"path": "/financial/receivables",
					"query": {"overdue": "true", "dueUntil": "{until}"},
					"date_format": "iso",
					"items": "items",
					"pagination": {"type": "page", "page_param": "page", "size_param": "pageSize", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"number": "titleNumber",
						"due_date": "dueDate",
						"amount": "value",
						"sacado_document": "customerDocument",
						"sacado_name": "customerName",
						"cedente_document": "assignorDocument",
						"situation": "status",
					},
					"outcomes": {"paid": ["settled", "baixado"], "repurchased": ["repurchased", "recomprado"]},
					"default_outcome": "open",
				},
				"settled_titles": {
					"method": "GET",
					"path": "/financial/receivables",
					"query": {"status": "settled", "settledFrom": "{since}", "settledTo": "{until}"},
					"date_format": "iso",
					"items": "items",
					"pagination": {"type": "page", "page_param": "page", "size_param": "pageSize", "size": 100, "start": 1},
					"fields": {
						"erp_id": "id",
						"due_date": "dueDate",
						"paid_date": "settlementDate",
						"paid_amount": "paidValue",
						"situation": "status",
					},
					"outcomes": {"paid": ["settled", "baixado"], "repurchased": ["repurchased", "recomprado"]},
					"default_outcome": "paid",
				},
			},
			"template": True,
		},
	},
]


@frappe.whitelist(methods=["GET"])
def list_curated_templates() -> list[dict]:
	"""Lista os modelos oficiais pré-configurados pela CredTech para integração rápida (1 clique)."""
	frappe.only_for("System Manager")
	return [
		{
			"id": t["id"],
			"name": t["name"],
			"category": t["category"],
			"description": t["description"],
			"default_base_url": t["default_base_url"],
			"secrets_required": t["secrets_required"],
			"template": t["template"],
		}
		for t in CURATED_TEMPLATES
	]
