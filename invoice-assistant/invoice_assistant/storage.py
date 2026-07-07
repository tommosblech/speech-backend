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

CREATE TABLE IF NOT EXISTS dismissed (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    received_at TEXT NOT NULL,
    subject TEXT,
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

    def get_rule(self, rule_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()

    def update_rule(self, rule_id: int, kind: str, category: str) -> None:
        self.db.execute(
            "UPDATE rules SET kind=?, category=? WHERE id=?", (kind, category, rule_id)
        )
        self.db.commit()

    def reclassify_matching_invoices(
        self, match_type: str, pattern: str, kind: str, category: str
    ) -> int:
        """Wendet eine geänderte Regel rückwirkend auf erfasste Rechnungen an."""
        if match_type == "email":
            cur = self.db.execute(
                "UPDATE invoices SET kind=?, category=? WHERE sender_email=?",
                (kind, category, pattern),
            )
        else:
            cur = self.db.execute(
                "UPDATE invoices SET kind=?, category=? WHERE sender_email LIKE ?",
                (kind, category, f"%@{pattern}"),
            )
        self.db.commit()
        return cur.rowcount

    def known_categories(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT category FROM rules").fetchall()
        learned = [r["category"] for r in rows]
        return sorted(set(self.config.categories) | set(learned))

    # ---------- Rechnungen ----------

    def _fingerprint_match(
        self,
        table: str,
        message_id: str,
        filename: str,
        sender_email: str | None,
        received_at: datetime | None,
    ) -> bool:
        """Duplikat-Prüfung: erst über die Outlook-ID, dann über den stabilen
        Fingerabdruck Absender+Empfangszeit+Dateiname — der übersteht auch
        verschobene Mails, deren interne ID sich dabei ändert."""
        row = self.db.execute(
            f"SELECT 1 FROM {table} WHERE message_id=? AND filename=?",
            (message_id, filename),
        ).fetchone()
        if row:
            return True
        if sender_email and received_at:
            row = self.db.execute(
                f"SELECT 1 FROM {table} WHERE sender_email=? AND received_at=? AND filename=?",
                (sender_email, received_at.isoformat(), filename),
            ).fetchone()
            if row:
                return True
        return False

    def _same_invoice_number(
        self, table: str, sender_email: str | None,
        invoice_number: str | None, amount: float | None,
    ) -> bool:
        """Gleiche Rechnung erkannt an: gleiche Absender-Domain + gleiche
        Rechnungsnummer (+ gleicher Betrag als Sicherheitsnetz)."""
        if not sender_email or not invoice_number or not invoice_number.strip():
            return False
        domain = sender_email.split("@")[-1]
        norm = invoice_number.replace(" ", "").upper()
        sql = (
            f"SELECT 1 FROM {table} "
            "WHERE invoice_number IS NOT NULL "
            "AND REPLACE(UPPER(invoice_number), ' ', '') = ? "
            "AND sender_email LIKE ? "
        )
        params: list = [norm, f"%@{domain}"]
        if amount is None:
            sql += "AND amount IS NULL"
        else:
            sql += "AND amount = ?"
            params.append(amount)
        return self.db.execute(sql, params).fetchone() is not None

    def already_recorded(
        self,
        message_id: str,
        filename: str,
        sender_email: str | None = None,
        received_at: datetime | None = None,
        invoice_number: str | None = None,
        amount: float | None = None,
    ) -> bool:
        return self._fingerprint_match(
            "invoices", message_id, filename, sender_email, received_at
        ) or self._same_invoice_number("invoices", sender_email, invoice_number, amount)

    # ---------- Verworfene Treffer (nie wieder fragen) ----------

    def add_dismissed(
        self, *, message_id: str, filename: str, sender_email: str,
        received_at: str, subject: str | None,
    ) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO dismissed
               (message_id, filename, sender_email, received_at, subject, created_at)
               VALUES (?,?,?,?,?,?)""",
            (message_id, filename, sender_email, received_at, subject,
             datetime.now().isoformat()),
        )
        self.db.commit()

    def is_dismissed(
        self,
        message_id: str,
        filename: str,
        sender_email: str | None = None,
        received_at: datetime | None = None,
    ) -> bool:
        return self._fingerprint_match("dismissed", message_id, filename, sender_email, received_at)

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

    def get_invoice(self, invoice_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()

    def delete_invoice(self, invoice_id: int, *, dismiss: bool = True) -> None:
        """Entfernt eine Rechnung samt Beleg-Datei; mit dismiss=True merkt sich der
        Assistent die Mail, damit sie beim nächsten Scan nicht wieder auftaucht."""
        row = self.get_invoice(invoice_id)
        if not row:
            return
        if row["stored_path"]:
            from .config import absolute_path

            absolute_path(row["stored_path"]).unlink(missing_ok=True)
        if dismiss:
            self.add_dismissed(
                message_id=row["message_id"],
                filename=row["filename"],
                sender_email=row["sender_email"],
                received_at=row["received_at"],
                subject=row["subject"],
            )
        self.db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        self.db.commit()

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

    def categories_with_invoices(self, kind: str = "geschaeftlich") -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT category FROM invoices WHERE kind=? ORDER BY category", (kind,)
        ).fetchall()
        return [r["category"] for r in rows]

    def months_with_invoices(self) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT substr(received_at, 1, 7) AS m FROM invoices ORDER BY m DESC"
        ).fetchall()
        return [r["m"] for r in rows]

    # ---------- Offene Rückfragen (Warteschlange für die Oberfläche) ----------

    def pending_exists(
        self,
        message_id: str,
        filename: str,
        sender_email: str | None = None,
        received_at: datetime | None = None,
        invoice_number: str | None = None,
        amount: float | None = None,
    ) -> bool:
        return self._fingerprint_match(
            "pending", message_id, filename, sender_email, received_at
        ) or self._same_invoice_number("pending", sender_email, invoice_number, amount)

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

    def cleanup_duplicates(self) -> int:
        """Entfernt Alt-Duplikate (gleicher Absender + Empfangszeit + Dateiname),
        die vor der Fingerabdruck-Prüfung entstehen konnten. Behalten wird je
        Gruppe bevorzugt der Eintrag mit abgelegter Beleg-Datei."""
        from .config import absolute_path

        removed = 0
        rows = self.db.execute(
            """SELECT id, sender_email, received_at, filename, stored_path
               FROM invoices
               ORDER BY sender_email, received_at, filename,
                        (stored_path IS NULL), id"""
        ).fetchall()
        kept: dict[tuple, sqlite3.Row] = {}
        for row in rows:
            key = (row["sender_email"], row["received_at"], row["filename"])
            if key in kept:
                keeper = kept[key]
                if row["stored_path"] and row["stored_path"] != keeper["stored_path"]:
                    absolute_path(row["stored_path"]).unlink(missing_ok=True)
                self.db.execute("DELETE FROM invoices WHERE id=?", (row["id"],))
                removed += 1
            else:
                kept[key] = row

        # Zweiter Durchgang: gleiche Absender-Domain + Rechnungsnummer + Betrag
        # (erkennt z. B. weitergeleitete Kopien mit anderem Empfangszeitpunkt)
        rows = self.db.execute(
            """SELECT id, sender_email, invoice_number, amount, stored_path
               FROM invoices
               WHERE invoice_number IS NOT NULL AND TRIM(invoice_number) != ''
               ORDER BY (stored_path IS NULL), id"""
        ).fetchall()
        kept_no: dict[tuple, sqlite3.Row] = {}
        for row in rows:
            key = (
                row["sender_email"].split("@")[-1],
                row["invoice_number"].replace(" ", "").upper(),
                row["amount"],
            )
            if key in kept_no:
                keeper = kept_no[key]
                if row["stored_path"] and row["stored_path"] != keeper["stored_path"]:
                    absolute_path(row["stored_path"]).unlink(missing_ok=True)
                self.db.execute("DELETE FROM invoices WHERE id=?", (row["id"],))
                removed += 1
            else:
                kept_no[key] = row

        pending_rows = self.db.execute(
            """SELECT id, sender_email, received_at, filename, file_path
               FROM pending ORDER BY sender_email, received_at, filename, id"""
        ).fetchall()
        seen: set[tuple] = set()
        for row in pending_rows:
            key = (row["sender_email"], row["received_at"], row["filename"])
            if key in seen:
                absolute_path(row["file_path"]).unlink(missing_ok=True)
                self.db.execute("DELETE FROM pending WHERE id=?", (row["id"],))
                removed += 1
            else:
                seen.add(key)

        self.db.commit()
        return removed

    def reset(self, *, keep_rules: bool = True) -> None:
        """Alle erfassten Daten löschen für einen frischen Scan.

        keep_rules=True behält die gelernten Zuordnungsregeln (empfohlen),
        sodass der neue Scan bekannte Absender wieder automatisch einsortiert.
        """
        import shutil

        self.db.execute("DELETE FROM invoices")
        self.db.execute("DELETE FROM pending")
        self.db.execute("DELETE FROM dismissed")
        if not keep_rules:
            self.db.execute("DELETE FROM rules")
        self.db.commit()
        for folder in (self.config.invoices_dir, self.config.pending_dir, self.config.reports_dir):
            shutil.rmtree(folder, ignore_errors=True)

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
