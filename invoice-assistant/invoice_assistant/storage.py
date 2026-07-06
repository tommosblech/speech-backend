"""Persistenz: SQLite-Datenbank + Dateiablage.

Tabellen:
  rules    — gelernte Zuordnungen (Absender/Domain -> privat/geschäftlich + Kategorie)
  invoices — alle erfassten Rechnungen inkl. Klassifizierung und Ablagepfad

Geschäftliche Rechnungen werden zusätzlich als Datei unter
data/invoices/JAHR/MONAT/ abgelegt.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    match_type TEXT NOT NULL,          -- 'email' oder 'domain'
    pattern TEXT NOT NULL UNIQUE,      -- z. B. billing@openai.com oder openai.com
    kind TEXT NOT NULL,                -- 'geschaeftlich' oder 'privat'
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    sender_name TEXT,
    subject TEXT,
    received_at TEXT NOT NULL,
    invoice_number TEXT,
    invoice_date TEXT,
    amount REAL,
    currency TEXT,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    stored_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, filename)
);

CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    sender_name TEXT,
    subject TEXT,
    received_at TEXT NOT NULL,
    invoice_number TEXT,
    invoice_date TEXT,
    amount REAL,
    currency TEXT,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, filename)
);
"""


@dataclass
class Rule:
    match_type: str
    pattern: str
    kind: str
    category: str


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- Regeln (das "Gedächtnis" des Assistenten) ----------

    def find_rule(self, sender_email: str) -> Rule | None:
        """Sucht erst nach exakter Adresse, dann nach der Domain."""
        domain = sender_email.split("@")[-1]
        row = self.db.execute(
            """SELECT * FROM rules
               WHERE (match_type='email' AND pattern=?)
                  OR (match_type='domain' AND pattern=?)
               ORDER BY match_type='email' DESC LIMIT 1""",
            (sender_email, domain),
        ).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE rules SET hits = hits + 1 WHERE id=?", (row["id"],))
        self.db.commit()
        return Rule(row["match_type"], row["pattern"], row["kind"], row["category"])

    def save_rule(self, rule: Rule) -> None:
        self.db.execute(
            """INSERT INTO rules (match_type, pattern, kind, category, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(pattern) DO UPDATE SET kind=excluded.kind, category=excluded.category""",
            (rule.match_type, rule.pattern, rule.kind, rule.category, datetime.now().isoformat()),
        )
        self.db.commit()

    def list_rules(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM rules ORDER BY pattern").fetchall()

    def delete_rule(self, rule_id: int) -> None:
        self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        self.db.commit()

    def known_categories(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT category FROM rules").fetchall()
        learned = [r["category"] for r in rows]
        return sorted(set(self.config.categories) | set(learned))

    # ---------- Rechnungen ----------

    def already_recorded(self, message_id: str, filename: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM invoices WHERE message_id=? AND filename=?",
            (message_id, filename),
        ).fetchone()
        return row is not None

    def record_invoice(
        self,
        *,
        message_id: str,
        filename: str,
        sender_email: str,
        sender_name: str,
        subject: str,
        received_at: datetime,
        invoice_number: str | None,
        invoice_date: datetime | None,
        amount: float | None,
        currency: str,
        kind: str,
        category: str,
        stored_path: Path | None,
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO invoices
               (message_id, filename, sender_email, sender_name, subject, received_at,
                invoice_number, invoice_date, amount, currency, kind, category,
                stored_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                message_id,
                filename,
                sender_email,
                sender_name,
                subject,
                received_at.isoformat(),
                invoice_number,
                invoice_date.isoformat() if invoice_date else None,
                amount,
                currency,
                kind,
                category,
                str(stored_path) if stored_path else None,
                datetime.now().isoformat(),
            ),
        )
        self.db.commit()

    def store_file(self, received_at: datetime, sender_email: str, filename: str, data: bytes) -> Path:
        """Legt die Rechnungsdatei unter data/invoices/JAHR/MONAT/ ab."""
        folder = self.config.invoices_dir / f"{received_at.year:04d}" / f"{received_at.month:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "rechnung"
        prefix = re.sub(r"[^A-Za-z0-9.-]+", "_", sender_email)
        path = folder / f"{received_at.strftime('%Y-%m-%d')}_{prefix}_{safe}"
        counter = 1
        while path.exists():
            path = folder / f"{received_at.strftime('%Y-%m-%d')}_{prefix}_{counter}_{safe}"
            counter += 1
        path.write_bytes(data)
        return path

    def list_invoices(self, month: str | None = None, kind: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        """Rechnungen für die Oberfläche; month als 'YYYY-MM'."""
        sql = "SELECT * FROM invoices WHERE 1=1"
        params: list = []
        if month:
            sql += " AND substr(received_at, 1, 7) = ?"
            params.append(month)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def months_with_invoices(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT substr(received_at, 1, 7) AS m FROM invoices ORDER BY m DESC"
        ).fetchall()
        return [r["m"] for r in rows]

    # ---------- Offene Rückfragen (Warteschlange für die Oberfläche) ----------

    def pending_exists(self, message_id: str, filename: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM pending WHERE message_id=? AND filename=?",
            (message_id, filename),
        ).fetchone()
        return row is not None

    def add_pending(
        self,
        *,
        message_id: str,
        filename: str,
        sender_email: str,
        sender_name: str,
        subject: str,
        received_at: datetime,
        invoice_number: str | None,
        invoice_date: datetime | None,
        amount: float | None,
        currency: str,
        content: bytes,
    ) -> None:
        """Legt eine unklassifizierte Rechnung samt Datei zur späteren Rückfrage ab."""
        self.config.pending_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "rechnung"
        path = self.config.pending_dir / f"{received_at.strftime('%Y%m%d%H%M%S')}_{safe}"
        counter = 1
        while path.exists():
            path = self.config.pending_dir / f"{received_at.strftime('%Y%m%d%H%M%S')}_{counter}_{safe}"
            counter += 1
        path.write_bytes(content)
        self.db.execute(
            """INSERT OR IGNORE INTO pending
               (message_id, filename, sender_email, sender_name, subject, received_at,
                invoice_number, invoice_date, amount, currency, file_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                message_id,
                filename,
                sender_email,
                sender_name,
                subject,
                received_at.isoformat(),
                invoice_number,
                invoice_date.isoformat() if invoice_date else None,
                amount,
                currency,
                str(path),
                datetime.now().isoformat(),
            ),
        )
        self.db.commit()

    def list_pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM pending ORDER BY received_at").fetchall()

    def count_pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM pending").fetchone()["n"]

    def get_pending(self, pending_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM pending WHERE id=?", (pending_id,)).fetchone()

    def delete_pending(self, pending_id: int) -> None:
        row = self.get_pending(pending_id)
        if row:
            Path(row["file_path"]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM pending WHERE id=?", (pending_id,))
            self.db.commit()

    def invoices_for_month(self, year: int, month: int, kind: str | None = "geschaeftlich") -> list[sqlite3.Row]:
        start = f"{year:04d}-{month:02d}-01"
        end = f"{year + (month == 12):04d}-{(month % 12) + 1:02d}-01"
        sql = "SELECT * FROM invoices WHERE received_at >= ? AND received_at < ?"
        params: list = [start, end]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY category, received_at"
        return self.db.execute(sql, params).fetchall()
