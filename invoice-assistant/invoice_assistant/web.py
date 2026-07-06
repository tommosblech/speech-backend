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

from .config import Config
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

    def snapshot(self) -> dict:
        with self.lock:
            return {"account": self.account, "auth": dict(self.auth), "scan": dict(self.scan)}


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    state = AppState()
    client = make_client(config)

    def store() -> Store:
        return Store(config)

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
        try:
            with state.lock:
                state.scan = {"status": "running", "progress": "Suche Mails im Postfach …"}
            messages = client.search_messages(since, until, query=query)
            found = auto = asked = 0
            for i, msg in enumerate(messages, 1):
                with state.lock:
                    state.scan["progress"] = f"Prüfe Mail {i} von {len(messages)} …"
                for cand in collect_candidates(client, msg):
                    if db.already_recorded(msg.id, cand.filename) or db.pending_exists(
                        msg.id, cand.filename
                    ):
                        continue
                    found += 1
                    rule = db.find_rule(msg.sender_email)
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
            with state.lock:
                state.scan = {
                    "status": "done",
                    "messages": len(messages),
                    "found": found,
                    "auto": auto,
                    "asked": asked,
                }
        except Exception as exc:
            with state.lock:
                state.scan = {"status": "error", "error": str(exc)}

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
            default_since=default_since,
            default_until=default_until,
        )

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
        store().delete_pending(pid)
        return redirect(url_for("pending"))

    @app.get("/pending/<int:pid>/file")
    def pending_file(pid: int):
        item = store().get_pending(pid)
        if not item:
            abort(404)
        return send_file(item["file_path"], download_name=item["filename"])

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

    @app.get("/rules")
    def rules():
        return render_template("rules.html", page="rules", rows=store().list_rules())

    @app.post("/rules/<int:rid>/delete")
    def rule_delete(rid: int):
        store().delete_rule(rid)
        return redirect(url_for("rules"))

    @app.get("/reports")
    def reports():
        months = store().months_with_invoices()
        existing = sorted(
            (p.stem.replace("rechnungen_", "") for p in config.reports_dir.glob("rechnungen_*.html")),
            reverse=True,
        ) if config.reports_dir.exists() else []
        return render_template(
            "reports.html", page="reports", months=months, existing=existing
        )

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
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
    return 0
