"""Gemeinsame Scan-Logik für CLI und Web-Oberfläche:
holt Anhänge einer Mail und liefert alle erkannten Rechnungs-Kandidaten.

Über den optionalen notes-Parameter meldet die Funktion, warum etwas
NICHT als Rechnung gewertet wurde — Grundlage für das Scan-Protokoll.
"""

from __future__ import annotations

import calendar
import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath

from .detector import InvoiceCandidate, analyze_text, candidate_from_body, candidate_from_bytes, extract_pdf_text
from .outlook import Message, OutlookClient
from .report import MONTH_NAMES

MAX_ATTACHMENT_SIZE = 15 * 1024 * 1024

BODY_KEYWORDS = (
    "rechnung", "invoice", "quittung", "receipt", "beleg",
    "billing", "payment", "zahlung", "gutschrift",
)

IMAGE_EXTS = (".jpg", ".jpeg", ".png")

# ---------- Beleg-Mails: selbst eingescannte Papierbelege ----------
# Konvention: Betreff beginnt mit "Beleg"/"Belege" (z. B. "Belege Juni",
# "Belege 2026-06"). Dann gilt JEDER Anhang als Beleg und wird einzeln zur
# Zuordnung vorgelegt — ohne Absender-Regeln, denn der Absender bist du selbst.


def is_receipt_mail(message: Message) -> bool:
    return message.subject.strip().lower().startswith(("beleg", "#beleg"))


_MONTH_BY_NAME = {name.lower(): i + 1 for i, name in enumerate(MONTH_NAMES)}


def _month_from_subject(subject: str, received: datetime) -> tuple[int, int] | None:
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})\b", subject)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(\d{1,2})[/.](20\d{2})\b", subject)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(2)), int(m.group(1))
    lowered = subject.lower()
    for name, month in _MONTH_BY_NAME.items():
        if name in lowered:
            year_match = re.search(r"\b(20\d{2})\b", subject)
            year = int(year_match.group(1)) if year_match else received.year
            if not year_match and month > received.month:
                year -= 1  # z. B. "Belege Dezember" im Januar verschickt
            return year, month
    return None


def is_previous_month_sender(message: Message, patterns: tuple | list = ()) -> bool:
    """Absender (z. B. EnBW), deren Rechnungen Anfang des Monats für den
    Vormonat kommen — Teilstring-Abgleich mit der Absender-Domain."""
    domain = message.sender_email.split("@")[-1].lower()
    return any(p.lower() in domain for p in patterns if p.strip())


def effective_date(message: Message, late_senders: tuple | list = ()) -> datetime:
    """Buchungsdatum einer Mail: normale Mails = Empfangszeit. Beleg-Mails und
    Vormonats-Absender (z. B. EnBW): in den ersten 10 Tagen des Monats
    eingetroffen → Vormonat; bei Beleg-Mails gewinnt ein Monat im Betreff.
    Ergebnis ist jeweils der Monatsletzte."""
    received = message.received.replace(tzinfo=None) if message.received.tzinfo else message.received
    receipt = is_receipt_mail(message)
    late = is_previous_month_sender(message, late_senders)
    if not receipt and not late:
        return message.received
    target = _month_from_subject(message.subject, received) if receipt else None
    if target:
        year, month = target
    elif received.day <= 10:
        prev = received.replace(day=1) - timedelta(days=1)
        year, month = prev.year, prev.month
    elif receipt:
        year, month = received.year, received.month
    else:
        return message.received  # Vormonats-Absender nach dem 10.: normales Datum
    return datetime(year, month, calendar.monthrange(year, month)[1], 12, 0)


def _make_receipt(name: str, data: bytes, text: str) -> InvoiceCandidate:
    fields: dict = {}
    if text:
        _, fields = analyze_text(text, name)
    return InvoiceCandidate(
        source="beleg",
        filename=name,
        content=data,
        text=text,
        score=99,
        invoice_number=fields.get("invoice_number"),
        amount=fields.get("amount"),
        currency=fields.get("currency", "EUR"),
        invoice_date=fields.get("invoice_date"),
    )


def _receipt_candidates(name: str, content_type: str, data: bytes) -> list[InvoiceCandidate]:
    """In einer Beleg-Mail ist jeder Anhang per Definition ein Beleg.
    Mehrseitige PDFs (Sammel-Scans) werden in einen Beleg PRO SEITE zerlegt;
    Felder wie der Betrag werden je Seite extrahiert, wo möglich."""
    lower = name.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader, PdfWriter

            reader = PdfReader(io.BytesIO(data))
            pages = list(reader.pages)
        except Exception:
            pages = []
        if len(pages) > 1:
            stem = name[:-4] if lower.endswith(".pdf") else name
            result: list[InvoiceCandidate] = []
            for i, page in enumerate(pages, 1):
                writer = PdfWriter()
                writer.add_page(page)
                buf = io.BytesIO()
                writer.write(buf)
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                result.append(_make_receipt(f"{stem}_Beleg{i:02d}.pdf", buf.getvalue(), text))
            return result
        return [_make_receipt(name, data, extract_pdf_text(data))]
    if lower.endswith((".txt", ".csv")):
        return [_make_receipt(name, data, data.decode("utf-8", errors="replace"))]
    return [_make_receipt(name, data, "")]


def make_client(config):
    """Wählt die Mail-Quelle: lokales Outlook (COM) oder Microsoft-Cloud (Graph)."""
    if config.source == "local":
        from .outlook_local import LocalOutlookClient

        return LocalOutlookClient(config)
    return OutlookClient(config)


def looks_invoice_like(message: Message) -> bool:
    text = f"{message.subject} {message.body_preview}".lower()
    return any(kw in text for kw in BODY_KEYWORDS)


def _basename(member: str) -> str:
    return PureWindowsPath(PurePosixPath(member).name).name or "anhang"


def collect_candidates(
    client, message: Message, notes: list[str] | None = None
) -> list[InvoiceCandidate]:
    def note(text: str) -> None:
        if notes is not None:
            notes.append(text)

    candidates: list[InvoiceCandidate] = []

    if is_receipt_mail(message):
        if not message.has_attachments:
            note("Beleg-Mail ohne Anhänge — nichts zu übernehmen")
            return []
        for att in client.list_attachments(message):
            lower = att.name.lower()
            if not (lower.endswith((".pdf", ".txt", ".csv")) or lower.endswith(IMAGE_EXTS)):
                note(f"Beleg-Mail: Anhang „{att.name}“ übersprungen (nur PDF, Bilder, TXT, CSV)")
                continue
            if att.size > MAX_ATTACHMENT_SIZE:
                note(f"Beleg-Mail: Anhang „{att.name}“ übersprungen: größer als 15 MB")
                continue
            data = client.download_attachment(message, att)
            receipts = _receipt_candidates(att.name, att.content_type, data)
            if len(receipts) > 1:
                note(f"Beleg-Mail: „{att.name}“ in {len(receipts)} Einzelbelege (pro Seite) zerlegt")
            candidates.extend(receipts)
        return candidates

    if message.has_attachments:
        for att in client.list_attachments(message):
            name_lower = att.name.lower()
            is_pdf = att.content_type == "application/pdf" or name_lower.endswith(".pdf")
            is_text = name_lower.endswith((".txt", ".csv"))
            is_zip = name_lower.endswith(".zip")
            if not (is_pdf or is_text or is_zip):
                if not name_lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".ics", ".vcf")):
                    note(f"Anhang „{att.name}“: Dateityp wird nicht geprüft (nur PDF, TXT, CSV, ZIP)")
                continue
            if att.size > MAX_ATTACHMENT_SIZE:
                note(f"Anhang „{att.name}“ übersprungen: größer als 15 MB")
                continue
            data = client.download_attachment(message, att)

            if is_zip:
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        for member in zf.namelist():
                            if not member.lower().endswith((".pdf", ".txt", ".csv")):
                                continue
                            cand = candidate_from_bytes(_basename(member), "", zf.read(member))
                            if cand:
                                candidates.append(cand)
                            else:
                                note(f"„{att.name}“ → „{member}“: keine Rechnungsmerkmale erkannt")
                except Exception:
                    note(f"Anhang „{att.name}“: ZIP-Datei nicht lesbar")
                continue

            cand = candidate_from_bytes(att.name, att.content_type, data)
            if cand:
                candidates.append(cand)
            else:
                note(f"Anhang „{att.name}“: keine Rechnungsmerkmale erkannt "
                     f"(kein Stichwort/Betrag/Rechnungsnr. im Inhalt)")

    if not candidates and looks_invoice_like(message):
        body_cand = candidate_from_body(message.subject, client.get_body_text(message))
        if body_cand:
            candidates.append(body_cand)
        elif not message.has_attachments:
            note("Betreff klingt nach Rechnung, aber im Mailtext fehlen Rechnungsmerkmale "
                 "(Betrag/Rechnungsnr.) — vermutlich nur ein Hinweis oder Portal-Link")
    return candidates
