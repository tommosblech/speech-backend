"""Erkennung von Rechnungen und Extraktion der wichtigsten Felder.

Vorgehen: PDF-/Text-Anhänge (und notfalls der Mailtext) werden auf typische
Rechnungsmerkmale geprüft — Schlüsselwörter wie "Rechnung"/"Invoice",
eine Rechnungsnummer und ein Geldbetrag. Zwei unabhängige Treffer gelten
als Rechnung; so lösen bloße Newsletter mit dem Wort "Rechnung" nicht aus.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader

INVOICE_KEYWORDS = [
    "rechnung",
    "invoice",
    "quittung",
    "receipt",
    "zahlungsbeleg",
    "gutschrift",
    "billing statement",
]

INVOICE_NO_RE = re.compile(
    r"(?:rechnungs?-?\s*(?:nummer|nr\.?)|invoice\s*(?:no\.?|number|#))\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/_.]{2,30})",
    re.IGNORECASE,
)

# Beträge: "Gesamtbetrag 119,00 €", "Total: EUR 1.234,56", "$49.00" …
# Wichtig: "Subtotal"/"Zwischensumme" (Netto-Zeilen) dürfen NICHT matchen,
# sonst wird auf US-Rechnungen der Betrag ohne Mehrwertsteuer erfasst.
#
# Prioritätsstufen: Der ZAHLBETRAG (z. B. "Rechnungsendbetrag" bei EnBW nach
# Abzug der Abschläge, oder "€45.52 due" bei manchen Stripe-Rechnungen wie
# Anthropic) schlägt Gesamt-/Bruttobeträge, diese schlagen die generischen
# Summenzeilen. Innerhalb einer Stufe gewinnt der höchste Wert (Brutto >=
# Netto). Ohne die Stufen würde auf Jahresabrechnungen die große Netto-/
# Brutto-Jahressumme statt des zu zahlenden Betrags erfasst.
_AMOUNT_GAP = r"[^\d€$£-]{0,25}(?:€|eur|usd|\$|£)?\s*"
_AMOUNT_NUM = r"(\d{1,3}(?:[.,\s]\d{3})*[.,]\d{2})"
AMOUNT_TIERS = [
    [
        re.compile(
            r"(?:rechnungs-?endbetrag|zu zahlen(?:der betrag)?|zahlbetrag"
            r"|noch zu zahlen|amount\s+due|verbleibende forderung)"
            + _AMOUNT_GAP + _AMOUNT_NUM,
            re.IGNORECASE,
        ),
        # Manche Stripe-Rechnungen (z. B. Anthropic) schreiben den Betrag VOR
        # dem Wort "due": "€45.52 due July 15, 2026" statt "Amount due $45.52".
        re.compile(
            r"(?:€|eur|usd|\$|£)\s*" + _AMOUNT_NUM + r"\s+due\b",
            re.IGNORECASE,
        ),
    ],
    [
        re.compile(
            r"(?:rechnungsbetrag|gesamtbetrag|endbetrag|brutto(?:betrag)?"
            r"|grand\s+total|amount\s+paid|total\s+due)"
            + _AMOUNT_GAP + _AMOUNT_NUM,
            re.IGNORECASE,
        ),
    ],
    [
        re.compile(
            r"(?:gesamt(?:summe)?|(?<!sub)(?<!zwischen)total"
            r"|(?<!zwischen)(?<!zwischen-)summe)"
            + _AMOUNT_GAP + _AMOUNT_NUM,
            re.IGNORECASE,
        ),
    ],
]
ANY_AMOUNT_RE = re.compile(r"(?:€|eur|usd|\$|£)\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})", re.IGNORECASE)

DATE_RE = re.compile(
    r"(?:rechnungsdatum|invoice date|datum|date)\s*[:]?\s*"
    r"(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

TEXT_CONTENT_TYPES = ("text/plain", "text/csv")


@dataclass
class InvoiceCandidate:
    """Eine als Rechnung erkannte Datei bzw. ein erkannter Mailtext."""

    source: str  # "attachment" oder "body"
    filename: str
    content: bytes
    text: str
    score: int
    invoice_number: str | None = None
    amount: float | None = None
    currency: str = "EUR"
    invoice_date: datetime | None = None


def extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _parse_amount(raw: str) -> float | None:
    raw = raw.replace(" ", "")
    # Deutsche Notation 1.234,56 vs. englische 1,234.56 anhand des letzten Trenners
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_date(raw: str) -> datetime | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def analyze_text(text: str, filename: str = "") -> tuple[int, dict]:
    """Bewertet einen Text: Punktzahl >= 2 gilt als Rechnung."""
    lowered = f"{filename}\n{text}".lower()
    score = 0
    fields: dict = {}

    if any(kw in lowered for kw in INVOICE_KEYWORDS):
        score += 1

    m = INVOICE_NO_RE.search(text)
    if m:
        score += 1
        fields["invoice_number"] = m.group(1).strip()

    # Erste Prioritätsstufe mit Treffern gewinnt; innerhalb der Stufe der
    # höchste Wert (Brutto >= Netto). Fallback: größter Betrag mit Währung.
    best: tuple[float, re.Match] | None = None
    for patterns in AMOUNT_TIERS:
        for pattern in patterns:
            for m in pattern.finditer(text):
                amount = _parse_amount(m.group(1))
                if amount is not None and (best is None or amount > best[0]):
                    best = (amount, m)
        if best is not None:
            break
    if best is None:
        for m in ANY_AMOUNT_RE.finditer(text):
            amount = _parse_amount(m.group(1))
            if amount is not None and (best is None or amount > best[0]):
                best = (amount, m)
    if best is not None:
        amount, m = best
        score += 1
        fields["amount"] = amount
        context = text[max(0, m.start() - 5) : m.end() + 5].lower()
        if "$" in context or "usd" in context:
            fields["currency"] = "USD"
        elif "£" in context:
            fields["currency"] = "GBP"

    m = DATE_RE.search(text)
    if m:
        parsed = _parse_date(m.group(1))
        if parsed:
            fields["invoice_date"] = parsed

    return score, fields


def candidate_from_bytes(
    filename: str, content_type: str, data: bytes, source: str = "attachment"
) -> InvoiceCandidate | None:
    """Prüft eine Datei (PDF oder Text) und liefert bei Erkennung einen Kandidaten."""
    name_lower = filename.lower()
    if content_type == "application/pdf" or name_lower.endswith(".pdf"):
        text = extract_pdf_text(data)
    elif content_type in TEXT_CONTENT_TYPES or name_lower.endswith((".txt", ".csv")):
        text = data.decode("utf-8", errors="replace")
    else:
        return None

    score, fields = analyze_text(text, filename)
    # Dateiname wie "Rechnung_2024.pdf" zählt allein schon als Rechnung —
    # wichtig für eingescannte PDFs ohne Textebene, aus denen sich nichts
    # extrahieren lässt (Betrag bleibt dann leer und wird nachgefragt).
    if any(kw in name_lower for kw in INVOICE_KEYWORDS):
        score += 2

    if score < 2:
        return None

    return InvoiceCandidate(
        source=source,
        filename=filename,
        content=data,
        text=text,
        score=score,
        invoice_number=fields.get("invoice_number"),
        amount=fields.get("amount"),
        currency=fields.get("currency", "EUR"),
        invoice_date=fields.get("invoice_date"),
    )


def candidate_from_body(subject: str, body: str) -> InvoiceCandidate | None:
    """Erkennt reine Text-Rechnungen im Mailkörper (ohne Anhang)."""
    score, fields = analyze_text(body, subject)
    if score < 3:  # ohne Datei verlangen wir mehr Sicherheit
        return None
    return InvoiceCandidate(
        source="body",
        filename=f"{re.sub(r'[^A-Za-z0-9._-]+', '_', subject)[:60]}.txt",
        content=body.encode("utf-8"),
        text=body,
        score=score,
        invoice_number=fields.get("invoice_number"),
        amount=fields.get("amount"),
        currency=fields.get("currency", "EUR"),
        invoice_date=fields.get("invoice_date"),
    )
