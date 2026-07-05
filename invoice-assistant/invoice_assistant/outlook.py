"""Zugriff auf das Postfach über die Microsoft-Graph-API.

Anmeldung per Device-Code-Flow (msal): Das Tool zeigt einen Code an, den man
unter https://microsoft.com/devicelogin eingibt. Es werden nur Leserechte
(Mail.Read) angefordert; der Token wird lokal im data-Ordner zwischengespeichert.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime

import msal
import requests

from .config import Config

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]


@dataclass
class Attachment:
    id: str
    name: str
    content_type: str
    size: int


@dataclass
class Message:
    id: str
    subject: str
    sender_name: str
    sender_email: str
    received: datetime
    body_preview: str
    has_attachments: bool
    attachments: list[Attachment] = field(default_factory=list)


class OutlookClient:
    def __init__(self, config: Config):
        self.config = config
        self._token: str | None = None

    # ---------- Anmeldung ----------

    def authenticate(self) -> str:
        """Meldet den Nutzer an und liefert dessen Kontonamen zurück."""
        cache = msal.SerializableTokenCache()
        if self.config.token_cache_path.exists():
            cache.deserialize(self.config.token_cache_path.read_text(encoding="utf-8"))

        app = msal.PublicClientApplication(
            self.config.client_id,
            authority=f"https://login.microsoftonline.com/{self.config.tenant}",
            token_cache=cache,
        )

        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])

        if not result:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Device-Flow fehlgeschlagen: {flow}")
            print(f"\n>>> {flow['message']}\n")
            result = app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(
                f"Anmeldung fehlgeschlagen: {result.get('error_description', result)}"
            )

        if cache.has_state_changed:
            self.config.token_cache_path.write_text(cache.serialize(), encoding="utf-8")
            self.config.token_cache_path.chmod(0o600)

        self._token = result["access_token"]
        me = self._get(f"{GRAPH}/me")
        return me.get("userPrincipalName") or me.get("displayName", "unbekannt")

    def _get(self, url: str, **params) -> dict:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            params=params or None,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ---------- Mails ----------

    def search_messages(
        self, since: datetime, until: datetime, query: str | None = None
    ) -> list[Message]:
        """Listet Mails im Zeitraum auf; optional zusätzlich per Volltextsuche."""
        messages: list[Message] = []
        select = "id,subject,sender,receivedDateTime,bodyPreview,hasAttachments"
        if query:
            url = f"{GRAPH}/me/messages"
            params = {"$search": f'"{query}"', "$select": select, "$top": "50"}
        else:
            flt = (
                f"receivedDateTime ge {since.strftime('%Y-%m-%dT00:00:00Z')} "
                f"and receivedDateTime lt {until.strftime('%Y-%m-%dT00:00:00Z')}"
            )
            url = f"{GRAPH}/me/messages"
            params = {
                "$filter": flt,
                "$select": select,
                "$top": "50",
                "$orderby": "receivedDateTime desc",
            }

        while url:
            data = self._get(url, **params)
            params = {}  # nextLink enthält die Parameter bereits
            for item in data.get("value", []):
                received = datetime.fromisoformat(
                    item["receivedDateTime"].replace("Z", "+00:00")
                )
                if query and not (since <= received.replace(tzinfo=None) < until):
                    continue
                sender = (item.get("sender") or {}).get("emailAddress", {})
                messages.append(
                    Message(
                        id=item["id"],
                        subject=item.get("subject") or "(kein Betreff)",
                        sender_name=sender.get("name", ""),
                        sender_email=(sender.get("address") or "").lower(),
                        received=received,
                        body_preview=item.get("bodyPreview", ""),
                        has_attachments=item.get("hasAttachments", False),
                    )
                )
            url = data.get("@odata.nextLink")
        return messages

    def list_attachments(self, message: Message) -> list[Attachment]:
        data = self._get(
            f"{GRAPH}/me/messages/{message.id}/attachments",
            **{"$select": "id,name,contentType,size"},
        )
        message.attachments = [
            Attachment(
                id=a["id"],
                name=a.get("name", "anhang"),
                content_type=a.get("contentType", ""),
                size=a.get("size", 0),
            )
            for a in data.get("value", [])
            if a.get("@odata.type", "").endswith("fileAttachment")
        ]
        return message.attachments

    def download_attachment(self, message: Message, attachment: Attachment) -> bytes:
        data = self._get(f"{GRAPH}/me/messages/{message.id}/attachments/{attachment.id}")
        return base64.b64decode(data["contentBytes"])

    def get_body_text(self, message: Message) -> str:
        data = self._get(
            f"{GRAPH}/me/messages/{message.id}",
            **{"$select": "body"},
        )
        body = data.get("body", {})
        text = body.get("content", "")
        if body.get("contentType") == "html":
            import re

            text = re.sub(r"<[^>]+>", " ", text)
        return text
