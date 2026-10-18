"""InvoiceLens HTTP API and web UI."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import ingest as ingest_mod
from . import llm, store, tracing
from .accountant import DISCLAIMER_EN, accountant_pack, csv_safe, explain, report_xlsx, tax_hint
from .analysis import anomalies, month_stats
from .ingest import ingest, ingest_upload
from .parser import InvoiceParseError

tracing.init()

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 50 * 1024 * 1024  # ZIP archives of a month of invoices



def _demo_preload() -> None:
    """Public demo start-up: show the sample invoices at once (keyword rules), then let the model
    categorize them in the background, so the dashboard ends up with the model's labels."""
    if store.count() == 0:
        _load_samples(use_model=False)
    ingest_mod.recategorize("demo_preload")


@asynccontextmanager
async def lifespan(_app):
    if _demo() and os.getenv("INVOICELENS_DEMO_PRELOAD", "1") != "0":
        threading.Thread(target=_demo_preload, daemon=True).start()
    elif os.getenv("LLM_WARMUP", "1") != "0":
        threading.Thread(target=llm.warm_up, daemon=True).start()
    yield


app = FastAPI(title="InvoiceLens", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/healthz")
def healthz():
    """Liveness for the hosting platform: answers without calling the model."""
    return {"ok": True}


@app.get("/api/health")
def health():
    """A real round trip to the model. The public demo answers from a one-minute cache."""
    now = time.monotonic()
    if _demo() and _health_cache and now - _health_cache[0] < 60:
        return _health_cache[1]
    try:
        llm.chat_json('Reply with JSON {"ok": true}.', "ping", "health")
        model_ok = True
    except llm.LLMUnavailable:
        model_ok = False
    result = {"model": llm.model_name(), "model_reachable": model_ok}
    _health_cache[:] = [now, result]
    return result


_health_cache: list = []


def _demo() -> bool:
    return os.getenv("INVOICELENS_DEMO") == "1"


def _can_load_samples() -> bool:
    # The demo always can (it resets to the samples); a local install only while it is empty,
    # so fake invoices are never mixed into someone's real books.
    return _demo() or store.count() == 0


@app.get("/api/config")
def config():
    return {
        "demo": _demo(),
        "model": llm.model_name(),
        "model_reachable": llm.endpoint_reachable(),
        "uploads_enabled": not _demo(),
        "can_load_samples": _can_load_samples(),
        "categories": llm.CATEGORIES,
        "tax_hint_disclaimer": DISCLAIMER_EN,
        "storage": None if _demo() else os.path.abspath(store.db_path()),
        "background": dict(ingest_mod.progress),
    }


@app.post("/api/invoices")
async def upload(files: list[UploadFile]):
    if _demo():
        raise HTTPException(403, "The public demo only shows the sample invoices. Run InvoiceLens locally for your own files.")
    results, errors, skipped = [], [], []
    cfg = llm._config()
    with tracing.agent_run("upload", cfg["model"], cfg["provider"]) as agent:
        await _upload_files(files, results, errors, skipped)
        tracing.set_data(agent, **{"invoicelens.stored": len(results), "invoicelens.errors": len(errors),
                                   "invoicelens.skipped": len(skipped)})
    return {"stored": results, "errors": errors, "skipped": skipped}


async def _upload_files(files, results, errors, skipped):
    for f in files:
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            errors.append({"file": f.filename, "error": "file larger than 50 MB"})
            continue
        try:
            stored, failed = ingest_upload(f.filename or "upload", data, skipped)
            results.extend(stored)
            errors.extend(failed)
        except (InvoiceParseError, llm.LLMUnavailable) as exc:
            errors.append({"file": f.filename, "error": str(exc)})


@app.get("/api/invoices")
def list_invoices():
    records = store.all_invoices()
    for r in records:
        _enrich(r, records)
    return records


def _enrich(record: dict, records: list[dict]) -> dict:
    """Add the computed checks, a plain-language explanation and the indicative tax hint."""
    record["anomalies"] = anomalies(record, records)
    record["explanation"] = explain(record, record["anomalies"])
    record["tax_hint"] = tax_hint(record["category"])
    return record


class InvoicePatch(BaseModel):
    category: str | None = None
    paid: bool | None = None


def _with_anomalies(invoice_id: int) -> dict:
    records = store.all_invoices()
    record = next((r for r in records if r["id"] == invoice_id), None)
    if record is None:
        raise HTTPException(404, "invoice not found")
    return _enrich(record, records)


@app.patch("/api/invoices/{invoice_id}")
def patch_invoice(invoice_id: int, body: InvoicePatch):
    """Override the category (stored with source "manual") and/or mark the invoice paid."""
    if body.category is None and body.paid is None:
        raise HTTPException(400, "nothing to change: send category and/or paid")
    if body.category is not None and body.category not in llm.CATEGORIES:
        raise HTTPException(400, f"category must be one of {llm.CATEGORIES}")
    if store.get(invoice_id) is None:
        raise HTTPException(404, "invoice not found")
    if body.category is not None:
        store.set_category(invoice_id, body.category)
    if body.paid is not None:
        store.set_paid(invoice_id, body.paid)
    return _with_anomalies(invoice_id)


@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int):
    if _demo():
        raise HTTPException(403, "the public demo is read-only")
    if not store.delete(invoice_id):
        raise HTTPException(404, "invoice not found")
    return {"ok": True}


class PaidBody(BaseModel):
    paid: bool


class CategoryBody(BaseModel):
    category: str


@app.post("/api/invoices/{invoice_id}/paid")
def mark_paid(invoice_id: int, body: PaidBody):
    if not store.set_paid(invoice_id, body.paid):
        raise HTTPException(404, "invoice not found")
    return {"ok": True}


@app.post("/api/invoices/{invoice_id}/category")
def change_category(invoice_id: int, body: CategoryBody):
    if body.category not in llm.CATEGORIES:
        raise HTTPException(400, f"category must be one of {llm.CATEGORIES}")
    if not store.set_category(invoice_id, body.category):
        raise HTTPException(404, "invoice not found")
    return {"ok": True}


def _current_month() -> str:
    return date.today().isoformat()[:7]


@app.get("/api/dashboard")
def dashboard(month: str | None = None):
    return month_stats(store.all_invoices(), month or _current_month())


@app.get("/api/summary")
def summary(month: str | None = None):
    stats = month_stats(store.all_invoices(), month or _current_month())
    # The public demo reuses a summary while the numbers are unchanged, so visitors clicking the
    # button do not burn the hosted model's free-tier quota.
    key = json.dumps(stats, sort_keys=True, default=str)
    if _demo() and key in _summary_cache:
        return _summary_cache[key]
    try:
        text = llm.monthly_summary(stats)
    except llm.LLMUnavailable as exc:
        raise HTTPException(503, f"model not reachable: {exc}")
    result = {"model": llm.model_name(), **text}
    if _demo():
        if len(_summary_cache) > 24:
            _summary_cache.clear()
        _summary_cache[key] = result
    return result


_summary_cache: dict[str, dict] = {}


@app.get("/api/export.csv")
def export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        ["id", "date", "number", "supplier", "vat_number", "category", "taxable", "vat", "total", "paid"]
    )
    for r in store.all_invoices():
        inv = r["invoice"]
        writer.writerow(
            [
                r["id"],
                inv["date"],
                csv_safe(inv["number"]),
                csv_safe(inv["supplier"]["name"]),
                csv_safe(inv["supplier"]["vat_number"]),
                r["category"],
                f"{inv['taxable']:.2f}".replace(".", ","),
                f"{inv['tax']:.2f}".replace(".", ","),
                f"{inv['total']:.2f}".replace(".", ","),
                "yes" if r["paid"] else "no",
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoicelens.csv"},
    )


def _deadlines_ics() -> str:
    """Unpaid payment due dates as a calendar file, with a reminder two days before each."""
    stamp = date.today().strftime("%Y%m%d")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//InvoiceLens//EN"]
    for r in store.all_invoices():
        if r["paid"]:
            continue
        inv = r["invoice"]
        for i, p in enumerate(inv["payments"]):
            if not p["due_date"]:
                continue
            summary = f"Pay {inv['supplier']['name']} {p['amount']:.2f} EUR (invoice {inv['number']})"
            summary = summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            summary = summary.replace("\r", " ").replace("\n", " ")
            lines += [
                "BEGIN:VEVENT",
                f"UID:invoice-{r['id']}-{i}@invoicelens",
                f"DTSTAMP:{stamp}T000000Z",
                f"DTSTART;VALUE=DATE:{p['due_date'].replace('-', '')}",
                f"SUMMARY:{summary}",
                "BEGIN:VALARM", "TRIGGER:-P2D", "ACTION:DISPLAY", f"DESCRIPTION:{summary}", "END:VALARM",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.get("/api/deadlines.ics")
def deadlines_ics():
    return StreamingResponse(
        iter([_deadlines_ics()]),
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=invoicelens-deadlines.ics"},
    )


@app.get("/api/accountant-pack.zip")
def accountant_pack_zip(month: str | None = None):
    """One ZIP for the accountant: printable summary (HTML), the month's invoices and checks as CSV,
    and the unpaid due dates as .ics. Built in code; the model is not called."""
    month = month or _current_month()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "month must be YYYY-MM")
    data = accountant_pack(store.all_invoices(), month, _deadlines_ics())
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=invoicelens-commercialista-{month}.zip"},
    )


@app.get("/api/report.xlsx")
def report_xlsx_file(month: str | None = None):
    """The month as an Excel workbook: summary by category, every invoice, and what to check with the rule used."""
    month = month or _current_month()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "month must be YYYY-MM")
    return Response(
        report_xlsx(store.all_invoices(), month),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=invoicelens-{month}.xlsx"},
    )


class WipeBody(BaseModel):
    confirm: str


@app.post("/api/wipe")
def wipe(body: WipeBody):
    """Delete every stored invoice for good. The client must send confirm="DELETE"."""
    if _demo():
        raise HTTPException(403, "the public demo is read-only")
    if body.confirm != "DELETE":
        raise HTTPException(400, 'send {"confirm": "DELETE"} to delete all invoices')
    return {"deleted": store.wipe(), "storage": os.path.abspath(store.db_path())}


def _load_samples(use_model: bool = True) -> int:
    cfg = llm._config()
    loaded = []
    with tracing.agent_run("load_samples", cfg["model"], cfg["provider"]) as agent:
        for path in sorted((ROOT / "samples").glob("*.xml")):
            loaded.extend(ingest(path.name, path.read_bytes(), use_model=use_model))
        tracing.set_data(agent, **{"invoicelens.stored": len(loaded)})
    return len(loaded)


@app.post("/api/demo/load-samples")
def load_samples():
    """Load the bundled fake invoices. In the public demo (INVOICELENS_DEMO=1) this resets the store,
    shows the samples at once with the keyword rules and lets the model recategorize them in the
    background; a local install allows it only while it has no invoices yet."""
    if not _can_load_samples():
        raise HTTPException(403, "sample invoices can only be loaded into an empty InvoiceLens")
    if _demo():
        if ingest_mod.progress["running"]:
            return {"loaded": store.count(), "categorizing": True}
        store.clear()
        loaded = _load_samples(use_model=False)
        return {"loaded": loaded, "categorizing": ingest_mod.recategorize_in_background("demo_reload")}
    return {"loaded": _load_samples()}


@app.post("/api/recategorize")
def recategorize():
    """Ask the model again for every invoice labelled "rules" (imported while it was offline). Runs in the
    background; /api/config reports progress under "background". Manual categories are kept."""
    started = ingest_mod.recategorize_in_background()
    if not started and not ingest_mod.progress["running"]:
        raise HTTPException(503, "model not reachable")
    return {"started": started, **ingest_mod.progress}
