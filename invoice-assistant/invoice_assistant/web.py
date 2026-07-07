"""Lokale Web-Oberfläche des Rechnungsassistenten.

Startet einen Server auf 127.0.0.1 (nur dieser Rechner) und öffnet den
Browser. Anmeldung, Scan, Rückfragen zu unbekannten Rechnungen, Regeln
und Monatsberichte werden per Klick bedient. Der Scan läuft im
Hintergrund; unbekannte Absender landen in »Offene Fragen« und werden
dort klassifiziert — die Antwort wird als Regel gelernt.
"""

from __future__ import annotations

import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

import os
import re

from . import updater
from .bundle import bundle_filename, generate_monthly_bundle
from .config import Config, absolute_path
from .report import generate_monthly_report
from .scanner import collect_candidates, make_client
from .storage import Rule, Store


class AppState:
    """Zustand von Anmeldung und laufendem Scan, geteilt zwischen Threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.account: str | None = None
        self.auth: dict = {"status": "none"}
        self.scan: dict = {"status": "idle"}
        self.scan_log: dict | None = None
        self.update: dict = {"status": "idle"}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "account": self.account,
                "auth": dict(self.auth),
                "scan": dict(self.scan),
                "update": dict(self.update),
            }


def _schedule_restart() -> None:
    """Beendet den Prozess mit Code 42 — die Startdatei startet dann neu."""
    threading.Timer(1.5, lambda: os._exit(42)).start()


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    state = AppState()
    client = make_client(config)

    def store() -> Store:
        return Store(config)

    # Alt-Duplikate aus Versionen ohne Fingerabdruck-Prüfung einmalig bereinigen
    try:
        cleaned = store().cleanup_duplicates()
        if cleaned:
            print(f"{cleaned} doppelte(r) Eintrag/Einträge bereinigt.", flush=True)
    except Exception:
        pass

    # Beim Start still verbinden (lokales Outlook direkt, Graph aus dem Token-Cache) —
    # im Hintergrund, damit die Oberfläche sofort erreichbar ist, auch wenn
    # Outlook langsam startet oder hängt.
    def silent_worker() -> None:
        try:
            account = client.try_silent()
        except Exception:
            account = None
        with state.lock:
            if account:
                state.account = account
                state.auth = {"status": "done"}
            elif state.auth.get("status") == "working":
                state.auth = {"status": "none"}

    if config.source == "local" or config.client_id:
        state.auth = {"status": "working"}
        threading.Thread(target=silent_worker, daemon=True).start()

    @app.context_processor
    def inject_globals():
        return {
            "pending_count": store().count_pending(),
            "account": state.account,
            "configured": bool(config.client_id),
            "source": config.source,
            "version": updater.current_version(),
        }

    @app.template_filter("euro")
    def euro(row) -> str:
        if row["amount"] is None:
            return "–"
        formatted = f"{row['amount']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{formatted} {row['currency'] or 'EUR'}"

    @app.template_filter("datum")
    def datum(iso: str | None) -> str:
        if not iso:
            return "–"
        return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}"

    # ---------- Anmeldung ----------

    def auth_worker() -> None:
        def on_code(flow: dict) -> None:
            with state.lock:
                state.auth = {
                    "status": "pending",
                    "user_code": flow["user_code"],
                    "verification_uri": flow.get(
                        "verification_uri", "https://microsoft.com/devicelogin"
                    ),
                }

        try:
            account = client.authenticate(device_code_callback=on_code)
            with state.lock:
                state.account = account
                state.auth = {"status": "done"}
        except Exception as exc:
            with state.lock:
                state.auth = {"status": "error", "error": str(exc)}

    @app.post("/auth/start")
    def auth_start():
        with state.lock:
            already = state.auth.get("status") in ("working", "pending", "done")
            if not already:
                state.auth = {"status": "working"}
        if not already:
            threading.Thread(target=auth_worker, daemon=True).start()
        return redirect(url_for("index"))

    # ---------- Scan ----------

    def scan_worker(since: datetime, until: datetime, query: str | None) -> None:
        db = Store(config)
        log: dict = {
            "period": f"{since:%d.%m.%Y} bis {(until - timedelta(days=1)):%d.%m.%Y}",
            "folders": None,
            "mails": [],
        }
        try:
            with state.lock:
                state.scan = {"status": "running", "progress": "Suche Mails im Postfach …"}
            messages = client.search_messages(since, until, query=query)
            log["folders"] = getattr(client, "last_folder_stats", None)
            found = auto = asked = 0
            for i, msg in enumerate(messages, 1):
                with state.lock:
                    state.scan["progress"] = f"Prüfe Mail {i} von {len(messages)} …"
                notes: list[str] = []
                results: list[str] = []
                for cand in collect_candidates(client, msg, notes=notes):
                    if (
                        db.already_recorded(
                            msg.id, cand.filename, msg.sender_email, msg.received,
                            cand.invoice_number, cand.amount,
                        )
                        or db.pending_exists(
                            msg.id, cand.filename, msg.sender_email, msg.received,
                            cand.invoice_number, cand.amount,
                        )
                    ):
                        results.append(f"„{cand.filename}“: bereits erfasst, übersprungen")
                        continue
                    if db.is_dismissed(msg.id, cand.filename, msg.sender_email, msg.received):
                        results.append(f"„{cand.filename}“: früher verworfen, übersprungen")
                        continue
                    rule = db.find_rule(msg.sender_email)
                    if rule and rule.kind == "ignorieren":
                        results.append(f"„{cand.filename}“: Absender wird laut Regel ignoriert")
                        continue
                    found += 1
                    if rule:
                        stored = None
                        if rule.kind == "geschaeftlich":
                            stored = db.store_file(
                                msg.received, msg.sender_email, cand.filename, cand.content
                            )
                        db.record_invoice(
                            message_id=msg.id,
                            filename=cand.filename,
                            sender_email=msg.sender_email,
                            sender_name=msg.sender_name,
                            subject=msg.subject,
                            received_at=msg.received,
                            invoice_number=cand.invoice_number,
                            invoice_date=cand.invoice_date,
                            amount=cand.amount,
                            currency=cand.currency,
                            kind=rule.kind,
                            category=rule.category,
                            stored_path=stored,
                        )
                        auto += 1
                        results.append(f"„{cand.filename}“: automatisch → {rule.kind}/{rule.category}")
                    else:
                        db.add_pending(
                            message_id=msg.id,
                            filename=cand.filename,
                            sender_email=msg.sender_email,
                            sender_name=msg.sender_name,
                            subject=msg.subject,
                            received_at=msg.received,
                            invoice_number=cand.invoice_number,
                            invoice_date=cand.invoice_date,
                            amount=cand.amount,
                            currency=cand.currency,
                            content=cand.content,
                        )
                        asked += 1
                        results.append(f"„{cand.filename}“: als offene Frage vorgemerkt")
                if (notes or results) and len(log["mails"]) < 800:
                    log["mails"].append({
                        "received": f"{msg.received:%d.%m.%Y}",
                        "sender": msg.sender_name or msg.sender_email,
                        "subject": msg.subject,
                        "results": results,
                        "notes": notes,
                    })
            with state.lock:
                state.scan = {
                    "status": "done",
                    "messages": len(messages),
                    "found": found,
                    "auto": auto,
                    "asked": asked,
                }
                state.scan_log = log
        except Exception as exc:
            with state.lock:
                state.scan = {"status": "error", "error": str(exc)}
                state.scan_log = log

    @app.post("/scan")
    def scan_start():
        if not state.account:
            return redirect(url_for("index"))
        with state.lock:
            if state.scan.get("status") == "running":
                return redirect(url_for("index"))
        since = datetime.strptime(request.form["since"], "%Y-%m-%d")
        until = datetime.strptime(request.form["until"], "%Y-%m-%d") + timedelta(days=1)
        query = request.form.get("query", "").strip() or None
        threading.Thread(target=scan_worker, args=(since, until, query), daemon=True).start()
        return redirect(url_for("index"))

    # ---------- Selbst-Aktualisierung ----------

    def update_worker() -> None:
        try:
            files, source = updater.self_update()
            with state.lock:
                state.update = {"status": "done", "files": len(files), "source": source}
            _schedule_restart()
        except Exception as exc:
            with state.lock:
                state.update = {"status": "error", "error": str(exc)}

    @app.post("/update")
    def update_start():
        with state.lock:
            running = state.update.get("status") in ("running", "done")
            if not running:
                state.update = {"status": "running"}
        if not running:
            threading.Thread(target=update_worker, daemon=True).start()
        return redirect(url_for("index"))

    @app.get("/api/status")
    def api_status():
        snap = state.snapshot()
        snap["pending_count"] = store().count_pending()
        return jsonify(snap)

    # ---------- Seiten ----------

    @app.get("/")
    def index():
        snap = state.snapshot()
        default_since = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
        default_until = datetime.now().strftime("%Y-%m-%d")
        return render_template(
            "index.html",
            page="home",
            auth=snap["auth"],
            scan=snap["scan"],
            update=snap["update"],
            default_since=default_since,
            default_until=default_until,
        )

    @app.get("/scanlog")
    def scanlog():
        with state.lock:
            log = state.scan_log
        return render_template("scanlog.html", page="home", log=log)

    @app.get("/pending")
    def pending():
        db = store()
        return render_template(
            "pending.html",
            page="pending",
            items=db.list_pending(),
            categories=db.known_categories(),
        )

    @app.post("/pending/<int:pid>/classify")
    def classify(pid: int):
        db = store()
        item = db.get_pending(pid)
        if not item:
            abort(404)
        kind = request.form["kind"]
        if kind not in ("geschaeftlich", "privat"):
            abort(400)
        category = (
            request.form.get("category_new", "").strip()
            or request.form.get("category", "").strip()
            or "Sonstiges"
        )
        if kind == "privat":
            category = "Privat"

        if request.form.get("save_rule"):
            scope = request.form.get("scope", "domain")
            if scope == "email":
                rule = Rule("email", item["sender_email"], kind, category)
            else:
                rule = Rule("domain", item["sender_email"].split("@")[-1], kind, category)
            db.save_rule(rule)

        received = datetime.fromisoformat(item["received_at"])
        stored = None
        if kind == "geschaeftlich":
            data = Path(item["file_path"]).read_bytes()
            stored = db.store_file(received, item["sender_email"], item["filename"], data)
        db.record_invoice(
            message_id=item["message_id"],
            filename=item["filename"],
            sender_email=item["sender_email"],
            sender_name=item["sender_name"] or "",
            subject=item["subject"] or "",
            received_at=received,
            invoice_number=item["invoice_number"],
            invoice_date=datetime.fromisoformat(item["invoice_date"])
            if item["invoice_date"]
            else None,
            amount=item["amount"],
            currency=item["currency"] or "EUR",
            kind=kind,
            category=category,
            stored_path=stored,
        )
        db.delete_pending(pid)
        return redirect(url_for("pending"))

    @app.post("/pending/<int:pid>/skip")
    def skip(pid: int):
        db = store()
        item = db.get_pending(pid)
        if item:
            # Merken, damit dieselbe Mail bei künftigen Scans nicht erneut auftaucht
            db.add_dismissed(
                message_id=item["message_id"],
                filename=item["filename"],
                sender_email=item["sender_email"],
                received_at=item["received_at"],
                subject=item["subject"],
            )
            if request.args.get("ignore"):
                domain = item["sender_email"].split("@")[-1]
                db.save_rule(Rule("domain", domain, "ignorieren", "–"))
            db.delete_pending(pid)
        return redirect(url_for("pending"))

    @app.get("/pending/<int:pid>/file")
    def pending_file(pid: int):
        item = store().get_pending(pid)
        if not item:
            abort(404)
        return send_file(absolute_path(item["file_path"]), download_name=item["filename"])

    @app.get("/invoices")
    def invoices():
        db = store()
        month = request.args.get("month") or None
        kind = request.args.get("kind") or None
        return render_template(
            "invoices.html",
            page="invoices",
            rows=db.list_invoices(month=month, kind=kind),
            months=db.months_with_invoices(),
            sel_month=month or "",
            sel_kind=kind or "",
        )

    @app.get("/invoices/<int:iid>/file")
    def invoice_file(iid: int):
        row = store().get_invoice(iid)
        if not row or not row["stored_path"]:
            abort(404)
        path = absolute_path(row["stored_path"])
        if not path.exists():
            abort(404)
        return send_file(path, download_name=row["filename"])

    @app.post("/invoices/<int:iid>/delete")
    def invoice_delete(iid: int):
        db = store()
        # Entfernen wird gemerkt (dismissed), damit der nächste Scan die Mail
        # nicht erneut als Rechnung vorschlägt; "reask" schickt sie stattdessen
        # bewusst zurück in die offenen Fragen.
        reask = request.form.get("mode") == "reask"
        row = db.get_invoice(iid)
        if row and reask and row["stored_path"] and absolute_path(row["stored_path"]).exists():
            db.add_pending(
                message_id=row["message_id"],
                filename=row["filename"],
                sender_email=row["sender_email"],
                sender_name=row["sender_name"] or "",
                subject=row["subject"] or "",
                received_at=datetime.fromisoformat(row["received_at"]),
                invoice_number=row["invoice_number"],
                invoice_date=datetime.fromisoformat(row["invoice_date"]) if row["invoice_date"] else None,
                amount=row["amount"],
                currency=row["currency"] or "EUR",
                content=absolute_path(row["stored_path"]).read_bytes(),
            )
            db.delete_invoice(iid, dismiss=False)
        else:
            db.delete_invoice(iid, dismiss=not reask)
        args = {k: v for k, v in (("month", request.form.get("month")), ("kind", request.form.get("kind"))) if v}
        return redirect(url_for("pending") if reask else url_for("invoices", **args))

    @app.get("/rules")
    def rules():
        db = store()
        return render_template(
            "rules.html", page="rules", rows=db.list_rules(),
            categories=db.known_categories(),
        )

    @app.post("/rules/<int:rid>/update")
    def rule_update(rid: int):
        db = store()
        row = db.get_rule(rid)
        if not row:
            abort(404)
        kind = request.form.get("kind", row["kind"])
        if kind not in ("geschaeftlich", "privat", "ignorieren"):
            abort(400)
        category = (
            request.form.get("category_new", "").strip()
            or request.form.get("category", "").strip()
            or row["category"]
        )
        if kind == "privat":
            category = "Privat"
        elif kind == "ignorieren":
            category = "–"
        db.update_rule(rid, kind, category)
        if request.form.get("apply_existing") and kind != "ignorieren":
            db.reclassify_matching_invoices(row["match_type"], row["pattern"], kind, category)
        return redirect(url_for("rules"))

    @app.post("/rules/<int:rid>/delete")
    def rule_delete(rid: int):
        store().delete_rule(rid)
        return redirect(url_for("rules"))

    PDF_NAME_RE = re.compile(r"rechnungen_\d{4}-\d{2}_(gesamt|uebersicht)(_[^/\\]+)?\.pdf")

    def _pdf_label(name: str) -> str:
        m = re.match(r"rechnungen_(\d{4}-\d{2})_(gesamt|uebersicht)(?:_(.+))?\.pdf", name)
        if not m:
            return name
        month, art, cat = m.groups()
        label = f"{'Gesamt-PDF' if art == 'gesamt' else 'Übersicht (PDF)'} {month}"
        label += f" – {cat.replace('_', ' ')}" if cat else " – alle Kategorien"
        return label

    @app.get("/reports")
    def reports():
        db = store()
        months = db.months_with_invoices()
        existing: list[str] = []
        pdfs: list[tuple[str, str]] = []
        if config.reports_dir.exists():
            existing = sorted(
                (p.stem.replace("rechnungen_", "") for p in config.reports_dir.glob("rechnungen_*.html")),
                reverse=True,
            )
            pdfs = sorted(
                ((p.name, _pdf_label(p.name)) for p in config.reports_dir.glob("rechnungen_*.pdf")
                 if PDF_NAME_RE.fullmatch(p.name)),
                reverse=True,
            )
        return render_template(
            "reports.html", page="reports", months=months, existing=existing,
            pdfs=pdfs, categories=db.categories_with_invoices(),
        )

    @app.post("/reports/bundle")
    def bundle_generate():
        year, month = (int(x) for x in request.form["month"].split("-"))
        category = request.form.get("category", "").strip() or None
        with_documents = request.form.get("content", "gesamt") == "gesamt"
        out = generate_monthly_bundle(
            config, store(), year, month, category=category, with_documents=with_documents
        )
        return redirect(url_for("bundle_file", name=out.name))

    @app.get("/reports/pdf/<name>")
    def bundle_file(name: str):
        if not PDF_NAME_RE.fullmatch(name):
            abort(404)
        path = config.reports_dir / name
        if not path.exists():
            abort(404)
        return send_file(path)

    @app.post("/reports/generate")
    def report_generate():
        year, month = (int(x) for x in request.form["month"].split("-"))
        generate_monthly_report(config, store(), year, month)
        return redirect(url_for("report_view", month=f"{year:04d}-{month:02d}"))

    @app.get("/reports/view/<month>")
    def report_view(month: str):
        path = config.reports_dir / f"rechnungen_{month}.html"
        if not path.exists():
            abort(404)
        return send_file(path)

    return app


def run(config: Config, port: int = 8321, open_browser: bool = True) -> int:
    url = f"http://127.0.0.1:{port}"
    print(f"Rechnungsassistent startet auf {url} (Beenden mit Strg+C)", flush=True)
    app = create_app(config)
    if open_browser and not os.environ.get("IA_RESTARTED"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0
