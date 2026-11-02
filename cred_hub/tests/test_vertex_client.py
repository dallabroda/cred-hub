"""Cliente do Vertex AI (Gemini) com sessão simulada (credtech#316). Sem Frappe e sem chamar o Google."""

import json

import pytest

from cred_hub.credit_check.clients.vertex import (
	VertexClient,
	VertexError,
	VertexInvalidOutput,
	VertexNotConfigured,
	VertexResult,
	endpoint,
)


class FakeResponse:
	def __init__(self, status, body=None, text=""):
		self.status_code = status
		self._body = body
		self.text = text or (json.dumps(body) if body is not None else "")
		self.ok = 200 <= status < 300

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


class FakeSession:
	def __init__(self, *responses):
		self.responses = list(responses)
		self.calls = []

	def post(self, url, headers=None, data=None, timeout=None):
		self.calls.append({"url": url, "headers": headers, "body": json.loads(data)})
		return self.responses.pop(0)


def _answer(obj, finish="STOP", thought="pensando..."):
	return FakeResponse(
		200,
		{
			"candidates": [
				{
					"content": {"parts": [{"text": thought, "thought": True}, {"text": json.dumps(obj)}]},
					"finishReason": finish,
				}
			],
			"usageMetadata": {"promptTokenCount": 20000, "candidatesTokenCount": 1500, "thoughtsTokenCount": 2500},
		},
	)


def _client(session):
	return VertexClient("proj", "global", "/run/secrets/sa.json", session=session, token_provider=lambda path: "tok")


def test_global_and_regional_endpoints():
	assert endpoint("p", "global", "m").startswith("https://aiplatform.googleapis.com/v1/projects/p/locations/global/")
	assert endpoint("p", "southamerica-east1", "m").startswith(
		"https://southamerica-east1-aiplatform.googleapis.com/v1/projects/p/locations/southamerica-east1/"
	)
	assert endpoint("p", "global", "gemini-3.8-flash").endswith("/publishers/google/models/gemini-3.8-flash:generateContent")


def test_generate_json_sends_schema_system_and_bearer_and_skips_thoughts():
	session = FakeSession(_answer({"ok": 1}))
	data, result = _client(session).generate_json("gemini-3.8-flash", "regras", "dados", {"type": "OBJECT"})
	assert data == {"ok": 1}
	call = session.calls[0]
	assert call["headers"]["Authorization"] == "Bearer tok"
	assert call["body"]["systemInstruction"] == {"parts": [{"text": "regras"}]}
	assert call["body"]["contents"][0]["parts"][0]["text"] == "dados"
	assert call["body"]["generationConfig"]["responseMimeType"] == "application/json"
	assert call["body"]["generationConfig"]["responseSchema"] == {"type": "OBJECT"}
	assert (result.prompt_tokens, result.output_tokens, result.thinking_tokens) == (20000, 1500, 2500)


def test_cost_counts_thinking_as_output():
	result = VertexResult("{}", "STOP", 20000, 1500, 2500, "gemini-3.8-flash", 1.0)
	# 20 mil x 0,75 + (1.500 + 2.500) x 3,75, por milhão
	assert result.cost_usd == pytest.approx(0.015 + 0.015)
	assert VertexResult("{}", "STOP", 1, 1, 1, "modelo-sem-preco", 1.0).cost_usd == 0.0


def test_invalid_json_and_cut_answers_are_retryable_errors_with_usage():
	session = FakeSession(FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "não é json"}]}, "finishReason": "STOP"}]}))
	with pytest.raises(VertexInvalidOutput) as error:
		_client(session).generate_json("gemini-3.8-flash", "s", "u", {})
	assert error.value.result is not None
	with pytest.raises(VertexInvalidOutput):
		_client(FakeSession(_answer({"a": 1}, finish="MAX_TOKENS"))).generate_json("gemini-3.8-flash", "s", "u", {})


def test_rate_limit_is_retried_once_then_succeeds(monkeypatch):
	monkeypatch.setattr("cred_hub.credit_check.clients.vertex.time.sleep", lambda seconds: None)
	session = FakeSession(FakeResponse(429, {"error": "limite"}), _answer({"ok": 2}))
	data, _result = _client(session).generate_json("gemini-3.8-flash", "s", "u", {})
	assert data == {"ok": 2} and len(session.calls) == 2


def test_model_missing_in_the_region_explains_it():
	with pytest.raises(VertexError) as error:
		_client(FakeSession(FakeResponse(404, {"error": "not found"}))).generate_json("gemini-3.8-flash", "s", "u", {})
	assert "região" in str(error.value)


def test_blocked_prompt_and_missing_configuration_fail_explicitly():
	with pytest.raises(VertexError):
		_client(FakeSession(FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}))).generate_json("m", "s", "u", {})
	with pytest.raises(VertexNotConfigured):
		VertexClient("", "global", "/x")
