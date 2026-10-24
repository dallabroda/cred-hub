"""v0.2: campos de CNPJ com nomes internos em inglês (regra de idioma do dono) e reprocessamento.

Copia os dados dos campos antigos (v0.1, em português) para os novos, remove os antigos e agenda uma nova
consulta de CNPJ para cada Organização/Lead, para preencher bairro, MEI, endereço, setor e tempo de mercado.
"""

import frappe

from cred_hub.cnpj.fields import DOCTYPES, REMOVED_FIELDS, RENAMED_FIELDS
from cred_hub.cnpj.mapping import company_size, is_valid_cnpj, only_digits
from cred_hub.install import after_install


def execute():
	after_install()  # cria os campos novos antes de copiar
	for doctype in DOCTYPES:
		columns = set(frappe.db.get_table_columns(doctype))
		for old, new in RENAMED_FIELDS.items():
			if old not in columns or new not in columns:
				continue
			if old == "porte":
				for name, value in frappe.db.sql(f"select name, porte from `tab{doctype}` where ifnull(porte, '') != ''"):
					frappe.db.set_value(doctype, name, "company_size", company_size(text=value), update_modified=False)
			else:
				frappe.db.sql(
					f"update `tab{doctype}` set `{new}` = `{old}` where `{old}` is not null and ifnull(`{new}`, '') = ''"
				)
		for old in (*RENAMED_FIELDS, *REMOVED_FIELDS):
			custom_field = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": old})
			if custom_field:
				frappe.delete_doc("Custom Field", custom_field, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype=doctype)

	for doctype in DOCTYPES:
		for name, cnpj in frappe.db.sql(f"select name, cnpj from `tab{doctype}` where ifnull(cnpj, '') != ''"):
			if is_valid_cnpj(cnpj):
				frappe.enqueue(
					"cred_hub.cnpj.enrich.enrich_doc",
					queue="long",
					job_id=f"cred_hub_cnpj::{doctype}::{name}",
					deduplicate=True,
					enqueue_after_commit=True,
					doctype=doctype,
					name=name,
					cnpj=only_digits(cnpj),
				)
