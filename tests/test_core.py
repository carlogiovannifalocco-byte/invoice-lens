import base64
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import llm, store
from app.analysis import anomalies, month_stats
from app.parser import InvoiceParseError, extract_xml, parse_fatturapa

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
TODAY = date(2026, 10, 17)


def sample(name: str) -> bytes:
    return next(SAMPLES.glob(f"{name}-*.xml")).read_bytes()


def test_parses_supplier_amounts_and_payments():
    (inv,) = parse_fatturapa(sample("01"))
    assert inv.supplier.name == "Nuvola Hosting S.r.l."
    assert inv.supplier.vat_number == "10000000001"
    assert inv.customer.name == "Giulia Rossi"
    assert inv.number == "2026/0412"
    assert [l.description for l in inv.lines] == ["Hosting VPS annuale", "Dominio .it rinnovo"]
    assert float(inv.taxable) == 252.00
    assert float(inv.tax) == 55.44
    assert float(inv.total) == 307.44
    assert inv.payments[0].due_date == "2026-10-31"


def test_multiple_bodies_in_one_file():
    xml = sample("01").decode()
    start = xml.index("<FatturaElettronicaBody>")
    end = xml.index("</FatturaElettronicaBody>") + len("</FatturaElettronicaBody>")
    body = xml[start:end]
    doubled = xml.replace(body, body + body.replace("2026/0412", "2026/0413"))
    assert [i.number for i in parse_fatturapa(doubled.encode())] == ["2026/0412", "2026/0413"]


def test_rejects_non_invoice_and_entity_bombs():
    with pytest.raises(InvoiceParseError):
        parse_fatturapa(b"<root/>")
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><FatturaElettronica>&a;</FatturaElettronica>'
    with pytest.raises(InvoiceParseError):
        parse_fatturapa(bomb)


@pytest.fixture
def p7m(tmp_path):
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=test",
         "-keyout", key, "-out", cert, "-days", "1"],
        check=True, capture_output=True,
    )
    src = tmp_path / "f.xml"
    src.write_bytes(sample("03"))
    out = tmp_path / "f.xml.p7m"
    subprocess.run(
        ["openssl", "cms", "-sign", "-binary", "-nodetach", "-outform", "DER",
         "-in", src, "-signer", cert, "-inkey", key, "-out", out],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def test_extracts_signed_p7m_der_and_base64(p7m):
    assert parse_fatturapa(p7m)[0].supplier.name == "Studio Bianchi Commercialisti"
    assert b"FatturaElettronica" in extract_xml(base64.encodebytes(p7m))


def _record(i, name):
    return {"id": i, "invoice": parse_fatturapa(sample(name))[0].to_dict(), "paid": False, "category": "x"}


def test_anomalies_duplicate_vat_overdue():
    records = [_record(1, "01"), _record(8, "08"), _record(7, "07"), _record(9, "09")]
    kinds = lambda r: {a["kind"] for a in anomalies(r, records, TODAY)}
    assert "duplicate" in kinds(records[1])
    assert "vat_mismatch" in kinds(records[2])
    assert "overdue" in kinds(records[3])
    assert kinds(records[0]) == {"duplicate"}


def test_month_stats_upcoming_and_totals():
    records = [_record(i, f"{i:02d}") for i in range(1, 11)]
    stats = month_stats(records, "2026-10", TODAY)
    assert stats["invoice_count"] == 9
    assert [p["due_date"] for p in stats["upcoming_payments"]][:2] == ["2026-10-20", "2026-10-31"]


def test_rules_fallback_categorizes():
    inv = parse_fatturapa(sample("02"))[0].to_dict()
    assert llm.categorize_by_rules(inv)["category"] == "travel"


def test_api_upload_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("FATTURALENS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")  # unreachable: forces rules fallback
    from app.main import app

    client = TestClient(app)
    res = client.post("/api/invoices", files=[("files", ("01.xml", sample("01"), "text/xml"))])
    assert res.status_code == 200
    assert res.json()["stored"][0]["category_source"] == "rules"
    assert client.get("/api/invoices").json()[0]["invoice"]["number"] == "2026/0412"
    assert "Nuvola Hosting" in client.get("/api/export.csv").text
    bad = client.post("/api/invoices", files=[("files", ("x.xml", b"<nope/>", "text/xml"))])
    assert bad.json()["errors"]


class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def test_model_path_sends_openai_compatible_request(monkeypatch):
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, body=json)
        return _FakeResponse('Sure! {"category": "software", "confidence": 0.9, "reason": "hosting"}')

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setenv("LLM_MODEL", "gemma3:4b")
    inv = parse_fatturapa(sample("01"))[0].to_dict()
    result = llm.categorize(inv)
    assert result == {"category": "software", "confidence": 0.9, "reason": "hosting", "source": "gemma3:4b"}
    assert sent["url"].endswith("/v1/chat/completions")
    assert sent["body"]["model"] == "gemma3:4b"
    assert "Hosting VPS annuale" in sent["body"]["messages"][1]["content"]


def test_model_unknown_category_becomes_other(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _FakeResponse('{"category": "pizza"}'))
    inv = parse_fatturapa(sample("01"))[0].to_dict()
    assert llm.categorize(inv)["category"] == "other"
