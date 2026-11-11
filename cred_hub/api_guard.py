"""What an API key may send and see beyond the route allow-list (audit 11/11/2026, credtech#375).

- Writes: fields the system fills (read only on the form: credit verdict, ERP run, protest status, SLA, totals...)
  and record internals (owner, docstatus...) are refused when a key tries to change them; child-table rows cannot
  carry their own doctype/parent nor point at a row of another record.
- Uploads: only document types we accept, attached only to a record the key can write, never a `file_url`
  (that would point a File at another stored file).
- Files listed or read by a key follow the card they are attached to (pipelines of the key).
"""

import os

import frappe
from frappe import _
from frappe.utils import cstr, flt, get_datetime, getdate

# Read-only on the form but legitimate input from an integration: contact channels (Frappe derives them from the
# child tables, and integrations send them flat) and lead/card attribution (UTM and ads of the source system)
WRITABLE_READ_ONLY = {
	"Contact": frozenset({"email_id", "phone", "mobile_no"}),
	"CRM Lead": frozenset(
		{
			"ad_platform",
			"ad_campaign_name",
			"ad_adset_name",
			"ad_name",
			"ad_form_name",
			"ad_campaign_id",
			"ad_adset_id",
			"ad_id",
			"lead_captured_at",
			"utm_source",
			"utm_medium",
			"utm_campaign",
			"utm_content",
			"utm_term",
			"gclid",
			"fbclid",
			"li_fat_id",
			"landing_page",
		}
	),
}
WRITABLE_READ_ONLY["CRM Deal"] = WRITABLE_READ_ONLY["CRM Lead"]
# Record internals a key never sets (`modified` stays allowed: Frappe uses it to refuse stale updates)
SYSTEM_FIELDS = frozenset(
	{"owner", "creation", "modified_by", "docstatus", "idx", "parent", "parentfield", "parenttype", "lft", "rgt"}
)
CHILD_SYSTEM_FIELDS = frozenset({"doctype", "parent", "parentfield", "parenttype", "owner", "creation", "docstatus"})
# Documents a key may upload (the Digital Folder list plus plain text and XML/OFX of bank and ERP exports)
UPLOAD_EXTENSIONS = frozenset(
	{
		".pdf",
		".jpg",
		".jpeg",
		".png",
		".webp",
		".gif",
		".xlsx",
		".xls",
		".csv",
		".doc",
		".docx",
		".zip",
		".txt",
		".xml",
		".ofx",
	}
)
# upload_file arguments a key may not use: `file_url` re-points a File at an existing file; `fieldname` writes the
# file URL into a field of the record, around the write checks
UPLOAD_DENIED_ARGS = ("file_url", "fieldname")


class WriteRefused(frappe.ValidationError):
	pass


def _same(df, current, new) -> bool:
	if current in (None, "") and new in (None, ""):
		return True
	try:
		if df.fieldtype in ("Int", "Float", "Currency", "Percent", "Check", "Long Int"):
			return flt(current) == flt(new)
		if df.fieldtype == "Date":
			return getdate(current) == getdate(new)
		if df.fieldtype == "Datetime":
			return get_datetime(current) == get_datetime(new)
	except Exception:
		return False
	return cstr(current) == cstr(new)


def refused_fields(doctype: str, data: dict, name: str | None = None) -> list[str]:
	"""Fields of `data` a key may not change: system internals, and read-only fields whose value differs from the
	stored one (sending back what was read is fine). New records: any value counts as a change."""
	meta = frappe.get_meta(doctype)
	allowed = WRITABLE_READ_ONLY.get(doctype, frozenset())
	refused = [k for k in data if k in SYSTEM_FIELDS and not (k == "docstatus" and not data[k])]
	if data.get("doctype") not in (None, "", doctype):
		refused.append("doctype")
	read_only = {
		k: meta.get_field(k)
		for k in data
		if meta.get_field(k) and meta.get_field(k).read_only and k not in allowed and k not in SYSTEM_FIELDS
	}
	if read_only:
		current = (frappe.db.get_value(doctype, name, list(read_only), as_dict=True) if name else None) or {}
		refused += [k for k, df in read_only.items() if not _same(df, current.get(k), data[k])]
	return refused


def refused_child_rows(doctype: str, data: dict, name: str | None = None) -> list[str]:
	"""Child rows that carry internals or the name of a row that is not this record's (Frappe would move it)."""
	meta = frappe.get_meta(doctype)
	problems = []
	for df in meta.get_table_fields():
		rows = data.get(df.fieldname)
		if not isinstance(rows, list):
			continue
		own = None
		for row in rows:
			if not isinstance(row, dict):
				problems.append(df.fieldname)
				break
			if any(row.get(k) not in (None, "", df.options if k == "doctype" else None) for k in CHILD_SYSTEM_FIELDS):
				problems.append(df.fieldname)
				break
			if row.get("name"):
				if own is None:
					own = set(
						frappe.get_all(df.options, {"parent": name, "parenttype": doctype}, pluck="name") if name else []
					)
				if row["name"] not in own:
					problems.append(df.fieldname)
					break
	return problems


def check_write(doctype: str, data: dict, name: str | None = None):
	"""Raise WriteRefused (417) naming what a key may not write."""
	if not isinstance(data, dict):
		return
	fields = refused_fields(doctype, data, name)
	if fields:
		raise WriteRefused(_("These fields are filled by the system and cannot be changed by the API: {0}").format(", ".join(fields)))
	tables = refused_child_rows(doctype, data, name)
	if tables:
		raise WriteRefused(
			_("Rows of {0} cannot set doctype, parent or the name of a row of another record.").format(", ".join(tables))
		)


def request_payload() -> dict:
	"""Body of a REST write as Frappe reads it (v1: `data` or the JSON body; v2: the form)."""
	form = frappe.local.form_dict or {}
	data = form.get("data")
	if data is None:
		return {k: v for k, v in form.items() if k not in ("cmd", "name")}
	try:
		data = frappe.parse_json(data)
	except Exception:
		return {}
	return data if isinstance(data, dict) else {}


def upload_file_name() -> str:
	try:
		files = frappe.request.files or {}
	except Exception:  # no request bound (jobs, console)
		return ""
	name = getattr(files.get("file"), "filename", None)
	return name.strip() if isinstance(name, str) else ""


def check_upload(scopes: set, resources: dict):
	"""upload_file through a key: a real multipart file of an accepted type, attached (if at all) to a record whose
	doctype the key may write. Runs before the user is set; the record itself is checked by `check_upload_record`."""
	from cred_hub.api_keys import _deny

	form = frappe.local.form_dict or {}
	if any(form.get(arg) not in (None, "") for arg in UPLOAD_DENIED_ARGS):
		_deny()
	file_name = upload_file_name()
	if not file_name:
		raise WriteRefused(_("Send the file in the multipart field file."))
	if os.path.splitext(file_name)[1].lower() not in UPLOAD_EXTENSIONS:
		raise WriteRefused(_("File type not allowed through the API: {0}").format(file_name or "?"))
	doctype = form.get("doctype")
	if doctype or form.get("docname"):
		# attaching changes the record: the key needs the WRITE scope of its doctype, reading is not enough
		_read, write = resources.get(doctype, (None, None))
		if not doctype or not form.get("docname") or not write or write not in scopes:
			_deny()


def check_upload_record():
	"""After the key's user is set: the record the file goes to must be one this key can write (pipelines apply)."""
	from cred_hub.api_keys import _deny

	form = frappe.local.form_dict or {}
	doctype, docname = form.get("doctype"), form.get("docname")
	if doctype and not frappe.has_permission(doctype, "write", doc=docname):
		_deny()


# ---------- Files follow the card (API users only) ----------


def file_query_conditions(user=None) -> str:
	"""Files a key lists: those attached to cards it sees (its pipelines), notes and tasks of those cards, or other
	records; never files of cards of other pipelines. People keep Frappe's own rules."""
	user = user or frappe.session.user
	from cred_hub.api_keys import is_api_user

	if not is_api_user(user):
		return ""
	from crm.permissions.reference import _visible_deals_sql

	deals = _visible_deals_sql(user)
	if not deals:
		# fail closed: an API user never sees files of cards it cannot list
		return "ifnull(`tabFile`.`attached_to_doctype`, '') != 'CRM Deal'"
	return (
		"(ifnull(`tabFile`.`attached_to_doctype`, '') != 'CRM Deal' "
		f"or `tabFile`.`attached_to_name` in ({deals}))"
	)


def has_file_permission(doc, ptype=None, user=None) -> bool:
	user = user or frappe.session.user
	from cred_hub.api_keys import is_api_user

	if not is_api_user(user) or doc.get("attached_to_doctype") != "CRM Deal" or not doc.get("attached_to_name"):
		return True
	return bool(frappe.has_permission("CRM Deal", "read", doc=doc.attached_to_name, user=user))
