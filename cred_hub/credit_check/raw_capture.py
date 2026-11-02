"""Guarda o retorno inteiro de cada chamada aos provedores (credtech#316).

Os clientes devolvem só o resumo que as regras entendem (ex.: o Score da DirectD vira 4 campos), e o resto do
retorno se perdia. O dossiê precisa mostrar o retorno completo, sem tirar nem pôr, então cada chamada HTTP grava
aqui o JSON como o provedor devolveu, e a esteira (`enrich.py`) junta tudo no relatório de crédito.

Só o retorno é guardado, nunca o pedido: tokens, senhas e chaves que um provedor ecoe de volta são trocados por
"***" antes de gravar.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_calls: ContextVar[list | None] = ContextVar("cred_hub_raw_calls", default=None)

SENSITIVE_KEYS = frozenset(
	{
		"token",
		"access_token",
		"accesstoken",
		"refresh_token",
		"authorization",
		"password",
		"senha",
		"secret",
		"client_secret",
		"clientsecret",
		"api_key",
		"apikey",
		"chave-api-dados",
	}
)


def scrub(node: Any) -> Any:
	"""Cópia do retorno sem credenciais (qualquer chave sensível, em qualquer nível)."""
	if isinstance(node, dict):
		return {
			key: ("***" if str(key).lower() in SENSITIVE_KEYS and node[key] else scrub(node[key])) for key in node
		}
	if isinstance(node, list):
		return [scrub(item) for item in node]
	return node


@contextmanager
def capture() -> Iterator[list]:
	"""Dentro do bloco, cada `record()` vai para a lista devolvida."""
	calls: list = []
	token = _calls.set(calls)
	try:
		yield calls
	finally:
		_calls.reset(token)


def record(provider: str, endpoint: str, payload: Any) -> None:
	"""Chamado pelos clientes HTTP com o JSON que o provedor devolveu. Fora de um `capture()`, não faz nada."""
	calls = _calls.get()
	if calls is not None:
		calls.append({"provider": provider, "endpoint": endpoint, "payload": scrub(payload)})
