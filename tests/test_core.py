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
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
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


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOICELENS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    from app.main import app

    return TestClient(app)


def test_zip_bulk_import_skips_receipts_and_reimports(tmp_path, monkeypatch):
    import io
    import zipfile

    client = _client(tmp_path, monkeypatch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ottobre/IT10000000001_a1.xml", sample("01"))
        z.writestr("ottobre/IT10000000002_a2.xml", sample("02"))
        z.writestr("ottobre/IT10000000001_a1_MT_001.xml", b"<MetadatiInvioFile/>")
        z.writestr("__MACOSX/._x.xml", b"junk")
        z.writestr("notes.txt", b"ignored")
    files = [("files", ("ottobre.zip", buf.getvalue(), "application/zip"))]
    first = client.post("/api/invoices", files=files).json()
    assert len(first["stored"]) == 2 and first["errors"] == []
    again = client.post("/api/invoices", files=files).json()
    assert again["stored"] == []
    assert len(client.get("/api/invoices").json()) == 2


def test_bad_zip_reported(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/invoices", files=[("files", ("x.zip", b"nope", "application/zip"))]).json()
    assert res["errors"][0]["error"] == "not a valid ZIP file"


def test_deadlines_calendar_excludes_paid(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/invoices", files=[("files", ("a.xml", sample("01"), "text/xml")),
                                        ("files", ("b.xml", sample("03"), "text/xml"))])
    ids = {r["invoice"]["number"]: r["id"] for r in client.get("/api/invoices").json()}
    client.post(f"/api/invoices/{ids['57']}/paid", json={"paid": True})
    body = client.get("/api/deadlines.ics").text
    assert "DTSTART;VALUE=DATE:20261031" in body
    assert "Studio Bianchi" not in body
    assert "Nuvola Hosting S.r.l. 307.44 EUR" in body


def test_patch_overrides_category_and_paid(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/invoices", files=[("files", ("a.xml", sample("05"), "text/xml"))])
    (rec,) = client.get("/api/invoices").json()
    assert rec["category_source"] == "rules"
    res = client.patch(f"/api/invoices/{rec['id']}", json={"category": "office"})
    assert res.status_code == 200
    body = res.json()
    assert body["category"] == "office" and body["category_source"] == "manual"
    assert body["paid"] is False
    assert any(a["kind"] == "overdue" for a in body["anomalies"])
    paid = client.patch(f"/api/invoices/{rec['id']}", json={"paid": True}).json()
    assert paid["paid"] is True and paid["category"] == "office"
    assert not any(a["kind"] == "overdue" for a in paid["anomalies"])
    assert client.patch(f"/api/invoices/{rec['id']}", json={"category": "pizza"}).status_code == 400
    assert client.patch(f"/api/invoices/{rec['id']}", json={}).status_code == 400
    assert client.patch("/api/invoices/999", json={"paid": True}).status_code == 404


def test_upload_reports_skipped_duplicates(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    files = [("files", ("a.xml", sample("01"), "text/xml"))]
    assert client.post("/api/invoices", files=files).json()["skipped"] == []
    again = client.post("/api/invoices", files=files).json()
    assert again["stored"] == [] and again["errors"] == []
    assert again["skipped"] == [
        {"file": "a.xml", "number": "2026/0412", "supplier": "Nuvola Hosting S.r.l.", "reason": "already imported"}
    ]


def test_delete_invoice_and_demo_is_read_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/invoices", files=[("files", ("a.xml", sample("01"), "text/xml"))])
    (rec,) = client.get("/api/invoices").json()
    assert client.delete(f"/api/invoices/{rec['id']}").json() == {"ok": True}
    assert client.get("/api/invoices").json() == []
    assert client.delete(f"/api/invoices/{rec['id']}").status_code == 404
    monkeypatch.setenv("INVOICELENS_DEMO", "1")
    assert client.delete("/api/invoices/1").status_code == 403


def test_config_reports_model_status_and_samples(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    llm._status_cache.clear()
    cfg = client.get("/api/config").json()
    assert cfg["model"] == llm.model_name()
    assert cfg["model_reachable"] is False
    assert cfg["uploads_enabled"] is True and cfg["can_load_samples"] is True
    assert "software" in cfg["categories"]

    class _Ok:
        status_code = 200

    llm._status_cache.clear()
    monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: _Ok())
    assert client.get("/api/config").json()["model_reachable"] is True
    llm._status_cache.clear()


def test_samples_load_only_into_empty_local_store(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/demo/load-samples")
    assert res.status_code == 200 and res.json()["loaded"] == 10
    assert client.get("/api/config").json()["can_load_samples"] is False
    assert client.post("/api/demo/load-samples").status_code == 403
    monkeypatch.setenv("INVOICELENS_DEMO", "1")
    assert client.post("/api/demo/load-samples").json()["loaded"] == 10
    assert len(client.get("/api/invoices").json()) == 10


def test_retries_once_on_timeout(monkeypatch):
    calls = []

    def flaky_post(url, json, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise llm.httpx.ReadTimeout("model still loading")
        return _FakeResponse('{"category": "software", "confidence": 0.9, "reason": "hosting"}')

    monkeypatch.setattr(llm.httpx, "post", flaky_post)
    inv = parse_fatturapa(sample("01"))[0].to_dict()
    assert llm.categorize(inv)["category"] == "software"
    assert len(calls) == 2


def test_summary_facts_are_plain_and_only_list_real_overdue():
    records = [_record(i, f"{i:02d}") for i in range(1, 11)]
    facts = llm.summary_facts(month_stats(records, "2026-10", TODAY))
    overdue = next(l for l in facts.splitlines() if l.startswith("Overdue:"))
    assert "Coworking Dorsoduro" in overdue and "Accademia Digitale" not in overdue
    assert "VAT that can be reclaimed" in facts and "{" not in facts
