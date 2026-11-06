"""Cliente HTTP para o Portal da Transparência (CGU) — CEIS e CNEP.

Etapa 0 da esteira: grátis, roda antes dos provedores pagos. Se a empresa já consta sancionada
e o cliente configurou `block_on_ceis_cnep`, a esteira reprova na hora e nem chama Escavador/
Infosimples/DirectD — economiza o custo pago inteiro.

Fonte: Portal da Transparência (CGU — Controladoria-Geral da União), portaldatransparencia.gov.br.
- CEIS: Cadastro de Empresas Inidôneas e Suspensas.
- CNEP: Cadastro Nacional de Empresas Punidas.
Chave grátis via cadastro em portaldatransparencia.gov.br/api-de-dados, enviada no header
`chave-api-dados`. Documentação confirmada pelo dono (30/10/2026): parâmetros e exemplo de
resposta reais, não adivinhados.

Regra de segurança desta esteira (exceção deliberada ao fail-closed): se a API do governo cair ou
estiver mal configurada, o campo fica "não verificado" — nunca trava o resto da esteira nem vira
'Error' geral, porque é a única consulta grátis e best-effort do pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from cred_hub.cnpj.mapping import only_digits
from cred_hub.credit_check.raw_capture import record

DEFAULT_BASE_URL = "https://api.portaldatransparencia.gov.br"
DEFAULT_TIMEOUT = 20


class CGUError(Exception):
	"""Erro base na comunicação com o Portal da Transparência."""


class CGUAuthError(CGUError):
	"""Chave de API inválida ou não informada (401/403)."""


def _get_cgu(endpoint: str, digits: str, api_key: str, base_url: str, timeout: int) -> list[dict[str, Any]]:
	if not api_key or not api_key.strip():
		raise CGUAuthError("Chave de API do Portal da Transparência não informada.")

	url = f"{base_url.rstrip('/')}/api-de-dados/{endpoint.lstrip('/')}"
	headers = {
		"chave-api-dados": api_key.strip(),
		"Accept": "application/json",
	}
	params = {"codigoSancionado": digits, "pagina": 1}

	try:
		response = requests.get(url, headers=headers, params=params, timeout=timeout)
	except requests.RequestException as e:
		raise CGUError(f"Falha de rede ao consultar {endpoint} no Portal da Transparência: {e}") from e

	if response.status_code in (401, 403):
		raise CGUAuthError(f"Chave do Portal da Transparência não autorizada para {endpoint}.")
	if not response.ok:
		raise CGUError(f"Erro {response.status_code} no Portal da Transparência ({endpoint}): {response.text[:300]}")

	try:
		data = response.json()
	except ValueError as e:
		raise CGUError(f"Resposta não-JSON do Portal da Transparência ({endpoint}): {response.text[:200]}") from e

	record("Portal da Transparência", endpoint, data)
	return data if isinstance(data, list) else []


def _parse_sanction(item: dict[str, Any]) -> dict[str, Any]:
	tipo = item.get("tipoSancao") or {}
	orgao = item.get("orgaoSancionador") or {}
	return {
		"tipo": tipo.get("descricaoResumida") or tipo.get("descricaoPortal") or "",
		"orgao": orgao.get("nome") or "",
		"data_inicio": item.get("dataInicioSancao") or "",
		"data_fim": item.get("dataFimSancao") or "",
		"texto_publicacao": item.get("textoPublicacao") or "",
		"link_publicacao": item.get("linkPublicacao") or "",
		"numero_processo": item.get("numeroProcesso") or "",
	}


def fetch_ceis_cnep(cnpj: str, api_key: str, base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Consulta CEIS e CNEP pelo CNPJ. Qualquer registro encontrado (ativo ou já encerrado) conta
	como sancionado — decisão conservadora: histórico de sanção é dado relevante pra crédito mesmo
	expirado, e o payload bruto fica disponível pra quem quiser julgar caso a caso."""
	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta CEIS/CNEP: {cnpj}")

	ceis = _get_cgu("ceis", digits, api_key, base_url, timeout)
	cnep = _get_cgu("cnep", digits, api_key, base_url, timeout)

	sanctions = [_parse_sanction(i) for i in ceis] + [_parse_sanction(i) for i in cnep]

	return {
		"sanctioned": bool(sanctions),
		"ceis_count": len(ceis),
		"cnep_count": len(cnep),
		"sanctions": sanctions,
		"raw": {"ceis": ceis, "cnep": cnep},
	}


def fetch_ceis_cnep_open_data(cnpj: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
	"""Fallback oficial sem necessidade de chave de API: consulta a base diária de Dados Abertos da CGU
	(https://dadosabertos-download.cgu.gov.br/PortalDaTransparencia/saida/{ceis|cnep}/{YYYYMMDD}_{CEIS|CNEP}.zip),
	mantendo índice compacto em disco (/tmp) por 24h para resposta instantânea (< 1ms após o download diário)."""
	import csv
	import datetime
	import io
	import json
	import tempfile
	import zipfile

	digits = only_digits(cnpj)
	if not digits or len(digits) != 14:
		raise ValueError(f"CNPJ inválido para consulta CEIS/CNEP: {cnpj}")

	today_utc = datetime.datetime.now(datetime.timezone.utc).date()
	cache_dir = Path(tempfile.gettempdir())
	cache_file = cache_dir / f"cred_hub_cgu_open_data_{today_utc.strftime('%Y%m%d')}.json"

	index: dict[str, dict[str, list[dict[str, Any]]]] | None = None
	if cache_file.exists():
		try:
			index = json.loads(cache_file.read_text(encoding="utf-8"))
		except Exception:
			index = None

	if index is None:
		index = {"ceis": {}, "cnep": {}}
		for kind in ("ceis", "cnep"):
			loaded = False
			for delta in range(0, 6):
				d = (today_utc - datetime.timedelta(days=delta)).strftime("%Y%m%d")
				url = f"https://dadosabertos-download.cgu.gov.br/PortalDaTransparencia/saida/{kind}/{d}_{kind.upper()}.zip"
				try:
					r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
					if r.status_code != 200:
						continue
					z = zipfile.ZipFile(io.BytesIO(r.content))
					with z.open(z.namelist()[0]) as raw_csv:
						text_stream = io.TextIOWrapper(raw_csv, encoding="latin-1", errors="replace")
						reader = csv.DictReader(text_stream, delimiter=";")
						for row in reader:
							doc_digits = only_digits(row.get("CPF OU CNPJ DO SANCIONADO") or "")
							if len(doc_digits) != 14:
								continue
							item = {
								"tipo": (row.get("CATEGORIA DA SANÇÃO") or "").strip(),
								"orgao": (row.get("ÓRGÃO SANCIONADOR") or "").strip(),
								"data_inicio": (row.get("DATA INÍCIO SANÇÃO") or "").strip(),
								"data_fim": (row.get("DATA FINAL SANÇÃO") or "").strip(),
								"texto_publicacao": (row.get("PUBLICAÇÃO") or "").strip(),
								"link_publicacao": "",
								"numero_processo": (row.get("NÚMERO DO PROCESSO") or "").strip().strip("'"),
							}
							index[kind].setdefault(doc_digits, []).append(item)
					loaded = True
					break
				except Exception:
					continue
			if not loaded:
				raise CGUError(f"Não foi possível baixar a base pública {kind.upper()} do Portal da Transparência.")
		try:
			cache_file.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
		except Exception:
			pass

	ceis_list = index.get("ceis", {}).get(digits, [])
	cnep_list = index.get("cnep", {}).get(digits, [])
	sanctions = ceis_list + cnep_list
	record("Portal da Transparência (Dados Abertos CGU)", "ceis_cnep", {"ceis": ceis_list, "cnep": cnep_list})
	return {
		"sanctioned": bool(sanctions),
		"ceis_count": len(ceis_list),
		"cnep_count": len(cnep_list),
		"sanctions": sanctions,
		"raw": {"ceis": ceis_list, "cnep": cnep_list, "source": "dadosabertos-download.cgu.gov.br"},
	}
