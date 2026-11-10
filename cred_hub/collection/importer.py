"""Importação de títulos por planilha para o funil de Cobrança (credtech#315).

Fluxo da tela: o operador sobe uma planilha (.xlsx ou .csv), confere o mapeamento das colunas, vê a prévia (o
que será criado/atualizado e as linhas com problema) e importa. A importação roda em segundo plano e avisa o
operador no fim.

Regras (dono, 02/11/2026):
- CPF vira Contato e CNPJ vira Organização, sempre reaproveitando quem já tem o documento (cred_hub.party);
- o cliente escolhe como agrupar os cards (Configurações › Bureaus): por sacado + cedente (padrão) ou só por
  sacado; o cedente fica em cada título;
- a espécie nunca tem padrão: só entra quando a planilha traz um código que o cartório aceita, senão o título
  entra sem espécie e o operador escolhe antes de protestar;
- título repetido (mesmo número, nosso número e vencimento no card) atualiza o que existe; título que já foi à
  CENPROT não é alterado.
"""

import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import flt, getdate

from cred_hub.cnpj.mapping import format_cnpj, only_digits, parse_brl_number
from cred_hub.integrations import get_settings
from cred_hub.integrations.protest import SENDABLE, row_status
from cred_hub.party.document import CNPJ, CPF, document_kind, format_cpf
from cred_hub.party.resolve import contact_for_cpf, organization_for_cnpj
from cred_hub.party.roles import CEDENTE, DEBTOR, add_role

DEAL, TITLE_ROW, ORG, CONTACT = "CRM Deal", "CRM Deal Title", "CRM Organization", "Contact"
# Campos do título preenchidos pelo sync do ERP (cred_hub.erp.fields)
ERP_FIELDS = ("erp_source", "erp_title_id", "erp_operation", "erp_situation", "erp_amount", "erp_run_id")
MAX_ROWS = 20000
BY_DEBTOR, BY_PAIR = "Debtor", "Debtor and cedente"
EXTENSIONS = ("xlsx", "csv")

# Campo de destino -> (rótulo, obrigatório, cabeçalhos reconhecidos sozinhos; já normalizados)
TARGETS = {
	"debtor_document": ("Debtor CPF or CNPJ", True, ("cpf_cnpj_sacado", "cnpj_sacado", "cpf_sacado", "documento_sacado", "cpf_cnpj_devedor", "cnpj_devedor", "cpf_devedor", "documento_devedor")),
	"debtor_name": ("Debtor name", False, ("sacado", "nome_sacado", "razao_social_sacado", "devedor", "nome_devedor")),
	"cedente_document": ("Cedente CNPJ", False, ("cpf_cnpj_cedente", "cnpj_cedente", "documento_cedente")),
	"cedente_name": ("Cedente name", False, ("cedente", "nome_cedente", "razao_social_cedente")),
	"title_number": ("Title number", True, ("documento", "numero", "numero_titulo", "numero_documento", "titulo", "nf", "duplicata")),
	"our_number": ("Our number", False, ("nosso_numero", "nossonumero")),
	"species": ("Species (CENPROT code)", False, ("especie", "tipo_titulo", "especie_titulo", "tipo")),
	"issue_date": ("Issue date", False, ("data_emissao", "emissao", "dt_emissao")),
	"due_date": ("Due date", True, ("data_vencimento", "vencimento", "dt_vencimento")),
	"original_amount": ("Original amount", True, ("valor_face", "valor_original", "valor", "valor_titulo", "valor_nominal")),
	"updated_amount": ("Updated amount", False, ("valor_em_aberto", "valor_atualizado", "saldo", "valor_total", "valor_corrigido")),
	"digitable_line": ("Digitable line", False, ("linha_digitavel", "codigo_barras", "codigo_de_barras")),
}


def normalize_header(text) -> str:
	text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
	return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def suggest_mapping(headers: list[str]) -> dict:
	"""{campo de destino: índice da coluna} para os cabeçalhos conhecidos."""
	normalized = [normalize_header(h) for h in headers]
	mapping = {}
	for target, (_label, _required, aliases) in TARGETS.items():
		for alias in aliases:
			if alias in normalized and normalized.index(alias) not in mapping.values():
				mapping[target] = normalized.index(alias)
				break
	return mapping


# Leitura do arquivo ------------------------------------------------------------------------------------------


def _file(file_url: str):
	"""O arquivo enviado pelo próprio usuário (ou por um administrador): ninguém lê planilha alheia pela URL."""
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		frappe.throw(_("File not found."))
	file = frappe.get_doc("File", name)
	if file.owner != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw(_("You can only import a file you uploaded."), frappe.PermissionError)
	extension = (file.file_name or file_url).rsplit(".", 1)[-1].lower()
	if extension not in EXTENSIONS:
		frappe.throw(_("Send the spreadsheet as .xlsx or .csv."))
	return file, extension


def read_table(file_url: str) -> tuple[list[str], list[list]]:
	"""(cabeçalhos, linhas) da primeira aba; linhas vazias ficam de fora."""
	file, extension = _file(file_url)
	content = file.get_content()
	if extension == "csv":
		from frappe.utils.csvutils import read_csv_content

		rows = read_csv_content(content, use_sniffer=True)
	else:
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		rows = read_xlsx_file_from_attached_file(fcontent=content if isinstance(content, bytes) else content.encode())
	rows = [row for row in rows or [] if any(str(cell or "").strip() for cell in row)]
	if not rows:
		frappe.throw(_("The spreadsheet is empty."))
	if len(rows) - 1 > MAX_ROWS:
		frappe.throw(_("The spreadsheet has more than {0} lines. Split it in smaller files.").format(MAX_ROWS))
	return [str(h or "").strip() for h in rows[0]], rows[1:]


# Leitura de cada linha ---------------------------------------------------------------------------------------


def _cell(row, mapping, target):
	index = mapping.get(target)
	if index is None or index == "" or int(index) >= len(row):
		return None
	value = row[int(index)]
	return value.strip() if isinstance(value, str) else value


def parse_date(value):
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	text = str(value).strip().split()[0]
	match = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", text)
	if match:
		day, month, year = (int(part) for part in match.groups())
		year += 2000 if year < 100 else 0
		try:
			return date(year, month, day)
		except ValueError:
			return None
	try:
		return getdate(text)
	except Exception:  # noqa: BLE001 - formato desconhecido vira problema da linha
		return None


def _amount(value):
	try:
		return parse_brl_number(value)
	except (TypeError, ValueError):
		return None


def _species_options() -> set[str]:
	options = frappe.get_meta(TITLE_ROW).get_field("species").options or ""
	return {option.strip().upper() for option in options.split("\n") if option.strip()}


def parse_line(row, mapping, species_options) -> tuple[dict | None, list[str], list[str]]:
	"""(título, problemas que impedem a linha, avisos). Avisos não impedem: o operador completa depois."""
	problems, warnings = [], []
	debtor = only_digits(_cell(row, mapping, "debtor_document"))
	debtor_kind = document_kind(debtor)
	if not debtor_kind:
		problems.append(_("invalid debtor CPF/CNPJ ({0})").format(_cell(row, mapping, "debtor_document") or "-"))
	cedente = only_digits(_cell(row, mapping, "cedente_document"))
	if cedente and document_kind(cedente) != CNPJ:
		problems.append(_("invalid cedente CNPJ ({0})").format(_cell(row, mapping, "cedente_document")))
	number = str(_cell(row, mapping, "title_number") or "").strip()
	if not number or number.upper() == "S/N":
		problems.append(_("no title number"))
	due = parse_date(_cell(row, mapping, "due_date"))
	if not due:
		problems.append(_("invalid due date"))
	original = _amount(_cell(row, mapping, "original_amount"))
	if not original or original <= 0:
		problems.append(_("invalid original amount"))
	updated = _amount(_cell(row, mapping, "updated_amount"))
	issue = parse_date(_cell(row, mapping, "issue_date"))
	if not issue:
		warnings.append(_("no issue date (needed to protest)"))
	raw_species = str(_cell(row, mapping, "species") or "").strip().upper()
	code = re.split(r"[\s\-–:/]+", raw_species, maxsplit=1)[0] if raw_species else ""
	species = code if code in species_options else ""
	if not species:
		warnings.append(
			_("species {0} is not a registry code: choose it on the card").format(raw_species)
			if raw_species
			else _("no species: choose it on the card before protesting")
		)
	if problems:
		return None, problems, warnings
	return (
		{
			"debtor_document": debtor,
			"debtor_kind": debtor_kind,
			"debtor_name": str(_cell(row, mapping, "debtor_name") or "").strip(),
			"cedente_document": cedente,
			"cedente_name": str(_cell(row, mapping, "cedente_name") or "").strip(),
			"title_number": number[:140],
			"our_number": str(_cell(row, mapping, "our_number") or "").strip()[:140],
			"species": species,
			"issue_date": issue,
			"due_date": due,
			"original_amount": flt(original, 2),
			"updated_amount": flt(updated, 2) if updated else flt(original, 2),
			"digitable_line": str(_cell(row, mapping, "digitable_line") or "").strip()[:140],
		},
		[],
		warnings,
	)


# Quem é quem e qual card -------------------------------------------------------------------------------------


def _grouping(settings) -> str:
	return settings.get("collection_grouping") or BY_PAIR


def _existing_debtor(document, kind):
	if kind == CPF:
		found = frappe.get_all(CONTACT, {"cpf": ("in", (format_cpf(document), document))}, pluck="name", limit=1)
		return (CONTACT, found[0]) if found else None
	found = frappe.get_all(ORG, {"cnpj": ("in", (format_cnpj(document), document))}, pluck="name", limit=1)
	return (ORG, found[0]) if found else None


def _existing_org(cnpj):
	if not cnpj:
		return None
	found = frappe.get_all(ORG, {"cnpj": ("in", (format_cnpj(cnpj), cnpj))}, pluck="name", limit=1)
	return found[0] if found else None


def _open_stages(pipeline) -> list[str]:
	return frappe.get_all(
		"CRM Deal Status",
		{"pipeline": pipeline, "type": ("not in", ("Won", "Lost"))},
		pluck="name",
		order_by="position asc",
	)


def first_stage(pipeline) -> str:
	"""Etapa de entrada: a primeira do tipo "Open"; sem ela, a primeira em andamento."""
	opened = frappe.get_all(
		"CRM Deal Status", {"pipeline": pipeline, "type": "Open"}, pluck="name", order_by="position asc", limit=1
	)
	stages = opened or _open_stages(pipeline)
	if not stages:
		frappe.throw(_("The collection pipeline has no open stage."))
	return stages[0]


def find_card(pipeline, debtor, cedente_org, grouping) -> str | None:
	"""Card em aberto do devedor (e do cedente, no agrupamento por par) no funil de Cobrança."""
	if not debtor:
		return None
	stages = _open_stages(pipeline)
	if not stages:
		return None
	filters = {"pipeline": pipeline, "status": ("in", stages)}
	# Card ganho ou perdido está fechado (status do card, credtech#315): título novo abre outro card
	if frappe.get_meta(DEAL).has_field("outcome"):
		filters["outcome"] = "In progress"
	if grouping == BY_PAIR:
		filters["second_organization"] = cedente_org or ("is", "not set")
	doctype, name = debtor
	if doctype == ORG:
		filters["organization"] = name
		found = frappe.get_all(DEAL, filters, pluck="name", order_by="creation asc", limit=1)
		return found[0] if found else None
	contacts_table = frappe.get_meta(DEAL).get_field("contacts").options
	parents = frappe.get_all(contacts_table, {"parenttype": DEAL, "contact": name}, pluck="parent")
	if not parents:
		return None
	filters["name"] = ("in", parents)
	filters["organization"] = ("is", "not set")
	found = frappe.get_all(DEAL, filters, pluck="name", order_by="creation asc", limit=1)
	return found[0] if found else None


def _normalized_number(value) -> str:
	"""Title numbers compare without leading zeros when they are only digits ("000123" is "123")."""
	text = str(value or "").strip().upper()
	return (text.lstrip("0") or "0") if text.isdigit() else text


def _title_key(values) -> tuple:
	return (
		_normalized_number(values.get("title_number")),
		_normalized_number(values.get("our_number")),
		str(getdate(values.get("due_date")) if values.get("due_date") else ""),
	)


def _group_key(title, grouping) -> tuple:
	if grouping == BY_DEBTOR:
		return (title["debtor_document"],)
	return (title["debtor_document"], title["cedente_document"])


def parse_file(file_url, mapping) -> dict:
	headers, rows = read_table(file_url)
	mapping = {key: int(value) for key, value in (mapping or {}).items() if value not in (None, "", -1, "-1")}
	missing = [_(spec[0]) for key, spec in TARGETS.items() if spec[1] and key not in mapping]
	if missing:
		frappe.throw(_("Choose the column of: {0}.").format(", ".join(missing)))
	species_options = _species_options()
	titles, problems, warnings = [], [], 0
	for line, row in enumerate(rows, start=2):
		title, row_problems, row_warnings = parse_line(row, mapping, species_options)
		if row_problems:
			problems.append({"line": line, "message": "; ".join(row_problems)})
		if row_warnings:
			warnings += 1
		if title:
			title["line"] = line
			titles.append(title)
	return {"headers": headers, "rows": len(rows), "titles": titles, "problems": problems, "warnings": warnings}


# Telas -------------------------------------------------------------------------------------------------------


def _check_access(pipeline=None):
	settings = get_settings()
	if not settings.get("collection_pipeline"):
		frappe.throw(_("Turn on CENPROT and choose the collection pipeline in Settings > Bureaus first."))
	if pipeline and pipeline != settings.collection_pipeline:
		frappe.throw(_("The import is only for the collection pipeline."))
	frappe.has_permission(DEAL, "create", throw=True)
	frappe.has_permission("CRM Pipeline", "read", settings.collection_pipeline, throw=True)
	return settings


@frappe.whitelist()
def get_context(pipeline: str | None = None) -> dict:
	"""Se a tela mostra o botão de importar neste funil, e o agrupamento escolhido pelo cliente."""
	settings = get_settings()
	collection = settings.get("collection_pipeline")
	allowed = bool(collection) and (not pipeline or pipeline == collection) and bool(
		frappe.has_permission(DEAL, "create")
	)
	return {"enabled": allowed, "pipeline": collection or "", "grouping": _grouping(settings)}


@frappe.whitelist(methods=["POST"])
def columns(file_url: str) -> dict:
	"""Cabeçalhos da planilha, mapeamento sugerido e as 3 primeiras linhas para conferir."""
	_check_access()
	headers, rows = read_table(file_url)
	return {
		"headers": headers,
		"suggested": suggest_mapping(headers),
		"sample": [[str(cell if cell is not None else "") for cell in row] for row in rows[:3]],
		"rows": len(rows),
		"targets": [{"key": key, "label": _(label), "required": required} for key, (label, required, _a) in TARGETS.items()],
	}


@frappe.whitelist(methods=["POST"])
def preview(file_url: str, mapping) -> dict:
	"""O que a importação faria, sem gravar nada."""
	settings = _check_access()
	mapping = frappe.parse_json(mapping)
	parsed = parse_file(file_url, mapping)
	grouping = _grouping(settings)
	pipeline = settings.collection_pipeline
	groups = defaultdict(list)
	for title in parsed["titles"]:
		groups[_group_key(title, grouping)].append(title)
	summary = {"cards_new": 0, "cards_update": 0, "titles_new": 0, "titles_update": 0, "titles_locked": 0}
	people_new, companies_new = set(), set()
	for titles in groups.values():
		first = titles[0]
		debtor = _existing_debtor(first["debtor_document"], first["debtor_kind"])
		if not debtor:
			(people_new if first["debtor_kind"] == CPF else companies_new).add(first["debtor_document"])
		for title in titles:
			if title["cedente_document"] and not _existing_org(title["cedente_document"]):
				companies_new.add(title["cedente_document"])
		cedente_org = _existing_org(first["cedente_document"]) if grouping == BY_PAIR else None
		card = None if (grouping == BY_PAIR and first["cedente_document"] and not cedente_org) else find_card(
			pipeline, debtor, cedente_org, grouping
		)
		existing = {}
		if card:
			summary["cards_update"] += 1
			for row in frappe.get_doc(DEAL, card).get("titles") or []:
				existing[_title_key(row)] = row
		else:
			summary["cards_new"] += 1
		for title in titles:
			row = existing.get(_title_key(title))
			if not row:
				summary["titles_new"] += 1
			elif row_status(row) in SENDABLE:
				summary["titles_update"] += 1
			else:
				summary["titles_locked"] += 1
	return {
		**summary,
		"rows": parsed["rows"],
		"valid": len(parsed["titles"]),
		"problems": parsed["problems"][:100],
		"problem_count": len(parsed["problems"]),
		"warnings": parsed["warnings"],
		"people_new": len(people_new),
		"companies_new": len(companies_new),
		"grouping": grouping,
	}


@frappe.whitelist(methods=["POST"])
def start(file_url: str, mapping) -> dict:
	"""Importa em segundo plano; o operador recebe uma notificação com o resumo."""
	_check_access()
	mapping = frappe.parse_json(mapping)
	parse_file(file_url, mapping)  # erro de mapeamento aparece agora, não no fim
	job = frappe.enqueue(
		"cred_hub.collection.importer.run_import",
		queue="long",
		timeout=3600,
		file_url=file_url,
		mapping=mapping,
		user=frappe.session.user,
		enqueue_after_commit=True,
	)
	return {"ok": True, "job": getattr(job, "id", "")}


# Gravação ----------------------------------------------------------------------------------------------------


def _debtor_for(title) -> tuple[str, str]:
	if title["debtor_kind"] == CPF:
		return CONTACT, contact_for_cpf(title["debtor_document"], title["debtor_name"])
	return ORG, organization_for_cnpj(title["debtor_document"], title["debtor_name"])


def _cedente_for(title) -> str | None:
	if not title["cedente_document"]:
		return None
	return organization_for_cnpj(title["cedente_document"], title["cedente_name"])


def _row_values(title, cedente, debtor=None) -> dict:
	values = {
		"title_number": title["title_number"],
		"our_number": title["our_number"],
		"issue_date": title["issue_date"],
		"due_date": title["due_date"],
		"original_amount": title["original_amount"],
		"updated_amount": title["updated_amount"],
		"cedente": cedente,
	}
	if title["species"]:
		values["species"] = title["species"]
	if title["digitable_line"]:
		values["digitable_line"] = title["digitable_line"]
	# Sacado de cada título (credtech#367): o protesto usa o sacado do próprio título
	if debtor and frappe.get_meta(TITLE_ROW).has_field("sacado"):
		values["sacado"] = debtor[1] if debtor[0] == ORG else None
		values["sacado_contact"] = debtor[1] if debtor[0] == CONTACT else None
	# Origem no ERP (credtech#367): só o sync do ERP manda estes campos
	for fieldname in ERP_FIELDS:
		if title.get(fieldname) not in (None, ""):
			values[fieldname] = title[fieldname]
	return values


def _card_name(debtor, cedente, grouping) -> str:
	debtor_label = frappe.db.get_value(debtor[0], debtor[1], "organization_name" if debtor[0] == ORG else "full_name")
	name = debtor_label or debtor[1]
	if grouping == BY_PAIR and cedente and cedente != debtor[1]:
		name = f"{name} - {frappe.db.get_value(ORG, cedente, 'organization_name') or cedente}"
	return name[:140]


def import_group(pipeline, titles, grouping, stats):
	first = titles[0]
	debtor = _debtor_for(first)
	cedentes = {title["cedente_document"]: _cedente_for(title) for title in titles}
	card_cedente = cedentes.get(first["cedente_document"]) if grouping == BY_PAIR else None
	if card_cedente == debtor[1]:
		card_cedente = None
	name = find_card(pipeline, debtor, card_cedente, grouping)
	if name:
		deal = frappe.get_doc(DEAL, name)
		stats["cards_update"] += 1
	else:
		deal = frappe.new_doc(DEAL)
		deal.update(
			{
				"pipeline": pipeline,
				"status": first_stage(pipeline),
				"deal_name": _card_name(debtor, card_cedente, grouping),
				"organization": debtor[1] if debtor[0] == ORG else None,
				"second_organization": card_cedente,
			}
		)
		if frappe.flags.get("cred_hub_erp_run_id") and frappe.get_meta(DEAL).has_field("erp_run_id"):
			deal.erp_run_id = frappe.flags.cred_hub_erp_run_id
		if debtor[0] == CONTACT:
			deal.append("contacts", {"contact": debtor[1], "is_primary": 1})
		stats["cards_new"] += 1
	# Identity of a título inside the card: the ERP id when it has one (two distinct ERP títulos may share number, our
	# number and due date), otherwise number + our number + due date. A título without an ERP id may adopt a row
	# that has none (the same título coming from a spreadsheet).
	by_erp = {(row.get("erp_source"), str(row.erp_title_id)): row for row in deal.get("titles") or [] if row.get("erp_title_id")}
	by_key = {_title_key(row): row for row in deal.get("titles") or [] if not row.get("erp_title_id")}
	for title in titles:
		title_debtor = debtor if title is first else _debtor_for(title)
		# Papel das organizações (credtech#367), título a título: quem deve é sacado, quem vendeu é cedente. Empresa
		# que consta como cedente da própria dívida fica só sacado.
		if title_debtor[0] == ORG:
			add_role(title_debtor[1], DEBTOR)
		title_cedente = cedentes.get(title["cedente_document"])
		if title_cedente and title_cedente != title_debtor[1]:
			add_role(title_cedente, CEDENTE)
		values = _row_values(title, title_cedente, title_debtor)
		erp_id = (values.get("erp_source"), str(values["erp_title_id"])) if values.get("erp_title_id") else None
		row = (by_erp.get(erp_id) if erp_id else None) or by_key.get(_title_key(values))
		if not row:
			row = deal.append("titles", values)
			if erp_id:
				by_erp[erp_id] = row
			else:
				by_key[_title_key(values)] = row
			stats["titles_new"] += 1
		elif row_status(row) in SENDABLE:
			# só o que veio preenchido: a planilha nova nunca apaga o que o operador já completou (ex.: espécie)
			row.update({k: v for k, v in values.items() if v not in (None, "")})
			stats["titles_update"] += 1
		else:
			stats["titles_locked"] += 1
	if deal.is_new():
		deal.insert(ignore_permissions=True)
	else:
		deal.save(ignore_permissions=True)


def run_import(file_url, mapping, user):
	frappe.set_user(user)
	settings = get_settings()
	grouping = _grouping(settings)
	pipeline = settings.collection_pipeline
	parsed = parse_file(file_url, mapping)
	groups = defaultdict(list)
	for title in parsed["titles"]:
		groups[_group_key(title, grouping)].append(title)
	stats = {"cards_new": 0, "cards_update": 0, "titles_new": 0, "titles_update": 0, "titles_locked": 0, "failed": 0}
	failures = []
	for index, titles in enumerate(groups.values(), start=1):
		frappe.db.savepoint("cred_hub_collection_import")
		try:
			import_group(pipeline, titles, grouping, stats)
		except Exception as e:  # noqa: BLE001 - um card com erro não para a importação
			frappe.db.rollback(save_point="cred_hub_collection_import")
			frappe.clear_messages()
			stats["failed"] += 1
			failures.append(_("lines {0}: {1}").format(", ".join(str(t["line"]) for t in titles[:5]), e))
		if index % 50 == 0:
			frappe.db.commit()
			frappe.publish_realtime(
				"cred_hub_collection_import", {"done": index, "total": len(groups)}, user=user
			)
	frappe.db.commit()
	text = _(
		"Import of titles finished: {0} new cards, {1} updated cards, {2} new titles, {3} updated titles, "
		"{4} titles already at CENPROT left as they were, {5} lines with problems, {6} cards with errors."
	).format(
		stats["cards_new"], stats["cards_update"], stats["titles_new"], stats["titles_update"],
		stats["titles_locked"], len(parsed["problems"]), stats["failed"],
	)
	frappe.get_doc(
		{
			"doctype": "CRM Notification",
			"from_user": user,
			"to_user": user,
			"type": "Automation",
			"notification_text": text,
			"message": "<p>" + frappe.utils.escape_html(text) + "</p>"
			+ "".join(f"<p>{frappe.utils.escape_html(f)}</p>" for f in failures[:20]),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.publish_realtime("cred_hub_collection_import", {"finished": True, **stats}, user=user)
	return stats
