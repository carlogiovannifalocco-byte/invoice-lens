"""Anomaly checks and monthly statistics, computed deterministically before the model writes anything."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

VAT_TOLERANCE = 0.02  # FatturaPA allows rounding differences of a cent per summary line


def anomalies(record: dict, others: list[dict], today: date | None = None) -> list[dict]:
    today = today or date.today()
    inv = record["invoice"]
    found = []

    for other in others:
        o = other["invoice"]
        if (
            other["id"] != record.get("id")
            and o["supplier"]["vat_number"] == inv["supplier"]["vat_number"]
            and o["number"] == inv["number"]
            and o["date"][:4] == inv["date"][:4]
        ):
            found.append(
                {"kind": "duplicate", "message": f"Same number {inv['number']} as invoice #{other['id']}"}
            )
            break

    for row in inv["vat_summary"]:
        expected = round(row["taxable"] * row["rate"] / 100, 2)
        if abs(expected - row["tax"]) > VAT_TOLERANCE:
            found.append(
                {
                    "kind": "vat_mismatch",
                    "message": f"VAT at {row['rate']:g}% on {row['taxable']:.2f} should be "
                    f"{expected:.2f}, invoice says {row['tax']:.2f}",
                }
            )

    computed = round(inv["taxable"] + inv["tax"], 2)
    if inv.get("declared_total") is not None and abs(computed - inv["declared_total"]) > VAT_TOLERANCE:
        found.append(
            {
                "kind": "total_mismatch",
                "message": f"Declared total {inv['declared_total']:.2f} differs from taxable + VAT "
                f"{computed:.2f} (withholding tax or stamp duty?)",
            }
        )

    if not record.get("paid"):
        for p in inv["payments"]:
            if p["due_date"] and p["due_date"] < today.isoformat():
                found.append({"kind": "overdue", "message": f"Payment of {p['amount']:.2f} was due {p['due_date']}"})
    return found


def month_stats(records: list[dict], month: str, today: date | None = None) -> dict:
    today = today or date.today()
    in_month = [r for r in records if r["invoice"]["date"].startswith(month)]
    by_category: dict[str, float] = defaultdict(float)
    for r in in_month:
        by_category[r["category"]] += r["invoice"]["taxable"]

    horizon = (today + timedelta(days=30)).isoformat()
    upcoming = sorted(
        (
            {
                "invoice_id": r["id"],
                "supplier": r["invoice"]["supplier"]["name"],
                "due_date": p["due_date"],
                "amount": p["amount"],
            }
            for r in records
            if not r.get("paid")
            for p in r["invoice"]["payments"]
            if p["due_date"] and today.isoformat() <= p["due_date"] <= horizon
        ),
        key=lambda x: x["due_date"],
    )
    flagged = [
        {"invoice_id": r["id"], "supplier": r["invoice"]["supplier"]["name"], **a}
        for r in in_month
        for a in anomalies(r, records, today)
    ]
    return {
        "month": month,
        "invoice_count": len(in_month),
        "taxable_total": round(sum(r["invoice"]["taxable"] for r in in_month), 2),
        "vat_total": round(sum(r["invoice"]["tax"] for r in in_month), 2),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "upcoming_payments": upcoming,
        "anomalies": flagged,
    }
