"""Confiança de que o CNPJ achado é a empresa do lead (credtech#185). Sem Frappe.

- alta: CNPJ ativo na Receita e a razão social ou o nome fantasia lembram o domínio do e-mail;
- média: ativo, e o nome só bate com o "Nome da empresa" que o lead digitou (ou bate só em parte com o domínio);
- baixa: o nome não lembra o domínio nem o formulário (ex.: domínio registrado por uma agência) ou o CNPJ não
  está ativo. Baixa não é aplicada.
"""

import re
import unicodedata

from cred_hub.company.discover import registered_domain

HIGH, MEDIUM, LOW = "high", "medium", "low"
ACTIVE = "ATIVA"
_STOP = {
	"ltda", "sa", "s", "a", "me", "epp", "eireli", "de", "da", "do", "das", "dos", "e", "the", "inc", "cia",
	"comercio", "servicos", "industria", "brasil", "grupo", "holding", "participacoes", "empresa", "companhia",
	"tecnologia", "solucoes", "consultoria", "com", "br", "www", "net",
}
_MIN_TOKEN = 4
# Nome dentro do domínio (ou o contrário) só bate inteiro se cobrir pelo menos metade: "casa" em casadoscelulares não
_MIN_COVER = 0.5
# Só os sufixos jurídicos saem da junção de palavras seguidas ("Casa do Pão Ltda" -> casadopao)
_LEGAL_SUFFIXES = {"ltda", "sa", "s", "a", "me", "epp", "eireli", "mei", "cia"}


def _plain(text: str | None) -> str:
	text = unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode()
	return re.sub(r"[^a-z0-9 ]", " ", text)


def name_tokens(name: str | None) -> list[str]:
	return [w for w in _plain(name).split() if len(w) >= _MIN_TOKEN and w not in _STOP]


def _runs(name: str | None) -> set[str]:
	"""Junções de palavras seguidas do nome: "padaria sao joao" -> padariasao, padariasaojoao, saojoao..."""
	words = [w for w in _plain(name).split() if w not in _LEGAL_SUFFIXES]
	return {"".join(words[i:j]) for i in range(len(words)) for j in range(i + 2, len(words) + 1)}


def domain_label(domain: str | None) -> str:
	"""Parte do domínio que vira nome: `vendas.magazine-luiza.com.br` -> `magazineluiza`."""
	label = registered_domain(domain or "").split(".")[0]
	return re.sub(r"[^a-z0-9]", "", label)


def matches_domain(names: list[str], domain: str | None) -> str | None:
	"""full: o nome inteiro (sem palavras genéricas) está no domínio, ou o domínio está no nome; partial: só uma palavra."""
	label = domain_label(domain)
	if len(label) < 3:
		return None
	best = None
	for name in names:
		tokens = name_tokens(name)
		if not tokens:
			continue
		joined = "".join(tokens)
		if joined == label:
			return "full"
		inside = len(label) >= _MIN_TOKEN and (label in joined or joined in label)
		if inside and min(len(label), len(joined)) / max(len(label), len(joined)) >= _MIN_COVER:
			return "full"
		if label in _runs(name):
			return "full"
		# Sigla: "Credtech Inteligencia de Negocios" -> credtech é palavra inteira do nome
		if any(t == label or t in label for t in tokens):
			best = "partial" if best is None else best
	return best


def matches_company_name(names: list[str], company_name: str | None) -> bool:
	typed = set(name_tokens(company_name))
	return bool(typed) and any(typed & set(name_tokens(name)) for name in names)


def confidence(company: dict, domain: str | None, company_name: str | None = None) -> str:
	"""`company` no formato de cnpj.mapping (legal_name, trade_name, registration_status)."""
	if (company.get("registration_status") or "").upper() != ACTIVE:
		return LOW
	names = [company.get("legal_name"), company.get("trade_name")]
	by_domain = matches_domain(names, domain)
	if by_domain == "full":
		return HIGH
	if by_domain == "partial" or matches_company_name(names, company_name):
		return MEDIUM
	return LOW
