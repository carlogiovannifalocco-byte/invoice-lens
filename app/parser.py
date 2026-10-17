"""Deterministic parser for Italian electronic invoices (FatturaPA XML, plain or .p7m signed)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring


@dataclass
class Party:
    name: str = ""
    vat_number: str = ""
    tax_code: str = ""
    country: str = ""


@dataclass
class Line:
    description: str
    total: Decimal
    vat_rate: Decimal


@dataclass
class VatSummary:
    rate: Decimal
    taxable: Decimal
    tax: Decimal


@dataclass
class Payment:
    due_date: str
    amount: Decimal
    method: str = ""


@dataclass
class Invoice:
    number: str
    date: str
    doc_type: str
    currency: str
    supplier: Party
    customer: Party
    lines: list[Line] = field(default_factory=list)
    vat_summary: list[VatSummary] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    declared_total: Decimal | None = None
    source: str = "fatturapa"

    @property
    def taxable(self) -> Decimal:
        return sum((v.taxable for v in self.vat_summary), Decimal("0"))

    @property
    def tax(self) -> Decimal:
        return sum((v.tax for v in self.vat_summary), Decimal("0"))

    @property
    def total(self) -> Decimal:
        if self.declared_total is not None:
            return self.declared_total
        return self.taxable + self.tax

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(taxable=self.taxable, tax=self.tax, total=self.total)
        return _jsonable(data)


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


class InvoiceParseError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: Element | None, *path: str) -> Element | None:
    """Follow a path of local tag names, ignoring XML namespaces."""
    for name in path:
        if node is None:
            return None
        node = next((c for c in node if _local(c.tag) == name), None)
    return node


def _findall(node: Element | None, name: str) -> list[Element]:
    if node is None:
        return []
    return [c for c in node if _local(c.tag) == name]


def _text(node: Element | None, *path: str) -> str:
    found = _find(node, *path)
    return (found.text or "").strip() if found is not None else ""


def _dec(raw: str) -> Decimal:
    try:
        return Decimal(raw.strip()) if raw.strip() else Decimal("0")
    except InvalidOperation as exc:
        raise InvoiceParseError(f"invalid amount: {raw!r}") from exc


def _party(node: Element | None) -> Party:
    anag = _find(node, "DatiAnagrafici")
    name = _text(anag, "Anagrafica", "Denominazione")
    if not name:
        name = " ".join(
            p for p in (_text(anag, "Anagrafica", "Nome"), _text(anag, "Anagrafica", "Cognome")) if p
        )
    return Party(
        name=name,
        vat_number=_text(anag, "IdFiscaleIVA", "IdCodice"),
        tax_code=_text(anag, "CodiceFiscale"),
        country=_text(anag, "IdFiscaleIVA", "IdPaese"),
    )


def extract_xml(data: bytes) -> bytes:
    """Return the FatturaPA XML from plain XML or a CAdES (.p7m) envelope, DER or base64."""
    stripped = data.lstrip()
    if stripped.startswith(b"<"):
        return stripped
    for inform in ("DER", "PEM"):
        payload = data if inform == "DER" else _as_pem(data)
        if payload is None:
            continue
        try:
            out = subprocess.run(
                ["openssl", "cms", "-verify", "-noverify", "-inform", inform, "-out", "-"],
                input=payload,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if out.returncode == 0 and b"FatturaElettronica" in out.stdout:
            return out.stdout
    # Fallback: the signed content is usually stored in clear inside the envelope.
    match = re.search(rb"<\?xml.*?FatturaElettronica>", data, re.S)
    if match:
        return match.group(0)
    raise InvoiceParseError("no FatturaPA XML found in file")


def _as_pem(data: bytes) -> bytes | None:
    body = re.sub(rb"\s+", b"", data)
    if not re.fullmatch(rb"[A-Za-z0-9+/=]+", body or b"-"):
        return None
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return b"-----BEGIN CMS-----\n" + b"\n".join(lines) + b"\n-----END CMS-----\n"


def parse_fatturapa(data: bytes) -> list[Invoice]:
    """Parse a FatturaPA document. A single file may contain several invoice bodies."""
    try:
        root = fromstring(extract_xml(data))
    except InvoiceParseError:
        raise
    except Exception as exc:  # defusedxml raises several error types
        raise InvoiceParseError(f"invalid XML: {exc}") from exc
    if _local(root.tag) != "FatturaElettronica":
        raise InvoiceParseError("root element is not FatturaElettronica")

    header = _find(root, "FatturaElettronicaHeader")
    supplier = _party(_find(header, "CedentePrestatore"))
    customer = _party(_find(header, "CessionarioCommittente"))

    invoices = []
    for body in _findall(root, "FatturaElettronicaBody"):
        doc = _find(body, "DatiGenerali", "DatiGeneraliDocumento")
        goods = _find(body, "DatiBeniServizi")
        declared = _text(doc, "ImportoTotaleDocumento")
        invoices.append(
            Invoice(
                number=_text(doc, "Numero"),
                date=_text(doc, "Data"),
                doc_type=_text(doc, "TipoDocumento"),
                currency=_text(doc, "Divisa") or "EUR",
                supplier=supplier,
                customer=customer,
                lines=[
                    Line(
                        description=_text(l, "Descrizione"),
                        total=_dec(_text(l, "PrezzoTotale")),
                        vat_rate=_dec(_text(l, "AliquotaIVA")),
                    )
                    for l in _findall(goods, "DettaglioLinee")
                ],
                vat_summary=[
                    VatSummary(
                        rate=_dec(_text(r, "AliquotaIVA")),
                        taxable=_dec(_text(r, "ImponibileImporto")),
                        tax=_dec(_text(r, "Imposta")),
                    )
                    for r in _findall(goods, "DatiRiepilogo")
                ],
                payments=[
                    Payment(
                        due_date=_text(p, "DataScadenzaPagamento"),
                        amount=_dec(_text(p, "ImportoPagamento")),
                        method=_text(p, "ModalitaPagamento"),
                    )
                    for dp in _findall(body, "DatiPagamento")
                    for p in _findall(dp, "DettaglioPagamento")
                ],
                declared_total=_dec(declared) if declared else None,
            )
        )
    if not invoices:
        raise InvoiceParseError("no FatturaElettronicaBody found")
    return invoices
