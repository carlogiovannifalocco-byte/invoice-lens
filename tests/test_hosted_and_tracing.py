"""Hosted-endpoint robustness (JSON mode / system role fallbacks, rate limits), the demo's background
categorization, and the shape of the Sentry agent traces."""

import copy
import json
from pathlib import Path

import httpx
import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app import ingest, llm, store, tracing
from app.parser import parse_fatturapa

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
URL = "https://generativelanguage.googleapis.com/v1beta/openai"
ANSWER = '{"category": "software", "confidence": 0.9, "reason": "hosting"}'


def sample(name: str) -> bytes:
    return next(SAMPLES.glob(f"{name}-*.xml")).read_bytes()


def invoice() -> dict:
    return parse_fatturapa(sample("01"))[0].to_dict()


def ok(content: str = ANSWER) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": "r1", "model": "gemma-4-26b-a4b-it",
              "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}},
        request=httpx.Request("POST", URL),
    )


def error(status: int, text: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers, request=httpx.Request("POST", URL))


@pytest.fixture(autouse=True)
def hosted(monkeypatch):
    monkeypatch.setattr(llm, "_unsupported", {})
    monkeypatch.setenv("LLM_BASE_URL", URL)
    monkeypatch.setenv("LLM_MODEL", "gemma-4-26b-a4b-it")
    for var in ("LLM_JSON_MODE", "LLM_SYSTEM_AS_USER", "LLM_MIN_INTERVAL"):
        monkeypatch.delenv(var, raising=False)


def scripted(monkeypatch, *responses):
    """Replace httpx.post with a script of responses (or exceptions); returns the payloads sent."""
    sent, queue = [], list(responses)

    def post(url, json, headers, timeout):
        sent.append(json)
        item = queue.pop(0) if queue else ok()
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm.httpx, "post", post)
    return sent


def test_json_mode_refused_retries_without_it_and_remembers(monkeypatch):
    sent = scripted(monkeypatch, error(400, "JSON mode is not enabled for models/gemma-4-26b-a4b-it"))
    assert llm.categorize(invoice())["category"] == "software"
    assert "response_format" in sent[0] and "response_format" not in sent[1]
    llm.categorize(invoice())
    assert "response_format" not in sent[2]  # learned once, not retried on every call
    assert len(sent) == 3


def test_system_role_refused_merges_it_into_the_user_message(monkeypatch):
    monkeypatch.setenv("LLM_JSON_MODE", "0")
    sent = scripted(monkeypatch, error(400, "Developer instruction is not enabled for models/gemma-3-27b-it"))
    llm.categorize(invoice())
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user"]
    assert [m["role"] for m in sent[1]["messages"]] == ["user"]
    assert "bookkeeping assistant" in sent[1]["messages"][0]["content"]
    assert "Hosting VPS annuale" in sent[1]["messages"][0]["content"]


def test_env_switches_turn_both_fields_off_up_front(monkeypatch):
    monkeypatch.setenv("LLM_JSON_MODE", "0")
    monkeypatch.setenv("LLM_SYSTEM_AS_USER", "1")
    sent = scripted(monkeypatch)
    llm.categorize(invoice())
    assert "response_format" not in sent[0]
    assert [m["role"] for m in sent[0]["messages"]] == ["user"]


def test_a_400_that_no_fallback_fixes_is_reported(monkeypatch):
    scripted(monkeypatch, error(400, "bad"), error(400, "bad"), error(400, "API key not valid"))
    with pytest.raises(llm.LLMUnavailable, match="refused"):
        llm.categorize(invoice())


def test_a_bad_api_key_fails_at_once(monkeypatch):
    sent = scripted(monkeypatch, error(400, '{"error": {"message": "API key not valid. Please pass a valid API key."}}'))
    with pytest.raises(llm.LLMUnavailable):
        llm.categorize(invoice())
    assert len(sent) == 1 and llm._unsupported[(URL, "gemma-4-26b-a4b-it")] == set()


def test_rate_limit_waits_and_retries(monkeypatch):
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    sent = scripted(monkeypatch, error(429, "quota", {"retry-after": "3"}))
    assert llm.categorize(invoice())["category"] == "software"
    assert waits == [3.0] and len(sent) == 2


def test_min_interval_spaces_out_calls(monkeypatch):
    monkeypatch.setenv("LLM_MIN_INTERVAL", "4")
    monkeypatch.setattr(llm, "_last_call", [llm.time.monotonic()])
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    scripted(monkeypatch)
    llm.categorize(invoice())
    assert waits and 3 < waits[0] <= 4


@pytest.mark.parametrize("content", [
    "<thought>maybe {\"category\": \"other\"}? no</thought>" + ANSWER,
    "```json\n" + ANSWER + "\n```",
    "Here you go: " + ANSWER + " Hope this helps.",
])
def test_parse_json_tolerates_thinking_fences_and_chatter(content):
    assert llm._parse_json(content)["category"] == "software"


def test_parse_json_keeps_nested_objects():
    assert llm._parse_json('Result: {"a": {"b": 1}}') == {"a": {"b": 1}}


def test_provider_names_follow_otel_values(monkeypatch):
    assert llm._config()["provider"] == "gcp.gemini"
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert llm._config()["provider"] == "ollama"


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app.main import app

    return TestClient(app)


def test_healthz_does_not_call_the_model(tmp_path, monkeypatch):
    sent = scripted(monkeypatch)
    assert _client(tmp_path, monkeypatch).get("/healthz").json() == {"ok": True}
    assert sent == []


def test_recategorize_replaces_rules_but_keeps_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
    first = ingest.ingest("01.xml", sample("01"), use_model=False)[0]
    second = ingest.ingest("02.xml", sample("02"), use_model=False)[0]
    assert first["category_source"] == "rules"
    store.set_category(second["id"], "meals")
    scripted(monkeypatch)
    assert ingest.recategorize() == 1
    rows = {r["id"]: r for r in store.all_invoices()}
    assert rows[first["id"]]["category_source"] == "gemma-4-26b-a4b-it"
    assert rows[second["id"]]["category"] == "meals" and rows[second["id"]]["category_source"] == "manual"
    assert ingest.progress == {"running": False, "done": 1, "total": 1}


def test_recategorize_stops_after_two_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
    for name in ("01", "02", "03"):
        ingest.ingest(f"{name}.xml", sample(name), use_model=False)
    sent = scripted(monkeypatch, *[httpx.ConnectError("down")] * 10)
    assert ingest.recategorize() == 0
    assert len(sent) == 2


def test_demo_summary_is_cached_to_save_quota(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, INVOICELENS_DEMO="1")
    store.clear()
    sent = scripted(monkeypatch, *[ok('{"it": "Ciao", "en": "Hi"}')] * 2)
    assert client.get("/api/summary?month=2026-10").json()["en"] == "Hi"
    assert client.get("/api/summary?month=2026-10").json()["en"] == "Hi"
    assert len(sent) == 1


class _Capture(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)


@pytest.fixture
def sentry(monkeypatch):
    transactions = []

    def keep(event, hint):
        # A copy: the SDK later moves gen_ai spans out of this event into their own envelope item.
        transactions.append(copy.deepcopy(event))
        return event

    monkeypatch.setenv("SENTRY_SEND_PROMPTS", "1")
    sentry_sdk.init(dsn="https://public@o0.ingest.sentry.io/0", traces_sample_rate=1.0,
                    transport=_Capture(), before_send_transaction=keep)
    yield transactions
    sentry_sdk.get_client().close()
    sentry_sdk.init()


def _spans(transactions, op):
    return [s for t in transactions for s in t.get("spans", []) if s.get("op") == op]


def test_upload_trace_has_agent_chat_and_tool_spans(tmp_path, monkeypatch, sentry):
    client = _client(tmp_path, monkeypatch)
    scripted(monkeypatch, httpx.ReadTimeout("cold start"))  # first call times out, retry succeeds
    res = client.post("/api/invoices", files=[("files", ("01.xml", sample("01"), "text/xml"))])
    assert res.json()["stored"][0]["category_source"] == "gemma-4-26b-a4b-it"
    sentry_sdk.flush()

    agent = _spans(sentry, "gen_ai.invoke_agent")
    assert len(agent) == 1
    assert agent[0]["description"] == "invoke_agent InvoiceLens categorizer"
    assert agent[0]["data"]["gen_ai.agent.name"] == "InvoiceLens categorizer"
    assert agent[0]["data"]["gen_ai.operation.name"] == "invoke_agent"

    chat = _spans(sentry, "gen_ai.chat")
    assert len(chat) == 1
    data = chat[0]["data"]
    assert chat[0]["description"] == "chat gemma-4-26b-a4b-it"
    assert data["gen_ai.request.model"] == "gemma-4-26b-a4b-it"
    assert data["gen_ai.provider.name"] == "gcp.gemini"
    assert data["gen_ai.usage.input_tokens"] == 120 and data["gen_ai.usage.output_tokens"] == 20
    assert json.loads(data["invoicelens.events"]) == ["timeout"]
    assert "Hosting VPS annuale" in data["gen_ai.input.messages"]

    tools = {s["data"]["gen_ai.tool.name"] for s in _spans(sentry, "gen_ai.execute_tool")}
    assert {"parse_fatturapa", "duplicate_check", "anomaly_checks"} <= tools

    by_id = {s["span_id"]: s for t in sentry for s in t.get("spans", [])}
    invoice_span = by_id[chat[0]["parent_span_id"]]
    assert invoice_span["op"] == "function" and invoice_span["parent_span_id"] == agent[0]["span_id"]


def test_fallback_to_rules_is_visible_in_the_trace(tmp_path, monkeypatch, sentry):
    client = _client(tmp_path, monkeypatch)
    scripted(monkeypatch, *[httpx.ConnectError("refused")] * 3)
    res = client.post("/api/invoices", files=[("files", ("01.xml", sample("01"), "text/xml"))])
    assert res.json()["stored"][0]["category_source"] == "rules"
    sentry_sdk.flush()
    chat = _spans(sentry, "gen_ai.chat")[0]
    assert chat["status"] == "internal_error" and chat["data"]["error.type"] == "ConnectError"
    tools = [s["data"]["gen_ai.tool.name"] for s in _spans(sentry, "gen_ai.execute_tool")]
    assert "keyword_rules" in tools


def test_background_batch_gets_its_own_transaction(tmp_path, monkeypatch, sentry):
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
    ingest.ingest("01.xml", sample("01"), use_model=False)
    sentry.clear()
    scripted(monkeypatch)
    assert ingest.recategorize("demo_preload") == 1
    sentry_sdk.flush()
    assert sentry[0]["transaction"] == "InvoiceLens demo_preload"
    assert _spans(sentry, "gen_ai.invoke_agent")[0]["data"]["gen_ai.pipeline.name"] == "demo_preload"
    assert _spans(sentry, "gen_ai.chat")
