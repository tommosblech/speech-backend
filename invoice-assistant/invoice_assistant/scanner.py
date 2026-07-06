"""Gemeinsame Scan-Logik für CLI und Web-Oberfläche:
holt Anhänge einer Mail und liefert alle erkannten Rechnungs-Kandidaten.
"""

from __future__ import annotations

from .config import Config
from .detector import InvoiceCandidate, candidate_from_body, candidate_from_bytes
from .outlook import Message, OutlookClient

MAX_ATTACHMENT_SIZE = 15 * 1024 * 1024


def make_client(config: Config):
    """Wählt die Mail-Quelle: lokales Outlook (COM) oder Microsoft-Cloud (Graph)."""
    if config.source == "local":
        from .outlook_local import LocalOutlookClient

        return LocalOutlookClient(config)
    return OutlookClient(config)


def looks_invoice_like(message: Message) -> bool:
    text = f"{message.subject} {message.body_preview}".lower()
    return any(kw in text for kw in ("rechnung", "invoice", "quittung", "receipt", "beleg"))


def collect_candidates(client: OutlookClient, message: Message) -> list[InvoiceCandidate]:
    candidates: list[InvoiceCandidate] = []
    if message.has_attachments:
        for att in client.list_attachments(message):
            if att.size > MAX_ATTACHMENT_SIZE:
                continue
            is_pdf = att.content_type == "application/pdf" or att.name.lower().endswith(".pdf")
            is_text = att.name.lower().endswith((".txt", ".csv"))
            if not (is_pdf or is_text):
                continue
            data = client.download_attachment(message, att)
            cand = candidate_from_bytes(att.name, att.content_type, data)
            if cand:
                candidates.append(cand)
    if not candidates and looks_invoice_like(message):
        body_cand = candidate_from_body(message.subject, client.get_body_text(message))
        if body_cand:
            candidates.append(body_cand)
    return candidates
