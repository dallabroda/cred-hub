import ast
import re
from pathlib import Path

PO = Path(__file__).resolve().parents[1] / "locale" / "pt_BR.po"
PLACEHOLDER = re.compile(r"\{\d*\}|%\d*\$?[sd]")


def _entries():
	entries, msgid = [], None
	for line in PO.read_text(encoding="utf-8").splitlines():
		if line.startswith("msgid "):
			msgid = ast.literal_eval(line[6:])
		elif line.startswith("msgstr ") and msgid:
			entries.append((msgid, ast.literal_eval(line[7:])))
			msgid = None
	return entries


def test_po_has_entries():
	# Only this app's own texts: the CRM texts are translated in the crm app (dallabroda/crm#13),
	# and repeating them here would override its wording (cred_hub loads after crm)
	assert len(_entries()) > 100


def test_placeholders_preserved():
	for msgid, msgstr in _entries():
		assert sorted(PLACEHOLDER.findall(msgid)) == sorted(PLACEHOLDER.findall(msgstr)), msgid


def test_no_portugal_portuguese():
	text = " ".join(msgstr for _, msgstr in _entries())
	for word in ("Utilizador", "Contactos", "Eliminar", "Tem a certeza"):
		assert word not in text
