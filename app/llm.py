"""Open-weight model layer. Talks to any OpenAI-compatible endpoint; defaults to Gemma on a local Ollama."""

from __future__ import annotations

import json
import os
import re
import time
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
        "timeout": float(os.getenv("LLM_TIMEOUT", "300")),  # CPU-only laptops are slow
    }


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
        for attempt in (1, 2):
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
                if attempt == 2:
                    raise LLMUnavailable(str(exc)) from exc
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
    "sentences per language. The Italian text addresses the reader as 'tu' and uses no English words. "
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
