"""InvoiceLens HTTP API and web UI."""

from __future__ import annotations

import csv
import io
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import llm, store
from .analysis import anomalies, month_stats
from .ingest import ingest, ingest_upload
from .parser import InvoiceParseError

if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0, send_default_pii=False)

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 50 * 1024 * 1024  # ZIP archives of a month of invoices

app = FastAPI(title="InvoiceLens")


@app.get("/")
def index():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/api/health")
def health():
    try:
        llm.chat_json('Reply with JSON {"ok": true}.', "ping", "health")
        model_ok = True
    except llm.LLMUnavailable:
        model_ok = False
    return {"model": llm.model_name(), "model_reachable": model_ok}


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
    }


@app.post("/api/invoices")
async def upload(files: list[UploadFile]):
    if _demo():
        raise HTTPException(403, "The public demo only shows the sample invoices. Run InvoiceLens locally for your own files.")
    results, errors, skipped = [], [], []
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
    return {"stored": results, "errors": errors, "skipped": skipped}


@app.get("/api/invoices")
def list_invoices():
    records = store.all_invoices()
    for r in records:
        r["anomalies"] = anomalies(r, records)
    return records


class InvoicePatch(BaseModel):
    category: str | None = None
    paid: bool | None = None


def _with_anomalies(invoice_id: int) -> dict:
    records = store.all_invoices()
    record = next((r for r in records if r["id"] == invoice_id), None)
    if record is None:
        raise HTTPException(404, "invoice not found")
    record["anomalies"] = anomalies(record, records)
    return record


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
    try:
        text = llm.monthly_summary(stats)
    except llm.LLMUnavailable as exc:
        raise HTTPException(503, f"model not reachable: {exc}")
    return {"model": llm.model_name(), **text}


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
                inv["number"],
                inv["supplier"]["name"],
                inv["supplier"]["vat_number"],
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


@app.get("/api/deadlines.ics")
def deadlines_ics():
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
    return StreamingResponse(
        iter(["\r\n".join(lines) + "\r\n"]),
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=invoicelens-deadlines.ics"},
    )


@app.post("/api/demo/load-samples")
def load_samples():
    """Load the bundled fake invoices. In the public demo (INVOICELENS_DEMO=1) this resets the store;
    a local install allows it only while it has no invoices yet."""
    if not _can_load_samples():
        raise HTTPException(403, "sample invoices can only be loaded into an empty InvoiceLens")
    if _demo():
        store.clear()
    loaded = []
    for path in sorted((ROOT / "samples").glob("*.xml")):
        loaded.extend(ingest(path.name, path.read_bytes()))
    return {"loaded": len(loaded)}
