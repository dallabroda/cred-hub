"""Credit funnel -> ERP: register the card's company as a cedente in the ERP (credtech#367, PR 3).

Owner decisions (10/11/2026):
- Only the Credit funnel (the Collection funnel's cedentes are already in the ERP).
- The trigger is configurable per company: "card enters stage X of funnel Y" or manual only; off by default.
- The cedente goes with enriched data: the Organization filled by the Receita (OpenCNPJ/BrasilAPI: address in parts,
  IBGE code, tax regime), the card's main contact and the credit committee's approved limit.
- No ERP id is typed in the CRM: sector (by the CNAE), manager (card owner, else the company's default manager) and
  tax regime are matched by name against the ERP's own lists.
- Every attempt leaves a note on the card and on the Organization: created (with the manager), already registered,
  missing data, or refused by the ERP.
- Smart Securities is append-only (no update/delete by API): an existing CNPJ is never resent.

The resolvers at the top are pure (tested with plain pytest); the rest is Frappe glue.
"""

import re
import unicodedata

from .formats import digits

STAGE, MANUAL = "Card enters a stage", "Manual only"
TRIGGERS = (STAGE, MANUAL)
FROM_OWNER, FROM_DEFAULT = "card owner", "default manager"

# CNAE division (2 digits) -> words that name a specific sector, tried before the generic section
_SPECIFIC = (
	(range(62, 64), ("TI", "INFORMATICA", "TECNOLOGIA")),
	(range(49, 54), ("TRANSPORTE", "LOGISTICA")),
	(range(86, 89), ("SAUDE",)),
	(range(41, 44), ("CONSTRU",)),
	(range(13, 16), ("CONFEC", "VESTU", "TEXTIL")),
	(range(24, 26), ("METALUR",)),
	(range(22, 23), ("PLASTIC", "EMBALAG")),
	(range(1, 4), ("PRODUTOR RURAL", "AGRO")),
)


def norm(text) -> str:
	text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().upper()
	return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", text).split())


def cnae_division(main_cnae) -> int | None:
	code = digits(str(main_cnae or "").strip().split(" ")[0])
	return int(code[:2]) if len(code) >= 2 else None


def _generic_section(division: int) -> str:
	if 10 <= division <= 33:
		return "INDUSTRIA"
	if 45 <= division <= 47:
		return "COMERCIO"
	if 1 <= division <= 3:
		return "AGRO"
	return "SERVICOS"


def smart_sector(main_cnae, sectors: list[dict], overrides: dict | None = None) -> dict | None:
	"""The Smart sector for a CNAE: the company's override for that division, else the most specific sector name
	(e.g. division 62 -> "Serviço de TI"), else the generic one (Indústria / Comércio / Agro / Serviços)."""
	division = cnae_division(main_cnae)
	if division is None:
		return None
	by_name = {norm(s.get("descricao")): s for s in sectors}
	wanted = (overrides or {}).get(division) or (overrides or {}).get(f"{division:02d}")
	if wanted:
		return by_name.get(norm(wanted))
	for divisions, words in _SPECIFIC:
		if division in divisions:
			# Words in order of preference (a plastics factory is "Plástico" before "Embalagens")
			for word in words:
				for sector_name, sector in by_name.items():
					if word in sector_name.split() or (len(word) > 4 and word in sector_name):
						return sector
	generic = _generic_section(division)
	exact = next((s for name, s in by_name.items() if name == generic), None)
	return exact or next((s for name, s in by_name.items() if name.startswith(generic[:-1])), None)


def parse_overrides(text) -> dict:
	"""Settings text, one per line: "62 = Serviço de TI" -> {62: "Serviço de TI"}."""
	result = {}
	for line in str(text or "").splitlines():
		left, sep, right = line.partition("=")
		if sep and digits(left) and right.strip():
			result[int(digits(left)[:2])] = right.strip()
	return result


def smart_manager(owner_name, default_name, managers: list[dict]) -> tuple[dict | None, str]:
	"""(manager, where it came from). The card owner's name when it matches exactly one Smart manager, else the
	company's default manager. (None, "") when neither matches."""
	by_name = {}
	for manager in managers:
		by_name.setdefault(norm(manager.get("descricao")), []).append(manager)
	owner = norm(owner_name)
	if owner and len(by_name.get(owner) or []) == 1:
		return by_name[owner][0], FROM_OWNER
	default = norm(default_name)
	if default and by_name.get(default):
		return by_name[default][0], FROM_DEFAULT
	return None, ""


_REGIME_WORDS = {"MEI": "MEI", "Simples Nacional": "SIMPLES", "Lucro Presumido": "PRESUMIDO", "Lucro Real": "REAL"}


def smart_regime(tax_regime, regimes: list[dict]) -> dict | None:
	"""Receita regime -> the Smart's (S Simples, M Simples-MEI, P Presumido, R Real), by name. None if unknown."""
	word = _REGIME_WORDS.get(tax_regime or "")
	if not word:
		return None
	names = {norm(r.get("descricao")): r for r in regimes}
	if word == "SIMPLES":
		return names.get("SIMPLES") or next((r for n, r in names.items() if "SIMPLES" in n and "MEI" not in n), None)
	return next((r for n, r in names.items() if word in n.split()), None)


# Frappe glue -------------------------------------------------------------------------------------------------------


def on_deal_update(doc, method=None):
	"""CRM Deal on_update: the card entered the configured stage of the configured funnel -> send in background."""
	if not doc.has_value_changed("status") or not doc.get("organization"):
		return
	import frappe

	from .settings import get_settings

	settings = get_settings()
	if not (settings.erp_enabled and settings.get("cedente_send_enabled")):
		return
	if settings.get("cedente_trigger") != STAGE or not settings.get("cedente_stage"):
		return
	if doc.pipeline != settings.get("cedente_pipeline") or doc.status != settings.cedente_stage:
		return
	_enqueue(frappe, doc.name)


def _enqueue(frappe, deal: str):
	frappe.enqueue(
		"cred_hub.erp.cedente.send_for_deal",
		queue="long",
		job_id=f"cred_hub_erp_cedente::{deal}",
		deduplicate=True,
		enqueue_after_commit=True,
		deal=deal,
	)


def send_now(deal: str) -> dict:
	"""(Whitelisted as cred_hub.erp.settings.send_cedente.) Card button / API: send this card's company now (works with either trigger, never when switched off)."""
	import frappe
	from frappe import _

	from .settings import get_settings

	doc = frappe.get_doc("CRM Deal", deal)
	doc.check_permission("write")
	settings = get_settings()
	if not (settings.erp_enabled and settings.get("cedente_send_enabled")):
		return {"ok": False, "message": _("Turn on the cedente registration in the ERP integration settings first.")}
	if settings.get("cedente_pipeline") and doc.pipeline != settings.cedente_pipeline:
		return {"ok": False, "message": _("Only cards of the credit funnel register the cedente in the ERP.")}
	if not doc.get("organization"):
		return {"ok": False, "message": _("The card has no company to register.")}
	_enqueue(frappe, deal)
	return {"ok": True, "message": _("Sending to the ERP. The result appears in the card's activity.")}


def send_for_deal(deal: str) -> dict:
	"""Background job: build the cedente from the card, check the ERP, send, and leave the note."""
	import frappe

	from cred_hub.integrations.bureau_log import ERROR, SUCCESS

	from .base import ErpError
	from .settings import client, get_settings, log, user_message

	settings = get_settings()
	doc = frappe.get_doc("CRM Deal", deal)
	org = frappe.get_doc("CRM Organization", doc.organization)
	provider = settings.erp_provider
	# One registration at a time per site: two cards sent together would otherwise take the same master contract
	# number from the Smart, or send the same company twice
	if not _acquire_lock(frappe):
		_note(doc, org, frappe._("Another cedente registration is running. Send this card again in a minute."))
		frappe.db.commit()
		return {"ok": False, "busy": True}
	try:
		ensure_enriched(org)
		records = _records(doc, org)
		result = _build(settings, records)
		if not result.ok:
			return _missing(doc, org, provider, [r.label or r.erp_field for r in result.missing], result.errors)
		erp = client(settings)
		if provider == "Smart Securities":
			outcome = _send_smart(erp, settings, doc, org, result)
		else:
			outcome = _send_wba(erp, org, result)
	except _Missing as e:
		return _missing(doc, org, provider, e.items)
	except ErpError as e:
		frappe.db.rollback()
		_note(doc, org, frappe._("{0} refused the cedente registration: {1}").format(provider, user_message(e)))
		frappe.db.commit()
		_safe_log(log, provider, ERROR, org, doc, http_status=e.http_status, error=str(e))
		return {"ok": False, "error": str(e)}
	except Exception as e:
		# Not an ERP answer (a bug, a lookup that changed shape): the card still says it did not go, then it fails loudly
		frappe.db.rollback()
		_note(doc, org, frappe._("The cedente registration stopped because of an unexpected error. Support was notified. Check in the ERP whether the cedente was created before sending again."))
		frappe.db.commit()
		_safe_log(log, provider, ERROR, org, doc, error=f"{type(e).__name__}: {e}")
		raise
	finally:
		_release_lock(frappe)
	# The ERP write is irreversible (Smart has no update/delete): the note is saved first and never depends on the
	# audit log; a failing log is reported to the error log, not to the card
	_note(doc, org, outcome["note"])
	frappe.db.commit()
	_safe_log(log, provider, SUCCESS, org, doc, response={k: v for k, v in outcome.items() if k != "note"})
	return outcome


def _safe_log(log, provider, status, org, doc, **kwargs):
	import frappe

	try:
		# Bureau Query keeps the document as digits only (14 characters at most), like the other integrations
		log(provider, "Cedente registration", status, document=digits(org.get("cnpj")), reference=doc, **kwargs)
		frappe.db.commit()
	except Exception:  # noqa: BLE001 - the audit log must never undo or hide what happened in the ERP
		frappe.log_error(title="Cred Hub: ERP cedente registration log")


def _lock_name(frappe) -> str:
	return f"cred_hub_erp_cedente:{frappe.conf.db_name}"[:64]


def _acquire_lock(frappe) -> bool:
	"""MariaDB named lock, waiting up to 2 minutes (a registration takes a few Smart calls at 3 s each)."""
	return bool(frappe.db.sql("select get_lock(%s, 120)", (_lock_name(frappe),))[0][0])


def _release_lock(frappe):
	frappe.db.sql("select release_lock(%s)", (_lock_name(frappe),))


def ensure_enriched(org):
	"""Organizations enriched before credtech#367 lack the address in parts and the IBGE code: fetch the CNPJ again
	(OpenCNPJ/BrasilAPI are free) and fill only those fields. Never touches what the user changed."""
	from cred_hub.cnpj import client as cnpj_client
	from cred_hub.cnpj.mapping import is_valid_cnpj

	if not org.get("cnpj") or not is_valid_cnpj(org.cnpj):
		return
	needed = ("address_street", "ibge_code", "tax_regime")
	if all(org.get(f) for f in needed):
		return
	try:
		data = cnpj_client.fetch(org.cnpj)
	except (cnpj_client.CnpjNotFound, cnpj_client.CnpjUnavailable):
		return
	changed = False
	for field in ("address_street_type", "address_street", "address_number", "address_complement", "ibge_code", "tax_regime"):
		if not org.get(field) and data.get(field):
			org.set(field, data[field])
			changed = True
	if changed:
		org.flags.cred_hub_enriching = True
		org.save(ignore_permissions=True)


def _records(doc, org) -> dict:
	import frappe

	address = frappe.get_doc("Address", org.address).as_dict() if org.get("address") and frappe.db.exists("Address", org.address) else {}
	contact = {}
	primary = next((c.contact for c in doc.get("contacts") or [] if c.get("is_primary")), None)
	primary = primary or next((c.contact for c in doc.get("contacts") or []), None)
	if not primary:
		primary = frappe.db.get_value("Contact", {"company_name": org.name}, "name", order_by="creation asc")
	if primary and frappe.db.exists("Contact", primary):
		contact = frappe.get_doc("Contact", primary).as_dict()
	credit = {}
	committee = frappe.get_all(
		"Cred Hub Credit Committee",
		{"deal": doc.name, "status": ("in", ("Approved", "Approved with Reservations"))},
		["approved_limit", "status"],
		order_by="modified desc",
		limit=1,
	)
	if committee:
		credit = committee[0]
	return {"organization": org.as_dict(), "address": address, "contact": contact, "deal": doc.as_dict(), "credit": credit}


def _build(settings, records):
	from datetime import date

	from .mapper import DEFAULT_RULES, Rule, build

	rows = settings.get("cedente_field_map") or DEFAULT_RULES.get(settings.erp_provider) or []
	return build([Rule.from_row(r) for r in rows], records, today=date.today())


def _send_smart(erp, settings, doc, org, result) -> dict:
	import frappe
	from frappe import _

	from .mapper import finalize

	cnpj = digits(org.cnpj)
	existing = erp.find_cedente(cnpj)
	if existing:
		return {"status": "already", "id": existing.get("ID_CEDENTE"), "note": _(
			"The cedente was already registered in Smart Securities (code {0}). Nothing was sent: the Smart does not "
			"accept updates by API."
		).format(existing.get("ID_CEDENTE") or "-")}
	payload = finalize("Smart Securities", result.payload)
	missing = []
	sector = smart_sector(org.get("main_cnae"), erp.lookup("setor"), parse_overrides(settings.get("sector_by_cnae")))
	if sector:
		payload["idSetor"] = int(sector["idSetor"])
	else:
		missing.append(_("Sector (no Smart sector matches the CNAE; set it in the ERP settings)"))
	owner_name = frappe.db.get_value("User", doc.deal_owner, "full_name") if doc.get("deal_owner") else ""
	manager, source = smart_manager(owner_name, settings.get("default_manager"), erp.lookup("gerente"))
	if manager:
		payload["idGerente"] = int(manager["idGerente"])
	else:
		missing.append(_("Manager (the card owner is not a Smart manager and there is no default manager)"))
	if missing:
		raise _Missing(missing)
	regime = smart_regime(org.get("tax_regime"), erp.lookup("regime-tributario"))
	if regime:
		payload["idRegimeTributario"] = regime["idRegimeTributario"]
	payload["numeroContratoMae"] = erp.next_contract_number()
	answer = erp.create_cedente(payload)
	if answer.get("duplicate"):
		return {"status": "already", "id": None, "note": _(
			"The cedente was already registered in Smart Securities. Nothing was sent."
		)}
	return {
		"status": "created",
		"id": answer.get("id"),
		"manager": manager.get("descricao"),
		"note": _(
			"Cedente registered in Smart Securities. Code: {0} · Manager: {1} ({2}) · Sector: {3} · Tax regime: {4} · "
			"Master contract: {5}."
		).format(
			answer.get("id") or "-",
			manager.get("descricao"),
			_(source),
			sector.get("descricao"),
			(regime or {}).get("descricao") or "-",
			payload["numeroContratoMae"],
		),
	}


def _send_wba(erp, org, result) -> dict:
	from frappe import _

	from .mapper import finalize

	cnpj = digits(org.cnpj)
	existing = erp.find_person(cnpj)
	if existing and "CEDENTE" in (existing.get("tipoCadastro") or []):
		return {"status": "already", "id": existing.get("codigo"), "note": _(
			"The cedente was already registered in WBA (code {0}). Nothing was sent."
		).format(existing.get("codigo") or "-")}
	import frappe

	# WBA answers "Requisição Recebida." and registers later: until it shows up, never send the same CNPJ again
	marker = f"cred_hub_erp_cedente_sent:{cnpj}"
	if frappe.cache.get_value(marker):
		return {"status": "pending", "id": None, "note": _(
			"The cedente was already sent to WBA and is waiting for WBA to register it. Nothing was sent again."
		)}
	erp.send_people(finalize("WBA", result.payload))
	frappe.cache.set_value(marker, 1, expires_in_sec=24 * 3600)
	return {"status": "sent", "id": None, "note": _(
		"Cedente registration sent to WBA. WBA confirms it later; the registration appears in WBA in a few minutes."
	)}


class _Missing(Exception):
	def __init__(self, items):
		super().__init__(", ".join(items))
		self.items = items


def _missing(doc, org, provider, items, errors=()) -> dict:
	from frappe import _

	text = _("Cedente not sent to {0}: missing {1}. Complete it on the company and send again.").format(
		provider, ", ".join(_(i) for i in items) or "-"
	)
	if errors:
		text += " " + _("Field mapping errors: {0}").format("; ".join(errors))
	_note(doc, org, text)
	return {"ok": False, "missing": items, "errors": list(errors)}


def _note(doc, org, text: str):
	"""The card's activity and the company's history both tell what happened with the ERP registration."""
	doc.add_comment("Comment", text)
	org.add_comment("Comment", text)
