"""Descoberta de site oficial, LinkedIn e nº de funcionários de uma empresa. Sem Frappe, para poder testar isolado.

Cascata:
  1. Wikidata (P856 site, P4264 LinkedIn, P1128 funcionários) - só entidade com site oficial.
  2. Busca web -> domínio oficial (descarta diretórios e redes sociais).
  3. Link do LinkedIn no HTML do site (garante que não é homônimo); senão busca no LinkedIn.
  4. Funcionários: contagem Wikidata/web (porte grande) > piso SEBRAE por CNPJ. LinkedIn desligado (USE_LINKEDIN_EMPLOYEES).
"""

import re
import time
import unicodedata
from urllib.parse import unquote, urlparse

import requests

BLACKLIST = {
	"reclameaqui.com.br", "cnpj.biz", "econodata.com.br", "cnpja.com", "casadosdados.com.br",
	"informecadastral.com.br", "empresaqui.com.br", "cnpj.info", "consultasocio.com", "serasaexperian.com.br",
	"instagram.com", "facebook.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
	"pinterest.com", "wikipedia.org", "glassdoor.com", "glassdoor.com.br", "indeed.com", "catho.com.br",
	"infojobs.com.br", "vagas.com.br", "gupy.io", "crunchbase.com", "bloomberg.com", "yelp.com",
	"tripadvisor.com", "tripadvisor.com.br", "google.com", "bing.com", "mercadolivre.com.br", "amazon.com.br",
	"apontador.com.br", "telelistas.net", "guiamais.com.br", "solutudo.com.br", "jusbrasil.com.br",
	"cnpj.services", "receitafederal.gov.br", "opencorporates.com", "zoominfo.com", "dnb.com", "kompass.com",
	"lusha.com", "rocketreach.co", "apollo.io", "cylex.com.br", "restaurantguru.com.br",
}
# Sufixos de dois níveis comuns; o resto assume TLD simples (evita depender de tldextract)
_TWO_LEVEL = {"com", "org", "net", "gov", "edu", "ind", "adv", "eco", "art", "blog", "co"}

LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9%_\-\.]+)", re.I)
UA = {
	"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
	"Accept-Language": "pt-BR,pt;q=0.9",
}
TIMEOUT = 12
# Nº de funcionários lido do LinkedIn (página pública e faixa nos resultados): desligado por decisão do dono
USE_LINKEDIN_EMPLOYEES = False

# Faixas do campo "no_of_employees" do Frappe CRM
CRM_BUCKETS = [(10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"), (1000, "501-1000")]
CRM_LAST_BUCKET = "1000+"

_NUM = r"(\d{1,3}(?:[.,]\d{3})+|\d{2,7})"
HEADCOUNT_RES = [
	re.compile(_NUM + r"\s+(?:total\s+|full[- ]time\s+)?(?:employees|funcion[aá]rios|colaboradores|empregados)", re.I),
	re.compile(r"(?:employees|funcion[aá]rios|colaboradores)\s*(?:[:\-–]|total of|of)\s*" + _NUM, re.I),
]
RANGE_RE = re.compile(
	r"(\d[\d.,]*\s*[-–]\s*\d[\d.,]*|\d[\d.,]*\s*\+)\s*(?:employees|funcion[aá]rios|colaboradores)", re.I
)
_STOP = {"ltda", "sa", "me", "epp", "eireli", "de", "da", "do", "e", "the", "inc", "cia"}


# --- utilidades puras ---------------------------------------------------------------------------


def registered_domain(url: str) -> str:
	host = (urlparse(url if "//" in url else f"https://{url}").hostname or "").lower()
	parts = host.split(".")
	if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _TWO_LEVEL:
		return ".".join(parts[-3:])
	return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_blacklisted(url: str) -> bool:
	return registered_domain(url) in BLACKLIST


def root_url(url: str) -> str:
	scheme = urlparse(url if "//" in url else f"https://{url}").scheme or "https"
	return f"{scheme}://{registered_domain(url)}/"


def clean_linkedin(url: str) -> str:
	m = LINKEDIN_RE.search(url)
	if not m:
		return ""
	slug = unquote(m.group(1)).strip("/.")
	return f"https://www.linkedin.com/company/{slug}" if slug else ""


def name_words(name: str) -> list[str]:
	return [w for w in re.sub(r"[^\w ]", " ", name.lower()).split() if len(w) >= 3 and w not in _STOP]


def slug_matches(url: str, name: str) -> bool:
	"""Evita homônimo: o slug do LinkedIn achado pela busca precisa conter TODAS as palavras relevantes do nome.
	No teste real (24/10/2026) "nima-credtech" e "credtech-solutions" apareceram para a "Credtech Inteligência
	de Negócios": melhor não preencher do que preencher com outra empresa. O LinkedIn linkado no próprio site
	da empresa ou no Wikidata não passa por esta regra."""
	m = LINKEDIN_RE.search(url)
	if not m:
		return False
	slug = re.sub(r"[^a-z0-9]", "", _ascii(unquote(m.group(1))))
	words = [re.sub(r"[^a-z0-9]", "", _ascii(w)) for w in name_words(name)]
	words = [w for w in words if w]
	if not words:
		return False
	return all(w in slug for w in words)


def _ascii(text: str) -> str:
	"""boticário -> boticario (o slug do LinkedIn não tem acento)."""
	return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def to_crm_bucket(n: int) -> str:
	for limit, label in CRM_BUCKETS:
		if n <= limit:
			return label
	return CRM_LAST_BUCKET


def parse_range(text: str) -> str:
	"""'51-200 employees' / '10,001+ funcionários' -> '51-200' / '10001+'."""
	m = RANGE_RE.search(text or "")
	return re.sub(r"[\s.,]", "", m.group(1)).replace("–", "-") if m else ""


def range_to_crm_bucket(faixa: str) -> str:
	"""Faixa textual ('201-500', '10001+') -> opção do CRM, pelo limite inferior."""
	m = re.match(r"(\d+)", faixa or "")
	return to_crm_bucket(int(m.group(1))) if m else ""


def parse_headcounts(text: str) -> list[int]:
	out = []
	for rx in HEADCOUNT_RES:
		for m in rx.finditer(text or ""):
			n = int(re.sub(r"[.,]", "", m.group(1)))
			if 2 <= n <= 3_000_000:
				out.append(n)
	return out


def sebrae_range(porte: str, cnae: str, opcao_mei: bool = False) -> str:
	"""Piso/faixa SEBRAE a partir do porte da Receita e da divisão do CNAE."""
	porte = (porte or "").upper()
	prefix = int(cnae[:2]) if len(cnae) >= 2 and cnae[:2].isdigit() else 0
	industria = 5 <= prefix <= 33 or 41 <= prefix <= 43
	if opcao_mei or "MEI" in porte:
		return "1 (MEI)"
	if "MICRO" in porte or porte == "ME":
		return "1-19" if industria else "1-9"
	if "PEQUENO" in porte or porte == "EPP":
		return "20-99" if industria else "10-49"
	if "DEMAIS" in porte:
		return "100+" if industria else "50+"
	return ""


# Faixas SEBRAE que cabem sem ambiguidade numa opção do CRM
SEBRAE_TO_CRM = {"1 (MEI)": "1-10", "1-9": "1-10", "10-49": "11-50"}


# --- busca --------------------------------------------------------------------------------------


class Searcher:
	"""DuckDuckGo por padrão; SearXNG se `searxng` for informado (escala sem bloqueio)."""

	def __init__(self, searxng: str | None = None, delay: float = 0.5, retries: int = 2, session=None):
		self.searxng = searxng.rstrip("/") if searxng else None
		self.delay = delay
		self.retries = retries
		self.http = session or requests.Session()
		self.http.headers.update(UA)

	def hits(self, query: str, n: int = 8) -> list[dict]:
		"""Lista de {url, text} (text = título + snippet)."""
		for attempt in range(self.retries):
			try:
				found = self._searxng(query, n) if self.searxng else self._ddg(query, n)
				time.sleep(self.delay)
				return [h for h in found if h["url"]]
			except Exception as e:
				if "No results" in str(e):
					return []
				time.sleep(self.delay * (2**attempt) + 1)
		return []

	def _ddg(self, query, n):
		from ddgs import DDGS

		return [
			{"url": r.get("href") or r.get("url") or "", "text": f"{r.get('title', '')} {r.get('body', '')}"}
			for r in DDGS().text(query, max_results=n)
		]

	def _searxng(self, query, n):
		r = self.http.get(f"{self.searxng}/search", params={"q": query, "format": "json"}, timeout=TIMEOUT)
		r.raise_for_status()
		return [
			{"url": x.get("url", ""), "text": f"{x.get('title', '')} {x.get('content', '')}"}
			for x in r.json().get("results", [])
		][:n]


# --- fontes -------------------------------------------------------------------------------------


def _claim(claims, prop):
	try:
		return claims[prop][0]["mainsnak"]["datavalue"]["value"]
	except (KeyError, IndexError):
		return None


def wikidata(http, name: str) -> dict:
	"""{site, linkedin, funcionarios} da entidade que tem site oficial (descarta homônimos)."""
	api = "https://www.wikidata.org/w/api.php"
	try:
		found = http.get(
			api,
			params={"action": "wbsearchentities", "search": name, "language": "pt", "type": "item", "limit": 3, "format": "json"},
			timeout=TIMEOUT,
		).json()
		for hit in found.get("search", []):
			claims = (
				http.get(api, params={"action": "wbgetentities", "ids": hit["id"], "props": "claims", "format": "json"}, timeout=TIMEOUT)
				.json()["entities"][hit["id"]]
				.get("claims", {})
			)
			site = _claim(claims, "P856")
			if not site:
				continue
			li = _claim(claims, "P4264")
			emp = _claim(claims, "P1128")
			return {
				"site": root_url(str(site)),
				"linkedin": f"https://www.linkedin.com/company/{li}" if li else "",
				"funcionarios": int(float(emp["amount"])) if isinstance(emp, dict) and emp.get("amount") else 0,
			}
	except Exception:
		pass
	return {}


def find_site(searcher: Searcher, name: str) -> str:
	for q in (f'"{name}" site oficial', f"{name} site oficial"):
		for h in searcher.hits(q):
			if not is_blacklisted(h["url"]):
				return root_url(h["url"])
	return ""


def linkedin_from_site(http, site: str) -> str:
	try:
		html = http.get(site, timeout=TIMEOUT).text
	except Exception:
		return ""
	for m in LINKEDIN_RE.finditer(html):
		if m.group(1).lower() not in {"share", "sharearticle"}:
			return clean_linkedin(m.group(0))
	return ""


def linkedin_search(searcher: Searcher, name: str, known: str = "") -> tuple[str, str]:
	"""(url, faixa em texto) pela busca. Com `known`, só aceita resultado dessa mesma página."""
	known_slug = LINKEDIN_RE.search(known).group(1).lower() if known else ""
	url_found = ""
	for q in (f'site:linkedin.com/company/ "{name}"', f"{name} linkedin employees", f"{name} linkedin company"):
		for h in searcher.hits(q):
			m = LINKEDIN_RE.search(h["url"])
			if not m:
				continue
			ok = m.group(1).lower() == known_slug if known else slug_matches(h["url"], name)
			if not ok:
				continue
			url_found = url_found or clean_linkedin(h["url"])
			faixa = parse_range(h["text"])
			if faixa:
				return url_found, faixa
	return url_found, ""


def linkedin_page_employees(http, linkedin: str) -> int:
	"""nº de funcionários da página pública (JSON-LD), sem login. 0 se o LinkedIn bloquear."""
	try:
		r = http.get(linkedin, timeout=TIMEOUT)
		if r.status_code != 200 or "linkedin.com/company/" not in r.url:
			return 0
		m = re.search(r'"numberOfEmployees"\s*:\s*\{\s*"value"\s*:\s*(\d+)', r.text)
		return int(m.group(1)) if m else 0
	except Exception:
		return 0


def web_headcount(searcher: Searcher, name: str) -> int:
	"""Mediana das contagens achadas em snippets que citam a empresa. 0 se nada."""
	words = name_words(name)
	counts: list[int] = []
	for q in (f'"{name}" número de funcionários', f'"{name}" number of employees'):
		for h in searcher.hits(q, 6):
			text = h["text"].lower()
			if not words or not any(w in text or w in h["url"].lower() for w in words):
				continue
			counts += parse_headcounts(h["text"])
		if len(counts) >= 2:
			break
	counts.sort()
	return counts[len(counts) // 2] if counts else 0


# --- orquestrador -------------------------------------------------------------------------------


def _safe(fn, default, *args):
	"""Cada etapa é bônus: se falhar, segue sem ela."""
	try:
		return fn(*args)
	except Exception:
		return default


def discover(name: str, porte: str = "", cnae: str = "", opcao_mei: bool = False, searcher: Searcher | None = None) -> dict:
	"""Devolve {site, linkedin, funcionarios (int|0), faixa (texto), crm_bucket, fonte}. Campos vazios se não achou.
	Nunca levanta exceção: o que falhar fica em branco.

	`porte`/`cnae` (da Receita) alimentam o piso SEBRAE quando nenhuma fonte dá o número."""
	s = searcher or Searcher()
	out = {"site": "", "linkedin": "", "funcionarios": 0, "faixa": "", "crm_bucket": "", "fonte": ""}
	wd = _safe(wikidata, {}, s.http, name)
	out["site"] = wd.get("site") or _safe(find_site, "", s, name)
	out["linkedin"] = wd.get("linkedin") or (_safe(linkedin_from_site, "", s.http, out["site"]) if out["site"] else "")

	# Só o endereço do perfil (vem da busca na web, sem acessar o LinkedIn). Funcionários pelo LinkedIn ficam
	# desligados por decisão do dono (24/10/2026): a página deles bloqueia acesso automático e os termos de uso não
	# permitem. Para religar: USE_LINKEDIN_EMPLOYEES = True.
	url, faixa_snippet = _safe(linkedin_search, ("", ""), s, name, out["linkedin"])
	out["linkedin"] = out["linkedin"] or url
	if USE_LINKEDIN_EMPLOYEES:
		n = _safe(linkedin_page_employees, 0, s.http, out["linkedin"]) if out["linkedin"] else 0
		if n:
			return _with_count(out, n, "LinkedIn")
		if faixa_snippet:
			out.update(faixa=faixa_snippet, crm_bucket=range_to_crm_bucket(faixa_snippet), fonte="LinkedIn")
			return out

	# Sem LinkedIn: empresa de porte grande ("DEMAIS") ou desconhecido ainda tem contagem pública
	if not porte or "DEMAIS" in porte.upper():
		if wd.get("funcionarios"):
			return _with_count(out, wd["funcionarios"], "Wikidata")
		n = _safe(web_headcount, 0, s, name)
		if n:
			return _with_count(out, n, "Web")

	faixa = sebrae_range(porte, cnae, opcao_mei)
	if faixa:
		out.update(faixa=faixa, crm_bucket=SEBRAE_TO_CRM.get(faixa, ""), fonte="SEBRAE/CNPJ")
	return out


def _with_count(out, n, fonte):
	out.update(funcionarios=n, faixa=str(n), crm_bucket=to_crm_bucket(n), fonte=fonte)
	return out
