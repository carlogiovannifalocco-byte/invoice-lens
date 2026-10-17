"""Open-weight model layer. Talks to any OpenAI-compatible endpoint; defaults to Gemma on a local Ollama."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager

import httpx

try:
    import sentry_sdk
except ImportError:  # Sentry is optional
    sentry_sdk = None

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
    return {
        "base_url": os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        "model": os.getenv("LLM_MODEL", "gemma3:4b"),
        "api_key": os.getenv("LLM_API_KEY", "ollama"),
        "timeout": float(os.getenv("LLM_TIMEOUT", "120")),
    }


def model_name() -> str:
    return _config()["model"]


def chat_json(system: str, user: str, task: str) -> dict:
    """Ask the model for a JSON object. Each call is traced as a gen_ai span when Sentry is on."""
    cfg = _config()
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    with _span(cfg["model"], task) as span:
        try:
            resp = httpx.post(
                f"{cfg['base_url']}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=cfg["timeout"],
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailable(str(exc)) from exc
        body = resp.json()
        usage = body.get("usage") or {}
        if span is not None:
            span.set_data("gen_ai.usage.input_tokens", usage.get("prompt_tokens"))
            span.set_data("gen_ai.usage.output_tokens", usage.get("completion_tokens"))
        return _parse_json(body["choices"][0]["message"]["content"])


@contextmanager
def _span(model: str, task: str):
    """A Sentry gen_ai span (agent tracing) when Sentry is configured, otherwise a no-op."""
    if sentry_sdk is None or not sentry_sdk.get_client().is_active():
        yield None
        return
    with sentry_sdk.start_span(op="gen_ai.chat", name=f"chat {model}") as span:
        span.set_data("gen_ai.system", "openai_compatible")
        span.set_data("gen_ai.operation.name", "chat")
        span.set_data("gen_ai.request.model", model)
        span.set_data("invoicelens.task", task)
        yield span


def _parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise LLMUnavailable("model did not return JSON")
        return json.loads(match.group(0))


CATEGORIZE_SYSTEM = (
    "You are a bookkeeping assistant for Italian freelancers. Classify a purchase invoice into exactly "
    f"one category from this list: {', '.join(CATEGORIES)}. Reply with JSON: "
    '{"category": "<one of the list>", "confidence": <0..1>, "reason": "<max 15 words, English>"}'
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
    "You write short, friendly monthly expense summaries for an Italian freelancer. Use only the numbers "
    "given. Mention the biggest categories, VAT that can be reclaimed, anomalies to check and upcoming "
    'payments. Reply with JSON: {"it": "<paragraph in Italian>", "en": "<same paragraph in English>"}'
)


def monthly_summary(stats: dict) -> dict:
    return chat_json(SUMMARY_SYSTEM, json.dumps(stats, ensure_ascii=False), "monthly_summary")


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
