"""Selbst-Aktualisierung des Assistenten.

Lädt die neueste Version als ZIP von GitHub (oder nimmt eine bereits
manuell heruntergeladene ZIP aus dem Downloads-Ordner, falls der direkte
Download nicht möglich ist — z. B. bei privatem Repository) und ersetzt
die Programmdateien. Nie angetastet werden: data/ (Rechnungen, Regeln,
Berichte), config.json und die Python-Umgebung .venv.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

import requests

ZIP_URL = (
    "https://github.com/tommosblech/speech-backend/archive/refs/heads/"
    "claude/outlook-invoice-assistant-uqkx85.zip"
)
ZIP_GLOB = "speech-backend-claude-outlook-invoice-assistant-*.zip"
PRESERVE = {"data", "config.json", ".venv", "install.log"}


def install_dir() -> Path:
    override = os.environ.get("IA_INSTALL_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent  # der invoice-assistant-Ordner


def download_zip(timeout: tuple[int, int] = (8, 25)) -> bytes:
    """timeout = (Verbindungsaufbau, Herunterladen) in Sekunden — bewusst kurz,
    damit eine blockierte/langsame Verbindung (Firewall, Proxy) schnell auf
    den Downloads-Ordner-Rückfallweg umschaltet, statt den Nutzer bis zu
    zwei Minuten vor einer scheinbar hängenden Seite warten zu lassen."""
    resp = requests.get(ZIP_URL, timeout=timeout)
    resp.raise_for_status()
    if not resp.content.startswith(b"PK"):
        raise RuntimeError("Antwort ist kein ZIP-Archiv (privates Repository?)")
    return resp.content


def _version_tuple(v: str | None) -> tuple[int, int, int, int]:
    """Numerischer Vergleichsschlüssel für 'YYYY-MM-DD.N' — eine reine
    Textsortierung würde z. B. '.9' fälschlich für neuer als '.10' halten."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\.(\d+)", (v or "").strip())
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def _zip_version(data: bytes) -> str | None:
    """Liest die VERSION-Datei direkt aus den ZIP-Bytes, ohne zu entpacken."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith("/invoice-assistant/VERSION"):
                    return zf.read(name).decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    return None


def find_downloads_zip() -> Path | None:
    """Wählt unter allen passenden ZIPs im Downloads-Ordner die Datei mit der
    HÖCHSTEN Versionsnummer — nicht einfach die zuletzt geänderte. Sonst
    könnte eine alte, schon Monate zurückliegende ZIP (deren Dateisystem-
    Zeitstempel z. B. durch einen Virenscan oder ein erneutes Speichern
    aktuell wirkt) fälschlich für die neueste gehalten werden und die
    Installation auf eine sehr alte Version zurückstufen."""
    downloads = Path(os.environ.get("IA_DOWNLOADS_DIR", str(Path.home() / "Downloads")))
    if not downloads.exists():
        return None
    candidates = list(downloads.glob(ZIP_GLOB))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def key(p: Path) -> tuple:
        try:
            return _version_tuple(_zip_version(p.read_bytes()))
        except OSError:
            return (0, 0, 0, 0)

    return max(candidates, key=key)


def apply_zip(data: bytes) -> list[str]:
    """Ersetzt die Programmdateien durch die aus dem ZIP; data/ & Co. bleiben."""
    dest = install_dir().resolve()
    updated: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        prefix = None
        for name in names:
            parts = name.split("/")
            if len(parts) >= 2 and parts[1] == "invoice-assistant":
                prefix = f"{parts[0]}/invoice-assistant/"
                break
        if not prefix:
            raise RuntimeError("Das ZIP enthält keinen Ordner 'invoice-assistant'.")
        for name in names:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            if not rel or rel.split("/")[0] in PRESERVE:
                continue
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest) + os.sep):
                continue  # Sicherheitsnetz gegen Pfad-Ausbrüche im ZIP
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
            updated.append(rel)
    if not updated:
        raise RuntimeError("Im ZIP wurden keine Programmdateien gefunden.")
    return updated


def self_update(on_progress=None) -> tuple[list[str], str]:
    """Erst Direkt-Download versuchen, sonst ZIP aus dem Downloads-Ordner nehmen.

    on_progress(text) wird bei jedem Phasenwechsel aufgerufen, damit die
    Oberfläche anzeigen kann, was gerade passiert, statt eine ganze Weile
    eine unveränderte Meldung zu zeigen (wirkt sonst wie ein Absturz).
    """
    def progress(text: str) -> None:
        if on_progress:
            on_progress(text)

    progress("Verbinde mit GitHub …")
    try:
        data = download_zip()
        source = "GitHub (direkter Download)"
    except Exception as download_error:
        progress("Direkter Download nicht möglich — prüfe Downloads-Ordner …")
        local = find_downloads_zip()
        if not local:
            raise RuntimeError(
                f"Direkter Download fehlgeschlagen ({download_error}) und keine "
                f"passende ZIP-Datei im Downloads-Ordner gefunden. Bitte die ZIP "
                f"von GitHub in den Downloads-Ordner laden und erneut aktualisieren."
            )
        data = local.read_bytes()
        source = f"Downloads-Ordner ({local.name})"

    # Sicherheitsnetz gegen genau den Fall, der hier einmal auftrat: eine
    # veraltete ZIP (egal aus welcher Quelle) darf niemals eine neuere,
    # bereits installierte Version zurückstufen.
    installed = current_version()
    found = _zip_version(data)
    if found and _version_tuple(found) <= _version_tuple(installed):
        raise RuntimeError(
            f"Die gefundene Version ({found}) ist nicht neuer als die installierte "
            f"({installed}) — vermutlich liegt eine veraltete ZIP im Downloads-Ordner. "
            f"Bitte alte 'speech-backend-...zip'-Dateien dort löschen, eine frische "
            f"Version von GitHub laden und erneut aktualisieren."
        )

    progress("Installiere Dateien …")
    return apply_zip(data), source


def current_version() -> str:
    try:
        return (install_dir() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unbekannt"
