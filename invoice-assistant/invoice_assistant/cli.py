"""Interaktive Kommandozeile des Rechnungsassistenten.

Befehle:
  scan   — Postfach nach Rechnungen durchsuchen und klassifizieren
  report — druckbare Monatszusammenfassung erzeugen
  rules  — gelernte Zuordnungen anzeigen

Der Assistent erklärt vor jedem Schritt, was er tun wird, und fragt nach
Zustimmung (überspringbar mit --yes). Bei unbekannten Absendern fragt er,
ob die Rechnung privat oder geschäftlich ist und zu welcher Kategorie sie
gehört — und merkt sich die Antwort für das nächste Mal.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from .config import load_config
from .detector import InvoiceCandidate
from .outlook import Message
from .report import generate_monthly_report
from .scanner import collect_candidates, make_client
from .storage import Rule, Store


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} → automatisch JA (--yes)")
        return True
    answer = input(f"{question} [j/N] ").strip().lower()
    return answer in ("j", "ja", "y", "yes")


def choose_category(store: Store) -> str:
    categories = store.known_categories()
    print("  Bekannte Kategorien:")
    for i, cat in enumerate(categories, 1):
        print(f"    {i}) {cat}")
    while True:
        raw = input("  Kategorie (Nummer oder neuer Name): ").strip()
        if not raw:
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(categories):
            return categories[int(raw) - 1]
        return raw


def classify_interactively(store: Store, message: Message, assume_yes: bool) -> Rule | None:
    """Fragt den Nutzer nach der Zuordnung und speichert sie als Regel."""
    print(f"\n  Unbekannter Absender: {message.sender_name} <{message.sender_email}>")
    print(f"  Betreff: {message.subject}")

    if assume_yes:
        print("  --yes aktiv: unbekannter Absender wird übersprungen (beim nächsten interaktiven Lauf nachfragen).")
        return None

    while True:
        kind_raw = input("  Privat (p), geschäftlich (g) oder überspringen (s)? ").strip().lower()
        if kind_raw in ("s", ""):
            return None
        if kind_raw in ("p", "privat", "g", "geschaeftlich", "geschäftlich"):
            break
        print("  Bitte p, g oder s eingeben.")
    kind = "privat" if kind_raw.startswith("p") else "geschaeftlich"

    category = choose_category(store) if kind == "geschaeftlich" else "Privat"

    domain = message.sender_email.split("@")[-1]
    scope = input(
        f"  Regel für alle Mails der Domain '{domain}' (d) oder nur diese Adresse (a)? [d] "
    ).strip().lower()
    if scope == "a":
        rule = Rule("email", message.sender_email, kind, category)
    else:
        rule = Rule("domain", domain, kind, category)

    if confirm(f"  Regel speichern und künftig automatisch anwenden ({rule.pattern} → {kind}/{category})?", assume_yes):
        store.save_rule(rule)
        print("  ✔ Regel gespeichert.")
    else:
        print("  Regel nicht gespeichert – gilt nur für diese Rechnung.")
    return rule


def handle_candidate(
    store: Store,
    message: Message,
    candidate: InvoiceCandidate,
    assume_yes: bool,
) -> None:
    if store.already_recorded(message.id, candidate.filename):
        print(f"  Bereits erfasst, übersprungen: {candidate.filename}")
        return

    amount = f"{candidate.amount:.2f} {candidate.currency}" if candidate.amount else "unbekannt"
    print(f"\n► Rechnung erkannt: {candidate.filename}")
    print(f"  Von: {message.sender_name} <{message.sender_email}> am {message.received:%d.%m.%Y}")
    print(f"  Betrag: {amount} | Rechnungsnr.: {candidate.invoice_number or 'unbekannt'}")

    rule = store.find_rule(message.sender_email)
    if rule:
        print(f"  Gelernte Regel angewendet: {rule.pattern} → {rule.kind}/{rule.category}")
        kind, category = rule.kind, rule.category
    else:
        result = classify_interactively(store, message, assume_yes)
        if result is None:
            print("  Übersprungen.")
            return
        kind, category = result.kind, result.category

    stored_path = None
    if kind == "geschaeftlich":
        if confirm(f"  Datei unter data/invoices/{message.received.year}/{message.received.month:02d}/ ablegen?", assume_yes):
            stored_path = store.store_file(
                message.received, message.sender_email, candidate.filename, candidate.content
            )
            print(f"  ✔ Gespeichert: {stored_path}")
    else:
        print("  Privat – Datei wird nicht abgelegt, nur in der Datenbank vermerkt.")

    store.record_invoice(
        message_id=message.id,
        filename=candidate.filename,
        sender_email=message.sender_email,
        sender_name=message.sender_name,
        subject=message.subject,
        received_at=message.received,
        invoice_number=candidate.invoice_number,
        invoice_date=candidate.invoice_date,
        amount=candidate.amount,
        currency=candidate.currency,
        kind=kind,
        category=category,
        stored_path=stored_path,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.source == "graph" and not config.client_id:
        print(
            "Keine Azure client_id konfiguriert.\n"
            "Bitte OUTLOOK_CLIENT_ID setzen oder config.json anlegen (siehe README)."
        )
        return 1
    store = Store(config)

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else datetime.now() - timedelta(days=31)
    until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.now() + timedelta(days=1)

    if config.source == "local":
        print("Schritt 1/4 – Verbinde mit dem lokal installierten Outlook.")
    else:
        print("Schritt 1/4 – Anmeldung bei Microsoft (Device-Code, nur Leserechte).")
    if not confirm("Fortfahren?", args.yes):
        return 0
    client = make_client(config)
    account = client.authenticate()
    print(f"✔ Verbunden: {account}")

    print(f"\nSchritt 2/4 – Suche Mails vom {since:%d.%m.%Y} bis {until:%d.%m.%Y}.")
    if not confirm("Postfach jetzt durchsuchen?", args.yes):
        return 0
    messages = client.search_messages(since, until, query=args.query)
    print(f"✔ {len(messages)} Mail(s) im Zeitraum gefunden.")

    print("\nSchritt 3/4 – Prüfe Anhänge und Mailtexte auf Rechnungen.")
    found = 0
    for message in messages:
        for cand in collect_candidates(client, message):
            found += 1
            handle_candidate(store, message, cand, args.yes)

    print(f"\nSchritt 4/4 – Fertig: {found} Rechnung(en) verarbeitet.")
    print("Tipp: Monatsbericht mit »python -m invoice_assistant report --month YYYY-MM« erzeugen.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import run

    return run(load_config(args.config), port=args.port, open_browser=not args.no_browser)


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config)
    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
    else:
        now = datetime.now()
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    path = generate_monthly_report(config, store, year, month)
    print(f"✔ Druckbarer Bericht erzeugt: {path}")
    print("  Im Browser öffnen und mit Strg+P drucken.")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config)
    rules = store.list_rules()
    if not rules:
        print("Noch keine gelernten Regeln.")
        return 0
    print(f"{'Muster':40} {'Art':15} {'Kategorie':20} Treffer")
    for r in rules:
        print(f"{r['pattern']:40} {r['kind']:15} {r['category']:20} {r['hits']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invoice_assistant",
        description="Durchsucht Outlook-Mails nach Rechnungen, klassifiziert und archiviert sie.",
    )
    parser.add_argument("--config", default="config.json", help="Pfad zur config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p_web = sub.add_parser("web", help="Lokale Web-Oberfläche starten (empfohlen)")
    p_web.add_argument("--port", type=int, default=8321, help="Port (Standard: 8321)")
    p_web.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    p_web.set_defaults(func=cmd_web)

    p_scan = sub.add_parser("scan", help="Postfach nach Rechnungen durchsuchen")
    p_scan.add_argument("--since", help="Startdatum YYYY-MM-DD (Standard: vor 31 Tagen)")
    p_scan.add_argument("--until", help="Enddatum YYYY-MM-DD (Standard: heute)")
    p_scan.add_argument("--query", help="Zusätzliche Volltextsuche, z. B. 'Rechnung'")
    p_scan.add_argument("--yes", action="store_true", help="alle Zustimmungsfragen mit Ja beantworten")
    p_scan.set_defaults(func=cmd_scan)

    p_report = sub.add_parser("report", help="Monatszusammenfassung erzeugen")
    p_report.add_argument("--month", help="Monat als YYYY-MM (Standard: Vormonat)")
    p_report.set_defaults(func=cmd_report)

    p_rules = sub.add_parser("rules", help="Gelernte Zuordnungen anzeigen")
    p_rules.set_defaults(func=cmd_rules)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
