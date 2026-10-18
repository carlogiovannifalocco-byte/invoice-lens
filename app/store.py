"""SQLite storage. One row per invoice body; the parsed invoice is kept as JSON."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    invoice_json TEXT NOT NULL,
    category TEXT NOT NULL,
    category_reason TEXT NOT NULL DEFAULT '',
    category_source TEXT NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def db_path() -> str:
    return os.getenv("INVOICELENS_DB", "invoicelens.db")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "invoice": json.loads(row["invoice_json"]),
        "category": row["category"],
        "category_reason": row["category_reason"],
        "category_source": row["category_source"],
        "paid": bool(row["paid"]),
    }


def add(filename: str, invoice: dict, category: dict) -> dict:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO invoices (filename, invoice_json, category, category_reason, category_source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                filename,
                json.dumps(invoice, ensure_ascii=False),
                category["category"],
                category.get("reason", ""),
                category["source"],
            ),
        )
        return _row(conn.execute("SELECT * FROM invoices WHERE id = ?", (cur.lastrowid,)).fetchone())


def all_invoices() -> list[dict]:
    with connect() as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id")]


def get(invoice_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _row(row) if row else None


def count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]


def delete(invoice_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,)).rowcount == 1


def set_paid(invoice_id: int, paid: bool) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE invoices SET paid = ? WHERE id = ?", (int(paid), invoice_id))
        return cur.rowcount == 1


def set_category(invoice_id: int, category: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE invoices SET category = ?, category_source = 'manual', category_reason = 'set by you' "
            "WHERE id = ?",
            (category, invoice_id),
        )
        return cur.rowcount == 1


def clear() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM invoices")


def wipe() -> int:
    """Delete every invoice and compact the file, so deleted rows do not linger in free pages."""
    with connect() as conn:
        removed = conn.execute("DELETE FROM invoices").rowcount
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'invoices'")
    conn = sqlite3.connect(db_path())
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return removed
