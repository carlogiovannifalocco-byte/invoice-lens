"""Turn an uploaded file into stored invoices: XML is parsed deterministically, PDFs go through the model."""

from __future__ import annotations

import io

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
        try:
            category = llm.categorize(inv)
        except llm.LLMUnavailable:
            category = llm.categorize_by_rules(inv)
        stored.append(store.add(filename, inv, category))
    return stored
