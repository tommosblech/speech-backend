"""Zugriff auf das lokal installierte Outlook (Windows) über die COM-Schnittstelle.

Liest die Mails direkt aus deinem Outlook — egal, bei welchem Anbieter das
Postfach liegt. Kein Azure, keine App-Registrierung, keine Zugangsdaten:
Der Assistent benutzt einfach das, was Outlook bereits synchronisiert hat.

Voraussetzungen: Windows mit klassischem Outlook (Desktop). „Das neue
Outlook" hat keine COM-Schnittstelle — dort oben rechts den Schalter
„Neues Outlook" ausschalten oder die Cloud-Variante (Graph) nutzen.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
import threading
from datetime import datetime

from .config import Config
from .outlook import Attachment, Message

OL_FOLDER_INBOX = 6
OL_MAIL_ITEM = 43


def _naive(dt) -> datetime:
    """pywin32-Zeitstempel in ein normales datetime umwandeln."""
    return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _sender_address(item) -> str:
    """SMTP-Adresse ermitteln; Exchange liefert sonst interne X.500-Adressen."""
    try:
        if getattr(item, "SenderEmailType", "") == "EX":
            exch = item.Sender.GetExchangeUser()
            if exch and exch.PrimarySmtpAddress:
                return str(exch.PrimarySmtpAddress).lower()
        return str(item.SenderEmailAddress or "").lower()
    except Exception:
        return ""


class LocalOutlookClient:
    """Gleiche Schnittstelle wie OutlookClient (Graph), Quelle ist das lokale Outlook."""

    def __init__(self, config: Config):
        self.config = config
        self._tl = threading.local()  # COM-Objekte sind an ihren Thread gebunden

    def _namespace(self):
        ns = getattr(self._tl, "ns", None)
        if ns is None:
            try:
                import pythoncom
                import win32com.client
            except ImportError as exc:
                raise RuntimeError(
                    "Der lokale Outlook-Zugriff funktioniert nur unter Windows mit "
                    "installiertem pywin32 und klassischem Outlook. Alternative: "
                    "Cloud-Zugriff über Azure einrichten (siehe README)."
                ) from exc
            pythoncom.CoInitialize()
            app = win32com.client.Dispatch("Outlook.Application")
            ns = app.GetNamespace("MAPI")
            self._tl.ns = ns
        return ns

    def try_silent(self) -> str | None:
        try:
            return self.authenticate()
        except Exception:
            return None

    def authenticate(self, device_code_callback=None) -> str:
        ns = self._namespace()
        try:
            first = ns.Accounts.Item(1)
            return str(first.SmtpAddress or first.DisplayName)
        except Exception:
            return f"Lokales Outlook ({ns.CurrentUser.Name})"

    def search_messages(
        self, since: datetime, until: datetime, query: str | None = None
    ) -> list[Message]:
        ns = self._namespace()
        items = ns.GetDefaultFolder(OL_FOLDER_INBOX).Items
        items.Sort("[ReceivedTime]", True)  # neueste zuerst
        result: list[Message] = []
        for item in items:
            if getattr(item, "Class", 0) != OL_MAIL_ITEM:
                continue  # Termine, Zustellberichte usw. überspringen
            received = _naive(item.ReceivedTime)
            if received >= until:
                continue
            if received < since:
                break  # absteigend sortiert: alles Weitere ist älter
            subject = str(item.Subject or "")
            if query and query.lower() not in subject.lower():
                continue
            atts = getattr(item, "Attachments", None)
            result.append(
                Message(
                    id=str(item.EntryID),
                    subject=subject or "(kein Betreff)",
                    sender_name=str(getattr(item, "SenderName", "") or ""),
                    sender_email=_sender_address(item),
                    received=received,
                    body_preview="",
                    has_attachments=bool(atts and atts.Count > 0),
                )
            )
        return result

    def _item(self, message_id: str):
        return self._namespace().GetItemFromID(message_id)

    def list_attachments(self, message: Message) -> list[Attachment]:
        item = self._item(message.id)
        result: list[Attachment] = []
        for i in range(1, item.Attachments.Count + 1):
            att = item.Attachments.Item(i)
            name = str(att.FileName or f"anhang_{i}")
            result.append(
                Attachment(
                    id=str(i),
                    name=name,
                    content_type=mimetypes.guess_type(name)[0] or "",
                    size=int(att.Size),
                )
            )
        message.attachments = result
        return result

    def download_attachment(self, message: Message, attachment: Attachment) -> bytes:
        item = self._item(message.id)
        att = item.Attachments.Item(int(attachment.id))
        fd, tmp = tempfile.mkstemp(suffix="_" + os.path.basename(attachment.name))
        os.close(fd)
        try:
            att.SaveAsFile(tmp)
            with open(tmp, "rb") as fh:
                return fh.read()
        finally:
            os.unlink(tmp)

    def get_body_text(self, message: Message) -> str:
        return str(self._item(message.id).Body or "")
