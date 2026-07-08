"""
gmail/client.py — Gmail API wrapper for reading alerts, sending digests, labeling.

Retry policy (S11 retrofit): network-touching methods are decorated with the
shared navigation-retry decorator (see ``apply/retries.py``). This replaced
the hand-rolled per-call retry helper (impl-plan Blocker #6 / S11 retrofit).
The decorator retries transient httpx / ConnectionError / Playwright-timeout
exceptions three times with jittered backoff and reraises the ORIGINAL
exception on final failure — callers see the underlying error, not
``tenacity.RetryError``.

S12 additions (Gmail review loop):
    * ``get_or_create_label(name)`` — idempotent, public form of the private
      helper the original client used for ``mark_processed``.
    * ``list_labels()`` — raw label roster.
    * ``search(query, max_results=100)`` — thread-scoped search returning
      one dict per matching message with body_text + thread_id + id.
    * ``apply_label(msg_id, label_id)`` / ``remove_label(msg_id, label_id)``
      — label-move primitives for the pending → submitted/declined transitions.
    * ``reply_to_thread(thread_id, body)`` — RFC-threaded reply used for
      the ambiguous-clarification and 24h re-ping paths.
    * ``send_with_labels(subject, body, to, labels, attachments)`` —
      review-email primary; returns ``(msg_id, thread_id)``.

S13 additions (fast-path emailer):
    * ``send_immediate(subject, body, attachments)`` — bypass-digest single-
      shot delivery used by ``apply.notify`` (CAPTCHA escalation,
      session-expired alerts). Never blocks the pipeline.

All added methods use S11's ``@navigation_retry`` bare-form; the class
methods that need a mid-retry credential refresh use the factory form
``@navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)``.
"""

import os
import base64
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, Callable

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from apply.retries import navigation_retry

log = structlog.get_logger()


def _refresh_gmail_client_before_retry(retry_state) -> None:
    """before_sleep hook that refreshes the GmailClient's creds + service.

    Runs between attempts of a bound-method call decorated with
    ``@navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)``.
    ``retry_state.args[0]`` is ``self`` for a method call. Restores the
    pre-retrofit hand-rolled retry helper's behavior of rebuilding the
    OAuth token and service handle between attempts so an expired token
    or stale httplib2 socket can recover mid-retry (finding #2).

    Silent on exceptions so a refresh failure doesn't mask the original
    transient error — tenacity will still retry the underlying call.
    """
    args = getattr(retry_state, "args", None) or ()
    if not args:
        return
    client = args[0]
    if not isinstance(client, GmailClient):
        return
    try:
        client.refresh_connection()
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("gmail.refresh_before_retry_failed", error=str(exc))

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

    @navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)
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

    def refresh_connection(self):
        """Force-refresh credentials and rebuild the service object."""
        log.info("gmail.refresh_connection")
        if self.creds and self.creds.refresh_token:
            self.creds.refresh(Request())
        self.service = build("gmail", "v1", credentials=self.creds)

    @navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)
    def mark_processed(self, message_id: str, label_name: str):
        """Apply a label to mark a message as processed. Creates label if needed.

        The pre-call ``refresh_connection()`` from the pre-retrofit design
        is intentionally removed — the ``before_sleep_extra`` hook now
        refreshes between attempts on transient failure, so preemptively
        refreshing on the happy path just adds a token-endpoint RTT for no
        benefit (finding #9).
        """
        label_id = self._get_or_create_label(label_name)
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

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

    @navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)
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
    # Preserved from origin/main: dual-output renderer emits both PDF + DOCX,
    # both must survive the send with correct maintype/subtype.
    _MIME_MAP = {
        ".pdf": ("application", "pdf"),
        ".docx": (
            "application",
            "vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }

    @navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)
    def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        attachments: list[Path] | None = None,
    ):
        """Send an email with optional PDF/DOCX attachments (origin MIME_MAP + quoted filename)."""
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
                # Quote the filename so spaces and special characters survive
                # MIME parsing. Without quotes, "Acme Corp_Resume.docx" would
                # be truncated at the first space by RFC 2183 parsers and
                # arrive as "Acme" on the recipient side.
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{filepath.name}"',
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
        """Send a digest email with optional PDF attachments.

        Thin delegate to ``send_email``. Retries + mid-retry
        ``refresh_connection`` live inside the decorated ``send_email`` via
        ``@navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)``.

        The old pre-call ``self.refresh_connection()`` here was removed
        (finding #8): it ran outside the retry surface, so any
        ``TransportError`` from ``creds.refresh(Request())`` propagated on
        first raise. Now the refresh happens BETWEEN attempts of
        ``send_email`` if and only if a retryable error occurred, which is
        both faster on the happy path and more resilient on the failure
        path.
        """
        self.send_email(
            to=to, subject=subject, body_text=body_text, attachments=attachments
        )

    # ── S12 extensions: label CRUD + search + threaded reply + send-with-labels ──

    @navigation_retry
    def list_labels(self) -> list[dict]:
        """Return the raw label roster ``[{"id": ..., "name": ...}, ...]``."""
        results = self.service.users().labels().list(userId="me").execute()
        return results.get("labels", [])

    @navigation_retry
    def get_or_create_label(self, name: str) -> str:
        """Return the label ID for ``name``, creating it (with nested-label
        visibility defaults) if it doesn't already exist. Idempotent."""
        for label in self.list_labels():
            if label.get("name") == name:
                return label["id"]
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = (
            self.service.users().labels().create(userId="me", body=body).execute()
        )
        return created["id"]

    @navigation_retry
    def apply_label(self, msg_id: str, label_id: str) -> None:
        """Add a label to a message."""
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [label_id]}
        ).execute()

    @navigation_retry
    def remove_label(self, msg_id: str, label_id: str) -> None:
        """Remove a label from a message."""
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": [label_id]}
        ).execute()

    @navigation_retry
    def search(self, query: str, max_results: int = 100) -> list[dict]:
        """Thread-scoped search returning one dict per matching message:

            {"id": <msg_id>, "thread_id": <thread_id>, "body_text": <str>}

        The Gmail API returns a bare list of ``{"id", "threadId"}`` refs;
        this method fetches each message's full payload so callers can
        read the reply body without a second round-trip.
        """
        results = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        out: list[dict] = []
        for ref in results.get("messages", []) or []:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            out.append(
                {
                    "id": msg.get("id"),
                    "thread_id": msg.get("threadId"),
                    "body_text": self._extract_body(msg, "text/plain"),
                }
            )
        return out

    @navigation_retry
    def reply_to_thread(self, thread_id: str, body: str) -> str:
        """Reply to an existing thread with a plain-text body. Returns the
        new message id. Uses ``threadId`` so the reply is RFC-threaded and
        shows up under the same conversation in the operator's inbox."""
        mime = MIMEText(body, "plain")
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        sent = (
            self.service.users()
            .messages()
            .send(
                userId="me",
                body={"raw": raw, "threadId": thread_id},
            )
            .execute()
        )
        return sent.get("id", "")

    @navigation_retry
    def send_with_labels(
        self,
        *,
        subject: str,
        body: str,
        to: str,
        labels: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> tuple[str, str]:
        """Send a plain-text email with optional inline attachments and
        pre-applied labels; return ``(msg_id, thread_id)``.

        Labels are applied AFTER the send in a follow-up ``modify`` call
        because the Gmail send API doesn't accept a ``labelIds`` field
        on ``users.messages.send``. This preserves the atomic contract
        callers rely on (``send_with_labels`` returns after both send +
        label-apply succeed)."""
        msg = MIMEMultipart()
        msg["to"] = to
        msg["subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        for filepath in attachments or []:
            filepath = Path(filepath)
            suffix = filepath.suffix.lower()
            maintype, subtype = self._MIME_MAP.get(
                suffix, ("application", "octet-stream")
            )
            with open(filepath, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
                encoders.encode_base64(part)
                # Quote filename so spaces + special characters survive MIME parsing.
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{filepath.name}"',
                )
                msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = (
            self.service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        msg_id = sent.get("id", "")
        thread_id = sent.get("threadId", "")

        if labels:
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": labels},
            ).execute()

        return msg_id, thread_id

    # ── S13 extensions: fast-path urgent alerts ────────────────────────────

    def _me(self) -> str:
        """Return the authenticated user's own address from MY_EMAIL.

        Raises ValueError if unset — callers (S13 fast-path notify) are
        expected to check reachability before invoking send_immediate.
        """
        addr = os.environ.get("MY_EMAIL", "").strip()
        if not addr:
            raise ValueError("MY_EMAIL is not set")
        return addr

    @navigation_retry(before_sleep_extra=_refresh_gmail_client_before_retry)
    def send_immediate(
        self,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
        to: str | None = None,
    ):
        """
        Send an urgent single-recipient alert.

        Retries per S11's `@navigation_retry` policy (3 attempts, exponential
        jitter backoff, credential refresh between attempts). On final
        failure the ORIGINAL exception propagates to the caller. S13's
        `notify.py` owns the swallow-and-log contract; this client stays
        honest.

        `to` defaults to `self._me()` (i.e. `MY_EMAIL`); callers can override
        to honor a `apply.fast_path_recipient` config value that differs from
        the operator's own account.
        """
        target = to if to is not None else self._me()
        self.send_email(
            to=target,
            subject=subject,
            body_text=body,
            attachments=attachments,
        )


# ── Standalone: run this file to complete OAuth setup ───────
if __name__ == "__main__":
    client = GmailClient()
    print("✓ Gmail authentication successful.")
    print("  Token saved. You won't need to re-authenticate unless it expires.")
