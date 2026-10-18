"""Anomaly checks and monthly statistics, computed deterministically before the model writes anything."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

VAT_TOLERANCE = 0.02  # FatturaPA allows rounding differences of a cent per summary line
POSSIBLE_DUPLICATE_DAYS = 10  # wider windows would flag normal monthly bills of the same amount


# The exact rule behind each flag, shown next to it so nobody has to trust a black box.
RULES = {
    "duplicate": (
        "Flagged when another invoice has the same supplier VAT number, the same invoice number and the same year.",
        "Segnalata se un'altra fattura ha stessa partita IVA del fornitore, stesso numero e stesso anno.",
    ),
    "amount_conflict": (
        "Flagged when another invoice has the same supplier VAT number, number and year but a different total.",
        "Segnalata se un'altra fattura ha stessa partita IVA, stesso numero e stesso anno ma un totale diverso.",
    ),
    "possible_duplicate": (
        f"Flagged when the same supplier billed the same total within {POSSIBLE_DUPLICATE_DAYS} days under a "
        "different invoice number.",
        f"Segnalata se lo stesso fornitore ha fatturato lo stesso totale entro {POSSIBLE_DUPLICATE_DAYS} giorni "
        "con un numero diverso.",
    ),
    "vat_mismatch": (
        "For each VAT line: taxable × rate, rounded to the cent, must equal the VAT on the invoice within €0.02.",
        "Per ogni riga di riepilogo IVA: imponibile × aliquota, arrotondato al centesimo, deve coincidere con "
        "l'imposta in fattura entro 0,02 €.",
    ),
    "total_mismatch": (
        "Taxable + VAT must equal the declared document total within €0.02.",
        "Imponibile + IVA deve coincidere con il totale documento dichiarato entro 0,02 €.",
    ),
    "overdue": (
        "Flagged when a payment due date is before today and you have not marked the invoice paid.",
        "Segnalata se una scadenza di pagamento è passata e la fattura non è segnata come pagata.",
    ),
}


def _it(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def anomalies(record: dict, others: list[dict], today: date | None = None) -> list[dict]:
    today = today or date.today()
    inv = record["invoice"]
    found = []

    same_number = near_total = None
    for other in others:
        o = other["invoice"]
        if other["id"] == record.get("id") or o["supplier"]["vat_number"] != inv["supplier"]["vat_number"]:
            continue
        if o["number"] == inv["number"] and o["date"][:4] == inv["date"][:4]:
            same_number = same_number or other
        elif (
            abs(o["total"] - inv["total"]) <= VAT_TOLERANCE
            and inv["total"] > 0
            and abs((date.fromisoformat(o["date"]) - date.fromisoformat(inv["date"])).days) <= POSSIBLE_DUPLICATE_DAYS
        ):
            near_total = near_total or other

    if same_number:
        o = same_number["invoice"]
        if abs(o["total"] - inv["total"]) > VAT_TOLERANCE:
            found.append(
                {
                    "kind": "amount_conflict",
                    "message": f"Same number {inv['number']} as invoice #{same_number['id']} but the total is "
                    f"{inv['total']:.2f} here and {o['total']:.2f} there",
                    "message_it": f"Stesso numero {inv['number']} della fattura #{same_number['id']} ma il totale è "
                    f"{_it(inv['total'])} qui e {_it(o['total'])} là",
                }
            )
        else:
            found.append(
                {
                    "kind": "duplicate",
                    "message": f"Same number {inv['number']} as invoice #{same_number['id']} "
                    f"(dated {o['date']}, file {same_number.get('filename', '?')})",
                    "message_it": f"Stesso numero {inv['number']} della fattura #{same_number['id']} "
                    f"(del {o['date']}, file {same_number.get('filename', '?')})",
                }
            )
    elif near_total:
        o = near_total["invoice"]
        found.append(
            {
                "kind": "possible_duplicate",
                "message": f"Same total {inv['total']:.2f} as invoice #{near_total['id']} "
                f"(n. {o['number']}, dated {o['date']}) from the same supplier",
                "message_it": f"Stesso totale {_it(inv['total'])} della fattura #{near_total['id']} "
                f"(n. {o['number']}, del {o['date']}) dello stesso fornitore",
            }
        )

    for row in inv["vat_summary"]:
        expected = round(row["taxable"] * row["rate"] / 100, 2)
        if abs(expected - row["tax"]) > VAT_TOLERANCE:
            found.append(
                {
                    "kind": "vat_mismatch",
                    "message": f"VAT at {row['rate']:g}% on {row['taxable']:.2f} should be "
                    f"{expected:.2f}, invoice says {row['tax']:.2f}",
                    "message_it": f"IVA al {row['rate']:g}% su {_it(row['taxable'])} dovrebbe essere "
                    f"{_it(expected)}, la fattura riporta {_it(row['tax'])}",
                }
            )

    computed = round(inv["taxable"] + inv["tax"], 2)
    if inv.get("declared_total") is not None and abs(computed - inv["declared_total"]) > VAT_TOLERANCE:
        found.append(
            {
                "kind": "total_mismatch",
                "message": f"Declared total {inv['declared_total']:.2f} differs from taxable + VAT "
                f"{computed:.2f} (withholding tax or stamp duty?)",
                "message_it": f"Totale dichiarato {_it(inv['declared_total'])} diverso da imponibile + IVA "
                f"{_it(computed)} (ritenuta o bollo?)",
            }
        )

    if not record.get("paid"):
        for p in inv["payments"]:
            if p["due_date"] and p["due_date"] < today.isoformat():
                found.append(
                    {
                        "kind": "overdue",
                        "message": f"Payment of {p['amount']:.2f} was due {p['due_date']}, "
                        f"{(today - date.fromisoformat(p['due_date'])).days} days ago, and is not marked paid",
                        "message_it": f"Pagamento di {_it(p['amount'])} scaduto il {p['due_date']} "
                        f"({(today - date.fromisoformat(p['due_date'])).days} giorni fa), non segnato come pagato",
                    }
                )
    for a in found:
        a["rule"], a["rule_it"] = RULES[a["kind"]]
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
