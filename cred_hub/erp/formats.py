"""Pure format helpers shared by the ERP clients and the field-mapping engine (credtech#367).

No Frappe imports: everything here runs under plain pytest.
"""

import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NULL_DATES = ("00/00/0000", "0000-00-00", "")


def digits(value) -> str:
	return re.sub(r"\D", "", str(value if value is not None else ""))


def to_decimal(value) -> Decimal | None:
	"""Money from either ERP: 1234.5, "1234.56", "1.234,56", "4471.18", "R$ 10,00". None when empty/invalid."""
	if value is None or value == "":
		return None
	if isinstance(value, Decimal):
		return value
	if isinstance(value, (int, float)):
		return Decimal(str(value))
	text = str(value).strip().replace("R$", "").replace(" ", "")
	if not text:
		return None
	if "," in text:
		# BR format: dot = thousands, comma = decimals
		text = text.replace(".", "").replace(",", ".")
	elif text.count(".") > 1 or re.fullmatch(r"-?[1-9]\d{0,2}\.\d{3}", text):
		# "1.234.567" or "1.500": BR thousands with no decimals (money never comes with 3 decimals)
		text = text.replace(".", "")
	try:
		return Decimal(text)
	except InvalidOperation:
		return None


def br_money(value) -> str | None:
	"""Decimal-like -> "1.234,56" (Smart Securities POST format)."""
	amount = to_decimal(value)
	if amount is None:
		return None
	amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	sign = "-" if amount < 0 else ""
	whole, cents = f"{abs(amount):.2f}".split(".")
	groups = []
	while len(whole) > 3:
		groups.insert(0, whole[-3:])
		whole = whole[:-3]
	groups.insert(0, whole)
	return f"{sign}{'.'.join(groups)},{cents}"


def to_date(value) -> date | None:
	"""Accepts date/datetime, "dd/mm/yyyy", "dd/mm/yyyy hh:mm:ss", "yyyy-mm-dd". None for the ERPs' null dates."""
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	text = str(value).strip()
	if text in NULL_DATES:
		return None
	# Only the date part matters: "dd/mm/yyyy hh:mm:ss" and ISO datetimes start with the 10-char date
	for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
		try:
			return datetime.strptime(text[:10], fmt).date()
		except ValueError:
			continue
	return None


def br_date(value) -> str | None:
	parsed = to_date(value)
	return parsed.strftime("%d/%m/%Y") if parsed else None


def iso_date(value) -> str | None:
	parsed = to_date(value)
	return parsed.isoformat() if parsed else None


def split_phone(value) -> tuple[str, str]:
	"""Brazilian phone -> (ddd, number) as digit strings. Drops the 55 country code. ("", "") when unusable."""
	number = digits(value)
	if number.startswith("55") and len(number) in (12, 13):
		number = number[2:]
	number = number.lstrip("0") if len(number) > 11 else number
	if len(number) < 10:
		return "", number
	return number[:2], number[2:]


def ascii_text(value) -> str:
	"""Strip accents and every non-alphanumeric char (Smart rejects punctuation in addresses)."""
	text = re.sub(r"\([^)]*\)", " ", str(value or ""))  # drop "(...)" groups before anything else
	text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
	text = re.sub(r"[^A-Za-z0-9 ]+", " ", text)
	return re.sub(r"\s+", " ", text).strip()


def street_name(value) -> str:
	""""Rua das Flores, 100" -> "Rua das Flores" (Frappe Address.address_line1 keeps the number after a comma)."""
	text = str(value or "").strip()
	head, sep, tail = text.rpartition(",")
	return head.strip() if sep and digits(tail) else text


def street_number(value) -> str:
	""""Rua das Flores, 100" -> "100"; "" when there is no number."""
	text = str(value or "").strip()
	_head, sep, tail = text.rpartition(",")
	tail = tail.strip()
	return tail if sep and digits(tail) else ""


def person_type(document) -> str:
	"""CPF (11 digits) -> "PF"; anything else with digits -> "PJ"."""
	return "PF" if len(digits(document)) == 11 else "PJ"


def normalize_key(key) -> str:
	"""Smart sends the same column with varying accents/case ("E-mail", "E-Mail", "Email") -> "email"."""
	text = unicodedata.normalize("NFKD", str(key)).encode("ascii", "ignore").decode("ascii")
	return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def normalize_keys(row: dict) -> dict:
	return {normalize_key(k): v for k, v in (row or {}).items()}
