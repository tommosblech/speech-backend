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


def download_zip(timeout: int = 120) -> bytes:
    resp = requests.get(ZIP_URL, timeout=timeout)
    resp.raise_for_status()
    if not resp.content.startswith(b"PK"):
        raise RuntimeError("Antwort ist kein ZIP-Archiv (privates Repository?)")
    return resp.content


def find_downloads_zip() -> Path | None:
    downloads = Path(os.environ.get("IA_DOWNLOADS_DIR", str(Path.home() / "Downloads")))
    if not downloads.exists():
        return None
    candidates = sorted(
        downloads.glob(ZIP_GLOB), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


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


def self_update() -> tuple[list[str], str]:
    """Erst Direkt-Download versuchen, sonst ZIP aus dem Downloads-Ordner nehmen."""
    try:
        data = download_zip()
        source = "GitHub (direkter Download)"
    except Exception as download_error:
        local = find_downloads_zip()
        if not local:
            raise RuntimeError(
                f"Direkter Download fehlgeschlagen ({download_error}) und keine "
                f"passende ZIP-Datei im Downloads-Ordner gefunden. Bitte die ZIP "
                f"von GitHub in den Downloads-Ordner laden und erneut aktualisieren."
            )
        data = local.read_bytes()
        source = f"Downloads-Ordner ({local.name})"
    return apply_zip(data), source


def current_version() -> str:
    try:
        return (install_dir() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unbekannt"
