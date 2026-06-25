"""
gmail/client.py — Gmail API wrapper for reading alerts, sending digests, labeling.
"""

import os
import base64
import time
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

import structlog
from google.auth.transport.requests import Request
from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _sanitize_query(value: str) -> str:
    """Strip characters that could alter Gmail search query semantics."""
    return value.replace('"', '').replace('\\', '').replace('\n', '').replace('\r', '')


class GmailClient:
    """Authenticated Gmail client with helpers for the hiring agent pipeline."""

    def __init__(self):
        self.creds = self._authenticate()
        self.service = build("gmail", "v1", credentials=self.creds)

    def _authenticate(self) -> Credentials:
        """OAuth2 flow — opens browser on first run, then reuses token."""
        token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "config/credentials/token.json"))
        creds_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "config/credentials/credentials.json"))

        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
                creds = flow.run_local_server(port=0)

            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        return creds

    # ── Read ────────────────────────────────────────────────────

    def find_unprocessed_alert(
        self,
        sender: str,
        subject_contains: str,
        processed_label: str,
    ) -> dict | None:
        """
        Find the newest Hiring.cafe alert that hasn't been labeled as processed.
        Matches by subject only so forwarded copies (From: user's own email,
        Subject: "Fwd: ... HiringCafe") are picked up alongside direct alerts.
        Returns {"id": str, "html": str, "text": str} or None.
        """
        # subject-only match — catches both direct (ali@hiring.cafe) and forwarded
        query = f'subject:"{_sanitize_query(subject_contains)}" -label:{_sanitize_query(processed_label)}'

        results = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=1)
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            return None

        msg_id = messages[0]["id"]
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )

        return {
            "id": msg_id,
            "html": self._extract_body(msg, "text/html"),
            "text": self._extract_body(msg, "text/plain"),
        }

    def _extract_body(self, message: dict, mime_type: str) -> str:
        """Extract body content of a given MIME type from a Gmail message."""
        payload = message.get("payload", {})

        # Simple single-part message
        if payload.get("mimeType") == mime_type:
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Multipart — recurse through parts
        for part in payload.get("parts", []):
            if part.get("mimeType") == mime_type:
                data = part.get("body", {}).get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

            # Nested multipart
            for sub in part.get("parts", []):
                if sub.get("mimeType") == mime_type:
                    data = sub.get("body", {}).get("data", "")
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        return ""

    # ── Label / Mark ────────────────────────────────────────────

    _TRANSIENT_ERRORS = (
        ConnectionResetError,
        BrokenPipeError,
        ConnectionAbortedError,
        HttpError,
        TransportError,
        OSError,
    )

    def refresh_connection(self):
        """Force-refresh credentials and rebuild the service object."""
        log.info("gmail.refresh_connection")
        if self.creds and self.creds.refresh_token:
            self.creds.refresh(Request())
        self.service = build("gmail", "v1", credentials=self.creds)

    def _retry_call(self, fn, *, attempts: int = 3, backoff: float = 2.0):
        """Run fn() with retries, refreshing the connection between attempts."""
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except self._TRANSIENT_ERRORS as exc:
                last_exc = exc
                log.warning("gmail.retry", attempt=attempt, error=str(exc))
                if attempt < attempts:
                    time.sleep(backoff)
                    self.refresh_connection()
        raise last_exc

    def mark_processed(self, message_id: str, label_name: str):
        """Apply a label to mark a message as processed. Creates label if needed."""
        self.refresh_connection()
        label_id = self._get_or_create_label(label_name)
        self._retry_call(
            lambda: self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        )

    def _get_or_create_label(self, label_name: str) -> str:
        """Get label ID by name, creating it if it doesn't exist."""
        results = self.service.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        # Create it
        body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self.service.users().labels().create(userId="me", body=body).execute()
        return created["id"]

    def get_unread_alerts(
        self,
        sender: str,
        subject_contains: str,
        processed_label: str,
        max_results: int = 10,
    ) -> list[dict]:
        """
        Return all unprocessed alert messages, newest first.
        Matches by subject only so forwarded copies are included alongside
        direct alerts.
        Each dict: {"id": str, "html": str, "text": str}.
        """
        query = f'subject:"{_sanitize_query(subject_contains)}" -label:{_sanitize_query(processed_label)}'
        results = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        out = []
        for m in results.get("messages", []):
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=m["id"], format="full")
                .execute()
            )
            out.append({
                "id": m["id"],
                "html": self._extract_body(msg, "text/html"),
                "text": self._extract_body(msg, "text/plain"),
            })
        return out

    # ── Send ────────────────────────────────────────────────────

    # MIME type dispatch for outbound attachments. Keyed on the lowercase
    # filename suffix. Anything not in the map gets a safe octet-stream
    # fallback so unknown formats don't break the send.
    _MIME_MAP = {
        ".pdf": ("application", "pdf"),
        ".docx": (
            "application",
            "vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }

    def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        attachments: list[Path] | None = None,
    ):
        """Send an email with optional PDF/DOCX attachments."""
        msg = MIMEMultipart()
        msg["to"] = to
        msg["subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        for filepath in (attachments or []):
            filepath = Path(filepath)
            suffix = filepath.suffix.lower()
            maintype, subtype = self._MIME_MAP.get(
                suffix, ("application", "octet-stream")
            )
            with open(filepath, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={filepath.name}",
                )
                msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

    def send_digest(
        self,
        to: str,
        subject: str,
        body_text: str,
        attachments: list[Path] | None = None,
    ):
        """Send a digest email with optional PDF/DOCX attachments (retries on transient errors)."""
        self.refresh_connection()
        self._retry_call(
            lambda: self.send_email(
                to=to, subject=subject, body_text=body_text, attachments=attachments
            )
        )


# ── Standalone: run this file to complete OAuth setup ───────
if __name__ == "__main__":
    client = GmailClient()
    print("✓ Gmail authentication successful.")
    print("  Token saved. You won't need to re-authenticate unless it expires.")
