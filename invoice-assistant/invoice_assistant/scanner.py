"""Gemeinsame Scan-Logik für CLI und Web-Oberfläche:
holt Anhänge einer Mail und liefert alle erkannten Rechnungs-Kandidaten.

Über den optionalen notes-Parameter meldet die Funktion, warum etwas
NICHT als Rechnung gewertet wurde — Grundlage für das Scan-Protokoll.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

from .detector import InvoiceCandidate, candidate_from_body, candidate_from_bytes
from .outlook import Message, OutlookClient

MAX_ATTACHMENT_SIZE = 15 * 1024 * 1024

BODY_KEYWORDS = (
    "rechnung", "invoice", "quittung", "receipt", "beleg",
    "billing", "payment", "zahlung", "gutschrift",
)


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
