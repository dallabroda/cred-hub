import frappe

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.base_url = frappe.utils.get_url()
