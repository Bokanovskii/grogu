"""Optional Gmail REST access with local draft-first safety."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional


class GmailError(RuntimeError):
    """Base error for Gmail integration failures."""


class GmailDisabledError(GmailError):
    """Raised when the opt-in integration is disabled."""


class ConfirmationRequiredError(GmailError):
    """Raised when an outbound Gmail operation lacks confirmation."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class GmailDraft:
    id: str
    to: str
    subject: str
    body: str
    created_at: str
    status: str = "draft"


class DraftStore:
    def __init__(self, home: Optional[Path] = None) -> None:
        root = Path(home or Path.home() / ".grogu").expanduser()
        self.path = root / "gmail" / "drafts.jsonl"

    def _read(self) -> List[dict]:
        try:
            lines = self.path.read_text(encoding="utf8").splitlines()
        except OSError:
            return []
        values = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
        return values

    def _write(self, values: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            "".join(json.dumps(value, sort_keys=True) + "\n" for value in values),
            encoding="utf8",
        )
        os.replace(temporary, self.path)

    def create(self, to: str, subject: str, body: str) -> GmailDraft:
        if not to.strip():
            raise ValueError("Gmail recipient is required")
        if not subject.strip():
            raise ValueError("Gmail subject is required")
        if not body.strip():
            raise ValueError("Gmail body is required")
        draft = GmailDraft(str(uuid.uuid4()), to, subject, body, now())
        self._write(self._read() + [asdict(draft)])
        return draft

    def get(self, draft_id: str) -> Optional[GmailDraft]:
        for value in self._read():
            if value.get("id") == draft_id:
                return GmailDraft(
                    value["id"], value["to"], value["subject"], value["body"],
                    value["created_at"], value.get("status", "draft"),
                )
        return None

    def mark_sent(self, draft_id: str) -> GmailDraft:
        values = self._read()
        for value in values:
            if value.get("id") == draft_id:
                value["status"] = "sent"
                self._write(values)
                result = self.get(draft_id)
                if result is not None:
                    return result
        raise ValueError(f"no Gmail draft with id {draft_id!r}")


class GmailAdapter:
    """Small REST client; credentials are read from the environment only."""

    api_root = "https://gmail.googleapis.com/gmail/v1/users/me"
    read_scope = "https://www.googleapis.com/auth/gmail.readonly"
    send_scope = "https://www.googleapis.com/auth/gmail.compose"

    def __init__(
        self,
        access_token: Optional[str] = None,
        enabled: Optional[bool] = None,
        request: Optional[Callable[..., object]] = None,
    ) -> None:
        self.access_token = access_token or os.environ.get("GROGU_GMAIL_ACCESS_TOKEN", "")
        self.enabled = (
            enabled
            if enabled is not None
            else os.environ.get("GROGU_GMAIL_ENABLED", "0") == "1"
        )
        self.request = request or urllib.request.urlopen

    def status(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "authenticated": bool(self.access_token),
            "scopes": [self.read_scope, self.send_scope],
            "reason": (
                ""
                if self.enabled and self.access_token
                else "Gmail is disabled by default; configure a Google OAuth "
                "desktop client, authorize the listed scopes, set "
                "GROGU_GMAIL_ENABLED=1, and provide "
                "GROGU_GMAIL_ACCESS_TOKEN"
            ),
            "setup": {
                "oauth_playground": "https://developers.google.com/oauthplayground",
                "enabled_variable": "GROGU_GMAIL_ENABLED=1",
                "token_variable": "GROGU_GMAIL_ACCESS_TOKEN",
                "scopes": [self.read_scope, self.send_scope],
            },
        }

    def search(self, query: str, limit: int = 20) -> List[dict]:
        self._require_enabled()
        if not query.strip():
            raise ValueError("Gmail search query is required")
        listing = self._get(
            "/messages",
            {"q": query, "maxResults": max(0, limit)},
        )
        results = []
        for message in listing.get("messages", [])[: max(0, limit)]:
            detail = self._get(
                f"/messages/{message['id']}",
                {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            headers = {
                item["name"].lower(): item["value"]
                for item in detail.get("payload", {}).get("headers", [])
            }
            results.append(
                {
                    "id": detail.get("id", message["id"]),
                    "thread_id": detail.get("threadId", ""),
                    "snippet": detail.get("snippet", ""),
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                }
            )
        return results

    def send(self, draft: GmailDraft, confirmed: bool = False) -> dict:
        self._require_enabled()
        if not confirmed:
            raise ConfirmationRequiredError(
                "sending Gmail requires explicit confirmation"
            )
        raw = f"To: {draft.to}\r\nSubject: {draft.subject}\r\n\r\n{draft.body}"
        encoded = base64.urlsafe_b64encode(raw.encode("utf8")).decode("ascii").rstrip("=")
        self._post("/messages/send", {"raw": encoded})
        return {"sent": True, "recipient": draft.to}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise GmailDisabledError(
                "Gmail integration is disabled; set GROGU_GMAIL_ENABLED=1 explicitly"
            )
        if not self.access_token:
            raise GmailError("Gmail access token is unavailable")

    def _get(self, path: str, params: dict) -> dict:
        request = urllib.request.Request(
            self.api_root + path + "?" + urllib.parse.urlencode(params, doseq=True),
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        return self._decode(self.request(request))

    def _post(self, path: str, value: dict) -> dict:
        request = urllib.request.Request(
            self.api_root + path,
            data=json.dumps(value).encode("utf8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._decode(self.request(request))

    @staticmethod
    def _decode(response: object) -> dict:
        value = json.loads(response.read().decode("utf8"))
        if not isinstance(value, dict):
            raise GmailError("Gmail returned an invalid response")
        return value
