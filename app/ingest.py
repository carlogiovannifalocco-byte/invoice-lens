"""Turn an uploaded file into stored invoices: XML is parsed deterministically, PDFs go through the model."""

from __future__ import annotations

import io
import zipfile

from pypdf import PdfReader

from . import llm, store
from .parser import InvoiceParseError, parse_fatturapa


def _pdf_invoice(data: bytes) -> dict:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
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


def ingest(filename: str, data: bytes) -> list[dict]:
    if filename.lower().endswith(".pdf"):
        invoices = [_pdf_invoice(data)]
    else:
        invoices = [inv.to_dict() for inv in parse_fatturapa(data)]

    stored = []
    for inv in invoices:
        if _already_imported(inv):
            continue
        try:
            category = llm.categorize(inv)
        except llm.LLMUnavailable:
            category = llm.categorize_by_rules(inv)
        stored.append(store.add(filename, inv, category))
    return stored


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


def ingest_upload(filename: str, data: bytes) -> tuple[list[dict], list[dict]]:
    """Ingest one uploaded file. A .zip (like the bulk download from the Agenzia delle Entrate portal)
    is unpacked and each invoice inside is ingested; SDI receipts and other non-invoices are skipped."""
    if not filename.lower().endswith(".zip"):
        return ingest(filename, data), []
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
            stored.extend(ingest(name, archive.read(m)))
        except (InvoiceParseError, llm.LLMUnavailable) as exc:
            errors.append({"file": name, "error": str(exc)})
    return stored, errors
