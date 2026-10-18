"""Sentry Agent Tracing, following Sentry's gen_ai span conventions. Every helper is a no-op when
SENTRY_DSN is not set, so the app never depends on Sentry.

Trace shape (one import or categorization batch):

    invoke_agent InvoiceLens categorizer        op=gen_ai.invoke_agent
    ├── invoice 2026/0412 Nuvola Hosting        op=function
    │   ├── execute_tool parse_fatturapa        op=gen_ai.execute_tool   (deterministic)
    │   ├── execute_tool duplicate_check        op=gen_ai.execute_tool   (deterministic)
    │   ├── chat gemma-4-26b-a4b-it             op=gen_ai.chat           (tokens, latency, retries)
    │   ├── execute_tool keyword_rules          op=gen_ai.execute_tool   (only when the model failed)
    │   └── execute_tool anomaly_checks         op=gen_ai.execute_tool   (deterministic)
    └── ...
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

try:
    import sentry_sdk
except ImportError:  # Sentry is optional
    sentry_sdk = None

log = logging.getLogger("invoicelens")

AGENT_NAME = "InvoiceLens categorizer"


def init() -> bool:
    """Initialise Sentry from the environment. Returns True when tracing is on."""
    dsn = os.getenv("SENTRY_DSN")
    if not dsn or sentry_sdk is None:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "local"),
        release=os.getenv("SENTRY_RELEASE") or os.getenv("RENDER_GIT_COMMIT") or None,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        # Invoices hold client names and prices: prompts are sent only when explicitly enabled
        # (the public demo does, because it only holds the fake sample invoices).
        send_default_pii=False,
    )
    return True


def active() -> bool:
    return sentry_sdk is not None and sentry_sdk.get_client().is_active()


def send_prompts() -> bool:
    return os.getenv("SENTRY_SEND_PROMPTS") == "1"


def _set(span, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False)  # span attributes take primitives only
        span.set_data(key, value)


@contextmanager
def span(op: str, name: str, **data: Any) -> Iterator[Any]:
    """A child span of whatever is running; a new transaction when nothing is (background threads)."""
    if not active():
        yield None
        return
    if sentry_sdk.get_current_span() is None:
        with sentry_sdk.start_transaction(op=op, name=name) as tx:
            _set(tx, data)
            yield tx
    else:
        with sentry_sdk.start_span(op=op, name=name) as sp:
            _set(sp, data)
            yield sp


@contextmanager
def agent_run(task: str, model: str, provider: str, invoices: int | None = None) -> Iterator[Any]:
    """The parent span of a whole batch: one upload, one sample load, one recategorization pass.
    Background work gets its own transaction so the agent span always has a root to live in."""
    if not active():
        yield None
        return
    outer = sentry_sdk.start_transaction(op="task", name=f"InvoiceLens {task}") \
        if sentry_sdk.get_current_span() is None else None
    try:
        if outer is not None:
            outer.__enter__()
        with sentry_sdk.start_span(op="gen_ai.invoke_agent", name=f"invoke_agent {AGENT_NAME}") as sp:
            _set(sp, {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.pipeline.name": task,
                "gen_ai.request.model": model,
                "gen_ai.provider.name": provider,
                "gen_ai.system": provider,
                "invoicelens.task": task,
                "invoicelens.invoices": invoices,
            })
            yield sp
    finally:
        if outer is not None:
            outer.__exit__(None, None, None)


@contextmanager
def tool(name: str, description: str, arguments: dict | None = None) -> Iterator[Any]:
    """A deterministic step shown as a tool call: parsing, duplicate check, keyword rules, checks."""
    with span(
        "gen_ai.execute_tool",
        f"execute_tool {name}",
        **{
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.type": "function",
            "gen_ai.tool.description": description,
            "gen_ai.tool.call.arguments": arguments,
        },
    ) as sp:
        yield sp


def result(sp, value: Any) -> None:
    """Record a tool's result (small, non-sensitive summaries only: counts, flags, a category)."""
    if sp is not None:
        _set(sp, {"gen_ai.tool.call.result": value})


def set_data(sp, **data: Any) -> None:
    if sp is not None:
        _set(sp, data)


def fail(sp, error_type: str) -> None:
    if sp is not None:
        sp.set_status("internal_error")
        sp.set_data("error.type", error_type)


def event(sp, history: list[str], message: str, **data: Any) -> None:
    """A retry, a timeout or a fallback: logged, kept as a breadcrumb on the trace, and listed on the span
    (invoicelens.events, invoicelens.event_count) so it can be searched and charted in Sentry.
    `history` is the caller's list of events for this span."""
    history.append(message)
    log.warning("%s %s", message, data or "")
    if not active():
        return
    sentry_sdk.add_breadcrumb(category="gen_ai", message=message, level="warning", data=data)
    if sp is not None:
        sp.set_data("invoicelens.events", json.dumps(history))
        sp.set_data("invoicelens.event_count", len(history))
