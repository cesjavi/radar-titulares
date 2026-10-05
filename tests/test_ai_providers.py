"""Groq y Fireworks (API compatible con OpenAI) con transporte HTTP simulado. Sin red."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from radar.ai.config import load_config
from radar.ai.openai_compat import FailoverProvider, OpenAICompatibleProvider
from radar.ai.providers import (
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderTimeout,
    ProviderUnavailable,
    make_provider,
)
from radar.ai.service import blocked_reason, run_ai
from radar.models import AiAnalysis, AiUsage
from tests.test_analysis import media  # noqa: F401  (fixture)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
FW_URL = "https://api.fireworks.ai/inference/v1/chat/completions"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secreto-groq")
    monkeypatch.setenv("RADAR_GROQ_MODEL", "modelo-groq-elegido")
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-secreto-fireworks")
    monkeypatch.setenv("RADAR_FIREWORKS_MODEL", "accounts/fireworks/models/modelo-elegido")


def ok_response(content: str, model="modelo-groq-elegido"):
    return httpx.Response(200, json={
        "id": "chatcmpl-1", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165,
                  "prompt_tokens_details": {"cached_tokens": 20}},
    }, headers={"x-request-id": "req_groq_1"})


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def provider(name, handler):
    rec = Recorder(handler)
    return OpenAICompatibleProvider(name, 10, 4096, transport=httpx.MockTransport(rec)), rec


def test_groq_request_shape_and_usage(keys):
    p, rec = provider("groq", lambda r: ok_response('{"x": 1}'))
    res = p.complete("sistema", "usuario", {"type": "object", "properties": {}, "required": [],
                                            "additionalProperties": False})
    req = rec.requests[0]
    body = json.loads(req.content)
    assert str(req.url) == GROQ_URL
    assert req.headers["authorization"] == "Bearer gsk-secreto-groq"
    assert body["model"] == "modelo-groq-elegido"
    assert body["messages"][0] == {"role": "system", "content": "sistema"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert "tools" not in body and "tool_choice" not in body
    assert (res.input_tokens, res.output_tokens, res.cache_read_tokens) == (120, 45, 20)
    assert res.request_id == "req_groq_1" and res.text == '{"x": 1}'


def test_fireworks_request_shape(keys):
    p, rec = provider("fireworks", lambda r: ok_response("{}", model="accounts/fireworks/models/modelo-elegido"))
    p.complete("s", "u", {"type": "object"})
    req = rec.requests[0]
    body = json.loads(req.content)
    assert str(req.url) == FW_URL
    assert req.headers["authorization"] == "Bearer fw-secreto-fireworks"
    assert body["model"] == "accounts/fireworks/models/modelo-elegido"
    assert body["response_format"]["type"] == "json_schema"
    assert "strict" not in body["response_format"]["json_schema"]


def test_groq_strict_can_be_disabled(keys, monkeypatch):
    monkeypatch.setenv("RADAR_GROQ_STRICT", "false")
    p, rec = provider("groq", lambda r: ok_response("{}"))
    p.complete("s", "u", {})
    assert "strict" not in json.loads(rec.requests[0].content)["response_format"]["json_schema"]


@pytest.mark.parametrize("response, exc_type", [
    (httpx.Response(429, json={"error": {"message": "Rate limit reached for requests"}},
                    headers={"retry-after": "12"}), ProviderRateLimited),
    (httpx.Response(429, json={"error": {"message": "Rate limit reached on tokens per day (TPD)"}}),
     ProviderQuotaExceeded),
    (httpx.Response(402, json={"error": {"message": "Payment required"}}), ProviderQuotaExceeded),
    (httpx.Response(401, json={"error": {"message": "Invalid API Key"}}), ProviderError),
    (httpx.Response(503, text="overloaded"), ProviderUnavailable),
    (httpx.Response(400, json={"error": {"message": "model does not support json_schema"}}), ProviderError),
])
def test_error_mapping(keys, response, exc_type):
    p, _ = provider("groq", lambda r: response)
    with pytest.raises(exc_type) as exc:
        p.complete("s", "u", {})
    if isinstance(exc.value, ProviderRateLimited):
        assert exc.value.retry_after == 12
    assert "gsk-secreto-groq" not in str(exc.value)


def test_timeout_and_refusal(keys):
    def timeout(request):
        raise httpx.ReadTimeout("lento", request=request)

    p, _ = provider("groq", timeout)
    with pytest.raises(ProviderTimeout):
        p.complete("s", "u", {})
    refusal = httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                     "message": {"content": None, "refusal": "No."}}]})
    p, _ = provider("fireworks", lambda r: refusal)
    with pytest.raises(ProviderRefusal):
        p.complete("s", "u", {})


def test_secret_is_redacted_from_echoed_errors(keys):
    p, _ = provider("groq", lambda r: httpx.Response(
        400, json={"error": {"message": "bad key gsk-secreto-groq"}}))
    with pytest.raises(ProviderError) as exc:
        p.complete("s", "u", {})
    assert "gsk-secreto-groq" not in str(exc.value) and "***" in str(exc.value)


def test_missing_model_or_key_is_not_called(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
    monkeypatch.delenv("RADAR_GROQ_MODEL", raising=False)
    p, rec = provider("groq", lambda r: ok_response("{}"))
    with pytest.raises(ProviderError, match="RADAR_GROQ_MODEL"):
        p.complete("s", "u", {})
    assert rec.requests == []


def test_failover_groq_to_fireworks(keys, monkeypatch):
    monkeypatch.setenv("RADAR_AI_PROVIDER", "groq,fireworks")

    def handler(request):
        if "groq" in str(request.url):
            return httpx.Response(429, json={"error": {"message": "tokens per day (TPD) exceeded"}})
        return ok_response("{}", model="accounts/fireworks/models/modelo-elegido")

    chain = make_provider(load_config(), transport=httpx.MockTransport(handler))
    assert isinstance(chain, FailoverProvider)
    res = chain.complete("s", "u", {})
    assert res.extra["provider"] == "fireworks"
    assert res.extra["fallos_previos"][0][:2] == ("groq", "cuota")


def test_failover_all_fail_raises_last(keys, monkeypatch):
    monkeypatch.setenv("RADAR_AI_PROVIDER", "groq,fireworks")
    chain = make_provider(load_config(), transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(ProviderUnavailable) as exc:
        chain.complete("s", "u", {})
    assert [f[0] for f in exc.value.failures] == ["groq", "fireworks"]


def test_real_calls_blocked_in_tests(keys):
    with pytest.raises(RuntimeError, match="bloqueadas"):
        OpenAICompatibleProvider("groq", 10, 100)


def test_blocked_reason_reports_missing_config(db, monkeypatch):
    monkeypatch.setenv("RADAR_AI_ENABLED", "true")
    monkeypatch.setenv("RADAR_AI_PROVIDER", "groq,fireworks")
    for k in ("GROQ_API_KEY", "RADAR_GROQ_MODEL", "FIREWORKS_API_KEY", "RADAR_FIREWORKS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    reason = blocked_reason(db, load_config())
    assert "GROQ_API_KEY" in reason and "FIREWORKS_API_KEY" in reason
    assert load_config().models_label == "groq:(sin modelo), fireworks:(sin modelo)"


def test_end_to_end_with_failover_records_usage_per_provider(db, media, keys, monkeypatch):
    from radar.analysis.engine import run_analysis
    from radar.ai.service import select_candidates
    from tests.test_ai import output
    from tests.test_analysis import seed_story

    monkeypatch.setenv("RADAR_AI_ENABLED", "true")
    monkeypatch.setenv("RADAR_AI_PROVIDER", "groq,fireworks")
    seed_story(db, media)
    run_analysis(db)
    db.commit()
    rel = select_candidates(db, 1)[0]
    content = json.dumps(output(rel.article_a.title, rel.article_b.title))

    def handler(request):
        if "groq" in str(request.url):
            return httpx.Response(503)
        return ok_response(content, model="accounts/fireworks/models/modelo-elegido")

    chain = make_provider(load_config(), transport=httpx.MockTransport(handler))
    s = run_ai(db, provider=chain, limit=1)
    db.commit()
    assert s.ok == 1
    an = db.scalar(select(AiAnalysis))
    assert an.provider == "fireworks" and an.model == "accounts/fireworks/models/modelo-elegido"
    rows = {(u.provider, u.status) for u in db.scalars(select(AiUsage))}
    assert ("groq", "error") in rows and ("fireworks", "ok") in rows
    ok = db.scalar(select(AiUsage).where(AiUsage.status == "ok"))
    assert (ok.input_tokens, ok.output_tokens) == (120, 45)


def test_unconfigured_provider_in_chain_is_skipped(monkeypatch):
    monkeypatch.setenv("RADAR_AI_PROVIDER", "groq,fireworks")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("RADAR_GROQ_MODEL", raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-x")
    monkeypatch.setenv("RADAR_FIREWORKS_MODEL", "accounts/fireworks/models/modelo")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return ok_response("{}", model="accounts/fireworks/models/modelo")

    p = make_provider(load_config(), transport=httpx.MockTransport(handler))
    assert p.name == "fireworks"  # Groq no se llama ni registra intentos
    p.complete("s", "u", {})
    assert seen == [FW_URL]
