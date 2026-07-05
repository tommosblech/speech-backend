"""Monatliche, druckbare Zusammenfassung als eigenständige HTML-Datei.

Die Datei ist ohne externe Abhängigkeiten druckbar (Strg+P im Browser)
und gruppiert alle geschäftlichen Rechnungen des Monats nach Kategorie.
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from .config import Config
from .storage import Store

MONTH_NAMES = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

STYLE = """
  body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 2.5rem; color: #1a1a1a; }
  h1 { font-size: 1.6rem; border-bottom: 2px solid #1a1a1a; padding-bottom: .4rem; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  table { border-collapse: collapse; width: 100%; margin-top: .5rem; }
  th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #ccc; font-size: .9rem; }
  th { border-bottom: 2px solid #666; }
  td.num, th.num { text-align: right; white-space: nowrap; }
  tr.total td { font-weight: bold; border-top: 2px solid #666; border-bottom: none; }
  .grand { margin-top: 2rem; font-size: 1.1rem; font-weight: bold; }
  .meta { color: #555; font-size: .85rem; }
  @media print { body { margin: 1cm; } h2 { page-break-after: avoid; } }
"""


def _fmt_amount(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "–"
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} {currency or 'EUR'}"


def generate_monthly_report(config: Config, store: Store, year: int, month: int) -> Path:
    rows = store.invoices_for_month(year, month, kind="geschaeftlich")
    by_category: dict[str, list] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)

    month_name = MONTH_NAMES[month - 1]
    parts = [
        f"<!doctype html><html lang='de'><head><meta charset='utf-8'>",
        f"<title>Geschäftliche Rechnungen {month_name} {year}</title>",
        f"<style>{STYLE}</style></head><body>",
        f"<h1>Geschäftliche Rechnungen – {month_name} {year}</h1>",
        f"<p class='meta'>{len(rows)} Rechnung(en), gruppiert nach Kostenkategorie.</p>",
    ]

    grand_totals: dict[str, float] = defaultdict(float)
    for category in sorted(by_category):
        items = by_category[category]
        parts.append(f"<h2>{html.escape(category)}</h2>")
        parts.append(
            "<table><tr><th>Datum</th><th>Absender</th><th>Betreff/Datei</th>"
            "<th>Rechnungsnr.</th><th class='num'>Betrag</th></tr>"
        )
        cat_totals: dict[str, float] = defaultdict(float)
        for row in items:
            date = (row["invoice_date"] or row["received_at"])[:10]
            if row["amount"] is not None:
                cat_totals[row["currency"] or "EUR"] += row["amount"]
            parts.append(
                "<tr>"
                f"<td>{html.escape(date)}</td>"
                f"<td>{html.escape(row['sender_name'] or row['sender_email'])}</td>"
                f"<td>{html.escape(row['subject'] or row['filename'])}</td>"
                f"<td>{html.escape(row['invoice_number'] or '–')}</td>"
                f"<td class='num'>{_fmt_amount(row['amount'], row['currency'])}</td>"
                "</tr>"
            )
        totals_str = " + ".join(_fmt_amount(v, c) for c, v in sorted(cat_totals.items())) or "–"
        parts.append(
            f"<tr class='total'><td colspan='4'>Summe {html.escape(category)}</td>"
            f"<td class='num'>{totals_str}</td></tr></table>"
        )
        for c, v in cat_totals.items():
            grand_totals[c] += v

    grand_str = " + ".join(_fmt_amount(v, c) for c, v in sorted(grand_totals.items())) or "–"
    parts.append(f"<p class='grand'>Gesamtsumme: {grand_str}</p>")
    parts.append("</body></html>")

    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"rechnungen_{year:04d}-{month:02d}.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
