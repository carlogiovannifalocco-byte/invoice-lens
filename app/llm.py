"""Open-weight model layer. Talks to any OpenAI-compatible endpoint; defaults to Gemma on a local Ollama."""

from __future__ import annotations

import json
import os
import re
import threading
import time

import httpx

from . import tracing

CATEGORIES = [
    "software",
    "hardware",
    "travel",
    "meals",
    "office",
    "utilities",
    "telecom",
    "professional_services",
    "training",
    "marketing",
    "rent",
    "other",
]


class LLMUnavailable(RuntimeError):
    pass


def _config() -> dict:
    base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    return {
        "base_url": base_url,
        "model": os.getenv("LLM_MODEL", "gemma3:4b"),
        "api_key": os.getenv("LLM_API_KEY", "ollama"),
        "timeout": float(os.getenv("LLM_TIMEOUT", "300")),  # CPU-only laptops are slow
        # Some hosted Gemma endpoints reject JSON mode or the system role with a 400. These switch them
        # off up front; without them InvoiceLens notices the 400 and retries without the field.
        "json_mode": os.getenv("LLM_JSON_MODE", "1") != "0",
        "system_as_user": os.getenv("LLM_SYSTEM_AS_USER", "0") == "1",
        # Seconds between two model calls, to stay under a free tier's requests-per-minute limit.
        "min_interval": float(os.getenv("LLM_MIN_INTERVAL", "0")),
        "provider": os.getenv("LLM_PROVIDER") or _guess_provider(base_url),
    }


def _guess_provider(base_url: str) -> str:
    """Provider name for gen_ai.provider.name, using the OpenTelemetry well-known values where they exist."""
    if "generativelanguage.googleapis.com" in base_url:
        return "gcp.gemini"
    if "11434" in base_url or "ollama" in base_url:
        return "ollama"
    return "openai"  # any other OpenAI-compatible server (llama.cpp, vLLM...)


def model_name() -> str:
    return _config()["model"]


_status_cache: dict[tuple, tuple[float, bool]] = {}
STATUS_TTL = 20.0


def endpoint_reachable() -> bool:
    """Cheap check (GET /models, short timeout, cached) used by the UI status badge.
    It does not run the model; /api/health does a real round trip."""
    cfg = _config()
    key = (cfg["base_url"], cfg["api_key"])
    hit = _status_cache.get(key)
    if hit and time.monotonic() - hit[0] < STATUS_TTL:
        return hit[1]
    try:
        resp = httpx.get(
            f"{cfg['base_url']}/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=float(os.getenv("LLM_STATUS_TIMEOUT", "1.5")),
        )
        ok = resp.status_code < 400
    except httpx.HTTPError:
        ok = False
    _status_cache[key] = (time.monotonic(), ok)
    return ok


# Fields an endpoint refused with a 400, remembered per (base_url, model) so later calls skip them.
_unsupported: dict[tuple[str, str], set[str]] = {}
_throttle_lock = threading.Lock()
_last_call = [0.0]
MAX_RATE_LIMIT_RETRIES = 2


def _throttle(min_interval: float) -> None:
    if min_interval <= 0:
        return
    with _throttle_lock:
        wait = _last_call[0] + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def build_payload(cfg: dict, system: str, user: str, json_mode: bool, system_role: bool) -> dict:
    if system_role:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    else:
        messages = [{"role": "user", "content": f"{system}\n\n{user}"}]
    payload = {"model": cfg["model"], "temperature": 0, "messages": messages}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _refused_field(error_text: str, json_mode: bool, system_role: bool) -> str | None:
    """Which optional field a 400 most likely refers to: "json_mode", "system" or None (a real error)."""
    text = error_text.lower()
    if "api key" in text or "api_key" in text:
        return None  # the Gemini API answers 400 to a bad key: not something a fallback can fix
    if json_mode and any(k in text for k in ("json", "response_format", "response_mime_type", "mime")):
        return "json_mode"
    if system_role and any(k in text for k in ("system", "developer instruction", "instruction")):
        return "system"
    if json_mode:
        return "json_mode"
    if system_role:
        return "system"
    return None


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), 30.0)
    except ValueError:
        return min(5.0 * 2 ** attempt, 30.0)


def chat_json(system: str, user: str, task: str) -> dict:
    """Ask the model for a JSON object. Each call is a Sentry gen_ai.chat span with token usage,
    and every retry or fallback is recorded on it."""
    cfg = _config()
    key = (cfg["base_url"], cfg["model"])
    refused = _unsupported.setdefault(key, set())
    json_mode = cfg["json_mode"] and "json_mode" not in refused
    system_role = not cfg["system_as_user"] and "system" not in refused
    events: list[str] = []
    with tracing.span(
        "gen_ai.chat",
        f"chat {cfg['model']}",
        **{
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": cfg["provider"],
            "gen_ai.system": cfg["provider"],
            "gen_ai.request.model": cfg["model"],
            "gen_ai.request.temperature": 0,
            "gen_ai.agent.name": tracing.AGENT_NAME,
            "invoicelens.task": task,
        },
    ) as span:
        if tracing.send_prompts():
            tracing.set_data(span, **{
                "gen_ai.system_instructions": [{"type": "text", "content": system}],
                "gen_ai.input.messages": [{"role": "user", "parts": [{"type": "text", "content": user}]}],
            })
        timeouts = rate_limited = 0
        started = time.monotonic()
        while True:
            payload = build_payload(cfg, system, user, json_mode, system_role)
            _throttle(cfg["min_interval"])
            try:
                resp = httpx.post(
                    f"{cfg['base_url']}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    timeout=cfg["timeout"],
                )
                resp.raise_for_status()
                break
            except httpx.TimeoutException as exc:
                # The first call after a cold start also loads the model into memory; retry once.
                timeouts += 1
                tracing.event(span, events, "timeout", task=task, seconds=cfg["timeout"], attempt=timeouts)
                if timeouts == 2:
                    tracing.fail(span, "timeout")
                    raise LLMUnavailable(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 400:
                    field = _refused_field(exc.response.text, json_mode, system_role)
                    if field is None:
                        tracing.fail(span, "http_400")
                        raise LLMUnavailable(f"model endpoint refused the request: {exc.response.text[:300]}") from exc
                    refused.add(field)
                    json_mode = json_mode and field != "json_mode"
                    system_role = system_role and field != "system"
                    tracing.event(span, events, f"fallback: retry without {field}", task=task, model=cfg["model"])
                    continue
                if status == 429 and rate_limited < MAX_RATE_LIMIT_RETRIES:
                    wait = _retry_after(exc.response, rate_limited)
                    rate_limited += 1
                    tracing.event(span, events, "rate limited (429): waiting", task=task, seconds=wait)
                    time.sleep(wait)
                    continue
                tracing.fail(span, f"http_{status}")
                raise LLMUnavailable(f"model endpoint returned HTTP {status}") from exc
            except httpx.HTTPError as exc:
                tracing.fail(span, type(exc).__name__)
                raise LLMUnavailable(str(exc)) from exc
        body = resp.json()
        usage = body.get("usage") or {}
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        tracing.set_data(span, **{
            "gen_ai.response.model": body.get("model") or cfg["model"],
            "gen_ai.response.id": body.get("id"),
            "gen_ai.response.finish_reasons": [choice["finish_reason"]] if choice.get("finish_reason") else None,
            "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
            "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
            "gen_ai.usage.total_tokens": usage.get("total_tokens"),
            "invoicelens.json_mode": json_mode,
            "invoicelens.system_role": system_role,
            "invoicelens.model_seconds": round(time.monotonic() - started, 2),
        })
        if tracing.send_prompts():
            tracing.set_data(span, **{
                "gen_ai.output.messages": [{"role": "assistant", "parts": [{"type": "text", "content": content}]}],
            })
        try:
            return _parse_json(content)
        except (LLMUnavailable, ValueError):
            tracing.fail(span, "invalid_json")
            raise


_THOUGHTS = re.compile(r"<(think|thought|thinking)>.*?</\1>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _parse_json(content: str) -> dict:
    """Parse the model's JSON, tolerating code fences, a sentence around it, or a thinking block
    (Gemma 4 can think before answering)."""
    content = _THOUGHTS.sub("", content).strip()
    candidates = [content] + _FENCE.findall(content)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    # Otherwise take the last {...} object that parses: the answer comes after any reasoning.
    found, i = None, content.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(content, i)
        except json.JSONDecodeError:
            i = content.find("{", i + 1)
            continue
        if isinstance(value, dict):
            found = value
        i = content.find("{", end)
    if found is None:
        raise LLMUnavailable("model did not return JSON")
    return found


CATEGORY_HINTS = {
    "software": "licences, SaaS, cloud, web hosting, domain names",
    "hardware": "computers, monitors, keyboards, devices",
    "travel": "trains, flights, hotels, taxis, fuel, tolls",
    "meals": "restaurants, business lunches, catering",
    "office": "coworking desks, stationery, office supplies",
    "utilities": "electricity, gas, water",
    "telecom": "phone lines, mobile plans, fibre or home internet",
    "professional_services": "accountant, lawyer, notary, consultants",
    "training": "courses, workshops, conferences, books",
    "marketing": "ads, sponsorships, promotional material",
    "rent": "office or studio rent",
    "other": "anything that fits none of the above",
}

CATEGORIZE_SYSTEM = (
    "You are a bookkeeping assistant for Italian freelancers. Classify a purchase invoice into exactly "
    "one of these categories:\n"
    + "\n".join(f"- {k}: {v}" for k, v in CATEGORY_HINTS.items())
    + '\nReply with JSON: {"category": "<one of the list>", "confidence": <0..1>, '
    '"reason": "<max 15 words, English>"}'
)


def categorize(invoice: dict) -> dict:
    lines = "\n".join(f"- {l['description']} ({l['total']} EUR)" for l in invoice["lines"][:20])
    user = f"Supplier: {invoice['supplier']['name']}\nLines:\n{lines or '- (none)'}"
    result = chat_json(CATEGORIZE_SYSTEM, user, "categorize")
    if result.get("category") not in CATEGORIES:
        result["category"] = "other"
    result["source"] = model_name()
    return result


EXTRACT_SYSTEM = (
    "Extract invoice data from the text of a PDF invoice. Reply with JSON only: "
    '{"number": str, "date": "YYYY-MM-DD", "supplier_name": str, "supplier_vat": str, '
    '"taxable": number, "vat_rate": number, "tax": number, "total": number, '
    '"due_date": "YYYY-MM-DD or empty", "description": str}. Use empty strings or 0 when unknown. '
    "Never invent values that are not in the text."
)


def extract_from_text(text: str) -> dict:
    return chat_json(EXTRACT_SYSTEM, text[:12000], "extract_pdf")


SUMMARY_SYSTEM = (
    "You write a short monthly expense summary for an Italian freelancer from a list of facts computed "
    "by their bookkeeping app. Use only these facts: do not add numbers, suppliers or problems that are "
    "not listed, and do not call an invoice late unless it is listed under 'Overdue'. At most four "
    "sentences per language, in a plain professional tone: no greetings, no compliments, no exclamation marks. "
    "The Italian text addresses the reader as 'tu' and uses no English words (write 'fattura', never 'invoice'). "
    'Reply with JSON: {"it": "<paragraph in Italian>", "en": "<same paragraph in English>"}'
)

_ANOMALY_LABELS = {
    "duplicate": "Possible duplicate",
    "vat_mismatch": "VAT does not match the rate",
    "total_mismatch": "Total does not add up",
    "overdue": "Overdue",
}


def summary_facts(stats: dict) -> str:
    """Turn the computed month into plain sentences, so a small model cannot misread nested JSON."""
    eur = lambda v: f"{v:.2f} EUR"
    lines = [
        f"Month: {stats['month']}. Invoices: {stats['invoice_count']}.",
        f"Spend before VAT: {eur(stats['taxable_total'])}. VAT that can be reclaimed: {eur(stats['vat_total'])}.",
    ]
    top = list(stats["by_category"].items())[:3]
    if top:
        lines.append("Biggest categories: " + "; ".join(f"{k.replace('_', ' ')} {eur(v)}" for k, v in top) + ".")
    for kind, label in _ANOMALY_LABELS.items():
        items = [a for a in stats["anomalies"] if a["kind"] == kind]
        if items:
            lines.append(f"{label}: " + "; ".join(f"{a['supplier']} ({a['message']})" for a in items) + ".")
    if stats["upcoming_payments"]:
        lines.append(
            "Payments due in the next 30 days: "
            + "; ".join(f"{p['supplier']} {eur(p['amount'])} on {p['due_date']}" for p in stats["upcoming_payments"])
            + "."
        )
    else:
        lines.append("No payments due in the next 30 days.")
    return "\n".join(lines)


def monthly_summary(stats: dict) -> dict:
    return chat_json(SUMMARY_SYSTEM, summary_facts(stats), "monthly_summary")


def warm_up() -> None:
    """Load the model in the background at startup so the first invoice does not wait for it."""
    if not endpoint_reachable():
        return
    try:
        chat_json("Reply with JSON.", '{"ping": true}', "warm_up")
    except (LLMUnavailable, ValueError):
        pass


# Used only when no model endpoint is reachable, so the app still works; results are labelled "rules".
_KEYWORDS = {
    "software": ["software", "licen", "saas", "abbonamento", "cloud", "hosting", "dominio", "subscription"],
    "hardware": ["laptop", "monitor", "notebook", "tastiera", "mouse", "hardware", "pc "],
    "travel": ["treno", "volo", "trenitalia", "italo", "hotel", "taxi", "carburante", "autostrad"],
    "meals": ["ristorante", "pranzo", "cena", "bar ", "catering"],
    "telecom": ["telefon", "fibra", "mobile", "sim", "internet"],
    "utilities": ["energia", "luce", "gas", "acqua", "elettric"],
    "office": ["cancelleria", "carta", "toner", "coworking", "ufficio"],
    "professional_services": ["consulenz", "commercialista", "notai", "avvocat", "contabil"],
    "training": ["corso", "formazione", "workshop", "conferenz"],
    "marketing": ["pubblicit", "advertising", "marketing", "social"],
    "rent": ["affitto", "locazione", "canone"],
}


def categorize_by_rules(invoice: dict) -> dict:
    haystack = " ".join(
        [invoice["supplier"]["name"]] + [l["description"] for l in invoice["lines"]]
    ).lower()
    for category, words in _KEYWORDS.items():
        if any(w in haystack for w in words):
            return {"category": category, "confidence": 0.5, "reason": "keyword match", "source": "rules"}
    return {"category": "other", "confidence": 0.2, "reason": "no keyword matched", "source": "rules"}
