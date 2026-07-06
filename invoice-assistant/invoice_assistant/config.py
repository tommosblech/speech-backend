"""Konfiguration des Assistenten.

Alle Pfade liegen standardmäßig unter ./data, damit nichts außerhalb des
Projektordners geschrieben wird. Die Azure-App-Registrierung (CLIENT_ID)
kommt aus der Umgebung oder aus config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("INVOICE_ASSISTANT_DATA", "data"))

# Vorschlagsliste für Kostenkategorien; frei erweiterbar, neue Kategorien
# entstehen automatisch, sobald der Nutzer sie bei einer Rückfrage eintippt.
DEFAULT_CATEGORIES = [
    "Elektroladen",
    "KI-Tools",
    "Software/Abos",
    "Hosting/Cloud",
    "Büromaterial",
    "Telekommunikation",
    "Reisekosten",
    "Fortbildung",
    "Sonstiges",
]


@dataclass
class Config:
    client_id: str = ""
    tenant: str = "consumers"  # "consumers" für private MS-Konten, sonst Tenant-ID
    mail_source: str = "auto"  # "local" (Outlook auf diesem Rechner), "graph" (Cloud) oder "auto"
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))

    @property
    def source(self) -> str:
        """Effektive Mail-Quelle: bei "auto" entscheidet die vorhandene client_id."""
        if self.mail_source in ("local", "graph"):
            return self.mail_source
        return "graph" if self.client_id else "local"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "invoices.sqlite3"

    @property
    def invoices_dir(self) -> Path:
        return self.data_dir / "invoices"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def pending_dir(self) -> Path:
        return self.data_dir / "pending"

    @property
    def token_cache_path(self) -> Path:
        return self.data_dir / "token_cache.json"


def load_config(path: str | Path = "config.json") -> Config:
    """Lädt config.json (falls vorhanden) und lässt Umgebungsvariablen vorgehen."""
    cfg = Config()
    p = Path(path)
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        cfg.client_id = raw.get("client_id", cfg.client_id)
        cfg.tenant = raw.get("tenant", cfg.tenant)
        cfg.mail_source = raw.get("mail_source", cfg.mail_source)
        if "data_dir" in raw:
            cfg.data_dir = Path(raw["data_dir"])
        if "categories" in raw:
            cfg.categories = list(raw["categories"])
    cfg.client_id = os.environ.get("OUTLOOK_CLIENT_ID", cfg.client_id)
    cfg.tenant = os.environ.get("OUTLOOK_TENANT", cfg.tenant)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
