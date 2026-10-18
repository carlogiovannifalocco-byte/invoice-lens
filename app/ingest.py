"""Turn an uploaded file into stored invoices: XML is parsed deterministically, PDFs go through the model."""

from __future__ import annotations

import io
import threading
import zipfile

from pypdf import PdfReader

from . import llm, store, tracing
from .analysis import anomalies
from .parser import InvoiceParseError, parse_fatturapa


def _pdf_invoice(data: bytes) -> dict:
    with tracing.tool("extract_pdf_text", "Read the PDF text layer with pypdf") as sp:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
        tracing.result(sp, {"characters": len(text)})
    if not text.strip():
        raise InvoiceParseError("PDF has no text layer (scanned image?)")
    f = llm.extract_from_text(text)
    taxable, tax, rate = float(f.get("taxable") or 0), float(f.get("tax") or 0), float(f.get("vat_rate") or 0)
    total = float(f.get("total") or taxable + tax)
    return {
        "number": str(f.get("number", "")),
        "date": str(f.get("date", "")),
        "doc_type": "PDF",
        "currency": "EUR",
        "supplier": {"name": f.get("supplier_name", ""), "vat_number": f.get("supplier_vat", ""), "tax_code": "", "country": "IT"},
        "customer": {"name": "", "vat_number": "", "tax_code": "", "country": ""},
        "lines": [{"description": f.get("description", ""), "total": taxable, "vat_rate": rate}],
        "vat_summary": [{"rate": rate, "taxable": taxable, "tax": tax}],
        "payments": [{"due_date": f["due_date"], "amount": total, "method": ""}] if f.get("due_date") else [],
        "declared_total": total,
        "source": f"pdf+{llm.model_name()}",
        "taxable": taxable,
        "tax": tax,
        "total": total,
    }


def ingest(filename: str, data: bytes, skipped: list[dict] | None = None, use_model: bool = True) -> list[dict]:
    """Parse, categorize and store. Invoices already imported are not stored again; when `skipped`
    is given, each one is reported there so the UI can say "duplicate skipped". With use_model=False
    the keyword rules categorize right away (the demo does that, then asks the model in the background)."""
    if filename.lower().endswith(".pdf"):
        invoices = [_pdf_invoice(data)]
    else:
        with tracing.tool("parse_fatturapa", "Parse FatturaPA XML or signed .p7m deterministically",
                          {"file": filename}) as sp:
            invoices = [inv.to_dict() for inv in parse_fatturapa(data)]
            tracing.result(sp, {"invoices": len(invoices)})

    stored = []
    for inv in invoices:
        with tracing.span("function", f"invoice {inv['number']} {inv['supplier']['name']}"[:120],
                          **{"invoicelens.file": filename}) as inv_span:
            with tracing.tool("duplicate_check", "Skip an invoice that is already stored") as sp:
                duplicate = _already_imported(inv)
                tracing.result(sp, {"duplicate": duplicate})
            if duplicate:
                if skipped is not None:
                    skipped.append({"file": filename, "number": inv["number"],
                                    "supplier": inv["supplier"]["name"], "reason": "already imported"})
                continue
            category = _categorize(inv, inv_span) if use_model else _rules(inv)
            record = store.add(filename, inv, category)
            _check(record)
            tracing.set_data(inv_span, **{"invoicelens.category": category["category"],
                                          "invoicelens.category_source": category["source"]})
            stored.append(record)
    return stored


def _rules(inv: dict) -> dict:
    with tracing.tool("keyword_rules", "Keyword fallback used when the model is not reachable") as sp:
        category = llm.categorize_by_rules(inv)
        tracing.result(sp, category)
        return category


def _categorize(inv: dict, inv_span=None) -> dict:
    try:
        return llm.categorize(inv)
    except llm.LLMUnavailable as exc:
        tracing.event(inv_span, [], "fallback: keyword rules", reason=str(exc)[:200])
        return _rules(inv)


def _check(record: dict) -> None:
    """Run the arithmetic checks once at import so the trace shows what was flagged."""
    with tracing.tool("anomaly_checks", "Duplicates, VAT vs rate, totals, overdue: plain arithmetic") as sp:
        if sp is None:
            return
        flags = anomalies(record, store.all_invoices())
        tracing.result(sp, {"flags": [a["kind"] for a in flags]})


_background = threading.Lock()
progress = {"running": False, "done": 0, "total": 0}


def recategorize(task: str = "recategorize") -> int:
    """Ask the model again for every invoice still labelled "rules" (imported while the model was offline,
    or preloaded by the demo). Manual choices are never touched. Stops after two failures in a row."""
    if not _background.acquire(blocking=False):
        return 0
    try:
        pending = [r for r in store.all_invoices() if r["category_source"] == "rules"]
        progress.update(running=True, done=0, total=len(pending))
        cfg = llm._config()
        updated = failures = 0
        with tracing.agent_run(task, cfg["model"], cfg["provider"], len(pending)) as agent:
            for r in pending:
                inv = r["invoice"]
                with tracing.span("function", f"invoice {inv['number']} {inv['supplier']['name']}"[:120]) as sp:
                    try:
                        category = llm.categorize(inv)
                    except llm.LLMUnavailable as exc:
                        failures += 1
                        tracing.event(sp, [], "model unavailable: keeping rules", reason=str(exc)[:200])
                        if failures == 2:
                            break
                        continue
                    failures = 0
                    if store.set_model_category(r["id"], category):
                        updated += 1
                    tracing.set_data(sp, **{"invoicelens.category": category["category"]})
                progress["done"] += 1
            tracing.set_data(agent, **{"invoicelens.updated": updated})
        return updated
    finally:
        progress["running"] = False
        _background.release()


def recategorize_in_background(task: str = "recategorize") -> bool:
    """Start recategorize() in a thread when the model endpoint answers. Returns whether it started."""
    if progress["running"] or not llm.endpoint_reachable():
        return False
    threading.Thread(target=recategorize, args=(task,), daemon=True).start()
    return True


def _already_imported(inv: dict) -> bool:
    """The same document imported twice (e.g. a monthly ZIP re-uploaded) is skipped, not flagged."""
    key = (inv["supplier"]["vat_number"], inv["number"], inv["date"], round(inv["total"], 2))
    return any(
        (r["invoice"]["supplier"]["vat_number"], r["invoice"]["number"], r["invoice"]["date"],
         round(r["invoice"]["total"], 2)) == key
        for r in store.all_invoices()
    )


MAX_ZIP_MEMBERS = 500
MAX_MEMBER_SIZE = 5 * 1024 * 1024
MAX_ZIP_TOTAL = 100 * 1024 * 1024


def ingest_upload(
    filename: str, data: bytes, skipped: list[dict] | None = None
) -> tuple[list[dict], list[dict]]:
    """Ingest one uploaded file. A .zip (like the bulk download from the Agenzia delle Entrate portal)
    is unpacked and each invoice inside is ingested; SDI receipts and other non-invoices are skipped."""
    if not filename.lower().endswith(".zip"):
        return ingest(filename, data, skipped), []
    stored, errors = [], []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [{"file": filename, "error": "not a valid ZIP file"}]
    members = [
        m for m in archive.infolist()
        if not m.is_dir() and not m.filename.startswith("__MACOSX")
        and m.filename.lower().endswith((".xml", ".p7m", ".pdf"))
    ]
    if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_TOTAL:
        return [], [{"file": filename, "error": "ZIP too large"}]
    for m in members:
        name = m.filename.rsplit("/", 1)[-1]
        if m.file_size > MAX_MEMBER_SIZE:
            errors.append({"file": name, "error": "file larger than 5 MB"})
            continue
        if "_MT_" in name or "_RC_" in name or "_NS_" in name:
            continue  # SDI metadata and delivery receipts, not invoices
        try:
            stored.extend(ingest(name, archive.read(m), skipped))
        except (InvoiceParseError, llm.LLMUnavailable) as exc:
            errors.append({"file": name, "error": str(exc)})
    return stored, errors
