"""Monats-Gesamt-PDF: Deckblatt mit Kategorien-Übersicht, dahinter alle
Rechnungsdateien des Monats in einem einzigen, druckbaren PDF.

PDF-Anhänge werden direkt übernommen; Text-Rechnungen (z. B. aus dem
Mailkörper) werden als eigene PDF-Seite gesetzt. Nicht lesbare Dateien
bekommen eine Hinweis-Seite, damit im Ausdruck nichts unbemerkt fehlt.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

from .config import Config
from .report import MONTH_NAMES
from .storage import Store


def _latin(text: str) -> str:
    """Auf den Zeichenvorrat der PDF-Grundschriften eindampfen."""
    return str(text).replace("€", "EUR").replace("–", "-").encode("latin-1", "replace").decode("latin-1")


def _fmt(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "-"
    s = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {currency or 'EUR'}"


def _as_reader(pdf: FPDF) -> PdfReader:
    return PdfReader(io.BytesIO(bytes(pdf.output())))


def _text_page(title: str, body: str) -> PdfReader:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(0, 8, _latin(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Courier", size=9)
    for line in body.splitlines()[:400]:
        pdf.multi_cell(0, 4.5, _latin(line[:120]) or " ", new_x="LMARGIN", new_y="NEXT")
    return _as_reader(pdf)


def _cover(rows, year: int, month: int) -> PdfReader:
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin(f"Geschäftliche Rechnungen - {MONTH_NAMES[month - 1]} {year}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 6, _latin(f"{len(rows)} Rechnung(en) - erstellt am {datetime.now():%d.%m.%Y} - "
                          "Belege folgen auf den nächsten Seiten"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    grand: dict[str, float] = defaultdict(float)

    for cat in sorted(by_cat):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, _latin(cat), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        cat_tot: dict[str, float] = defaultdict(float)
        for r in by_cat[cat]:
            date = (r["invoice_date"] or r["received_at"])[:10]
            date = f"{date[8:10]}.{date[5:7]}.{date[:4]}"
            left = f"{date}   {r['sender_name'] or r['sender_email']}   {r['subject'] or r['filename']}"
            pdf.cell(150, 6, _latin(left)[:95])
            pdf.cell(0, 6, _latin(_fmt(r["amount"], r["currency"])), align="R",
                     new_x="LMARGIN", new_y="NEXT")
            if r["amount"] is not None:
                cat_tot[r["currency"] or "EUR"] += r["amount"]
        pdf.set_font("Helvetica", "B", 9)
        tot = " + ".join(_fmt(v, c) for c, v in sorted(cat_tot.items())) or "-"
        pdf.cell(150, 6, _latin(f"Summe {cat}"))
        pdf.cell(0, 6, _latin(tot), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        for c, v in cat_tot.items():
            grand[c] += v

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    grand_s = " + ".join(_fmt(v, c) for c, v in sorted(grand.items())) or "-"
    pdf.cell(0, 8, _latin(f"Gesamtsumme: {grand_s}"), new_x="LMARGIN", new_y="NEXT")
    return _as_reader(pdf)


def generate_monthly_bundle(config: Config, store: Store, year: int, month: int) -> Path:
    rows = store.invoices_for_month(year, month, kind="geschaeftlich")
    writer = PdfWriter()
    writer.append(_cover(rows, year, month))

    for r in rows:
        label = f"{r['sender_name'] or r['sender_email']} - {r['subject'] or r['filename']}"
        path = Path(r["stored_path"]) if r["stored_path"] else None
        try:
            if path and path.exists() and path.suffix.lower() == ".pdf":
                writer.append(PdfReader(str(path)))
            elif path and path.exists():
                writer.append(_text_page(label, path.read_text(encoding="utf-8", errors="replace")))
            else:
                writer.append(_text_page(label, "(Zu dieser Rechnung wurde keine Datei abgelegt.)"))
        except Exception as exc:
            writer.append(_text_page(label, f"(Datei konnte nicht eingebunden werden: {exc})"))

    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"rechnungen_{year:04d}-{month:02d}_gesamt.pdf"
    with open(out, "wb") as fh:
        writer.write(fh)
    return out
