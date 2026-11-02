"""Template de funil "Crédito PJ" (credtech#316): prospecção e análise de crédito para quem concede ou intermedeia
crédito a empresas.

Mínimo de propósito: um funil com as 6 etapas que qualquer operação de crédito tem (Lead, Qualificação,
Documentos, Consultas, Análise, Comitê) e um final de ganho e um de perda, com 4 campos genéricos no Card
(finalidade, prazo, garantias e decisão do comitê). Nada é obrigatório por etapa: o sistema avisa e o operador
decide, e o cliente edita etapas, campos e regras como em qualquer funil. Variações por tipo de operação
(FIDC, fintech, promotor) e campos específicos só entram quando um cliente real pedir.

Nomes em inglês (padrão do backend); pt-BR em locale/pt_BR.po.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from cred_hub.integrations.fields import CREDIT_FORM_FIELDS
from cred_hub.integrations.fields import custom_fields as integration_custom_fields

PIPELINE = "Credit PJ"
DEAL = "CRM Deal"
SETTINGS = "Cred Hub Credit Settings"

# (etapa, tipo, cor, probabilidade)
STAGES = (
	("Lead", "Open", "gray", 10),
	("Qualification", "Ongoing", "blue", 20),
	("Documents", "Ongoing", "cyan", 30),
	("Queries", "Ongoing", "violet", 40),
	("Analysis", "Ongoing", "amber", 50),
	("Committee", "Ongoing", "orange", 60),
	("Credit Granted", "Won", "green", 100),
	("Not Granted", "Lost", "red", 0),
)
# Campos padrão do Card que o funil usa (o valor pedido é o `deal_value`) + os de crédito + o faturamento da empresa
STANDARD_CARD_FIELDS = ("organization", "deal_value", "next_step", "source")


def card_fields() -> list[dict]:
	"""Campos que o funil mostra, na ordem: do Card e, por último, o faturamento anual da Organização."""
	fields = [{"block": "Card", "fieldname": name} for name in (*STANDARD_CARD_FIELDS, *CREDIT_FORM_FIELDS)]
	fields.append({"block": "Organization", "fieldname": "annual_revenue"})
	return fields


def installed_pipeline() -> str | None:
	"""O funil "Crédito PJ" deste site, se já foi instalado e ainda existe."""
	if not frappe.db.exists("DocType", SETTINGS):
		return None
	name = frappe.db.get_single_value(SETTINGS, "credit_pipeline")
	return name if name and frappe.db.exists("CRM Pipeline", name) else None


def status() -> dict:
	"""O que a tela de configurações mostra: se já está instalado e as etapas que serão criadas."""
	pipeline = installed_pipeline()
	return {
		"installed": bool(pipeline),
		"pipeline": pipeline or "",
		"stages": [_(stage[0]) for stage in STAGES],
	}


def install() -> str:
	"""Cria o funil "Crédito PJ" com etapas e campos. Uma vez por site: com o funil já criado, o cliente edita
	as etapas, não instala de novo."""
	if installed_pipeline():
		frappe.throw(_("The Credit PJ funnel is already installed. Edit its stages in the funnel settings."))
	if frappe.db.exists("CRM Pipeline", {"pipeline_name": PIPELINE}):
		frappe.throw(_("A funnel named {0} already exists.").format(PIPELINE))

	meta = frappe.get_meta(DEAL)
	if not meta.has_field("credit_decision"):
		# site que ainda não rodou o migrate com os campos de crédito
		create_custom_fields(integration_custom_fields(), update=True)
		frappe.clear_cache(doctype=DEAL)
		meta = frappe.get_meta(DEAL)

	pipeline = frappe.new_doc("CRM Pipeline")
	pipeline.pipeline_name = PIPELINE
	pipeline.color = "violet"
	pipeline.is_active = 1
	if frappe.session.user not in ("Administrator", "Guest"):
		pipeline.append("members", {"user": frappe.session.user, "role": "Owner"})
	position = 0
	for field in card_fields():
		if field["block"] == "Card" and not meta.has_field(field["fieldname"]):
			continue
		position += 1
		pipeline.append("fields", {**field, "position": position})
	pipeline.insert(ignore_permissions=True)

	for position, (label, kind, color, probability) in enumerate(STAGES, start=1):
		frappe.get_doc(
			{
				"doctype": "CRM Deal Status",
				"pipeline": pipeline.name,
				"deal_status": label,
				"type": kind,
				"color": color,
				"probability": probability,
				"position": position,
			}
		).insert(ignore_permissions=True)

	settings = frappe.get_single(SETTINGS)
	settings.credit_pipeline = pipeline.name
	settings.save(ignore_permissions=True)
	return pipeline.name
