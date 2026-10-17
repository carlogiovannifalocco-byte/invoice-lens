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
from .ingest import ingest
from .parser import InvoiceParseError

if os.getenv("SENTRY_DSN"):
    import sentry_sdk

    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0, send_default_pii=False)

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 5 * 1024 * 1024

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


@app.get("/api/config")
def config():
    return {"demo": _demo(), "model": llm.model_name()}


@app.post("/api/invoices")
async def upload(files: list[UploadFile]):
    if _demo():
        raise HTTPException(403, "The public demo only shows the sample invoices. Run InvoiceLens locally for your own files.")
    results, errors = [], []
    for f in files:
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            errors.append({"file": f.filename, "error": "file larger than 5 MB"})
            continue
        try:
            results.extend(ingest(f.filename or "upload", data))
        except (InvoiceParseError, llm.LLMUnavailable) as exc:
            errors.append({"file": f.filename, "error": str(exc)})
    return {"stored": results, "errors": errors}


@app.get("/api/invoices")
def list_invoices():
    records = store.all_invoices()
    for r in records:
        r["anomalies"] = anomalies(r, records)
    return records


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


@app.post("/api/demo/load-samples")
def load_samples():
    """Load the bundled fake invoices. Disabled unless INVOICELENS_DEMO=1 (used by the public demo)."""
    if not _demo():
        raise HTTPException(403, "demo mode is off")
    store.clear()
    loaded = []
    for path in sorted((ROOT / "samples").glob("*.xml")):
        loaded.extend(ingest(path.name, path.read_bytes()))
    return {"loaded": len(loaded)}
