"""
tests/gmail/test_alert_intake.py — Hiring.cafe alert intake: sender match,
lookback window, drain-with-cap, and invalid_grant re-auth fallback.

Locks:
  - the alert query matches the SENDER as well as the subject marker. Since
    ~June 2026 Hiring.cafe digests are titled "N new jobs for <search>", so
    "HiringCafe" is only in the sender display name and a subject-only query
    misses every current digest. Forwarded copies (From: the user, Subject
    "Fwd: ... HiringCafe ...") are still matched through the subject clause.
  - only digests inside the lookback window (gmail.alert_lookback_days,
    default 14) are picked. Older unprocessed digests are ignored, NOT
    labelled, so the months-old backlog is never drained.
  - every in-window unprocessed digest is returned, OLDEST first, capped at
    gmail.max_alerts_per_run.
  - main() processes each returned digest, labels each only after its digest
    email is sent, and stops at the first failure so the rest stay
    unlabelled for the next run.
  - an expired refresh grant (RefreshError invalid_grant) falls through to
    the interactive consent flow when a human is present, and raises the
    grep-able AuthError naming invalid_grant when headless. The old token is
    never deleted; it is only replaced once a new one has been written.

Mocks only. Nothing here touches the network or a real token.
"""
import base64
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

SENDER = "ali@hiring.cafe"
SUBJECT_MARKER = "HiringCafe"
LABEL = "hiring-agent-processed"
NOW = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode()


# ── Fake Gmail service ────────────────────────────────────────────────────
#
# Interprets the small query grammar the alert reader is allowed to emit:
#   (from:<addr> OR subject:"<marker>")  |  subject:"<marker>"
#   -label:<name>
#   newer_than:<N>d
# Any other token raises, so a query-shape drift fails loudly here instead of
# being silently ignored by the double.

class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeMessages:
    PAGE = 3  # small page size so the reader must follow nextPageToken

    def __init__(self, inbox):
        # inbox: list of dicts {id, from, subject, labels, date}
        self._inbox = inbox
        self.queries: list[str] = []
        self.got: list[str] = []

    def _matches(self, msg, q):
        rest = q
        m = re.search(r'\(from:(\S+) OR subject:"([^"]*)"\)', rest)
        if m:
            sender, marker = m.group(1), m.group(2)
            if not (msg["from"] == sender or marker in msg["subject"]):
                return False
            rest = rest.replace(m.group(0), "")
        else:
            m = re.search(r'subject:"([^"]*)"', rest)
            assert m, f"query has no subject/sender clause: {q!r}"
            if m.group(1) not in msg["subject"]:
                return False
            rest = rest.replace(m.group(0), "")
        m = re.search(r"-label:(\S+)", rest)
        if m:
            if m.group(1) in msg["labels"]:
                return False
            rest = rest.replace(m.group(0), "")
        m = re.search(r"newer_than:(\d+)d", rest)
        if m:
            if msg["date"] < NOW - timedelta(days=int(m.group(1))):
                return False
            rest = rest.replace(m.group(0), "")
        assert not rest.strip(), f"unexpected query tokens {rest!r} in {q!r}"
        return True

    def list(self, userId, q, maxResults=100, pageToken=None):
        self.queries.append(q)
        hits = sorted(
            (m for m in self._inbox if self._matches(m, q)),
            key=lambda m: m["date"],
            reverse=True,  # Gmail lists newest first
        )
        start = int(pageToken or 0)
        size = min(maxResults, self.PAGE)
        page = hits[start:start + size]
        payload = {"messages": [{"id": m["id"]} for m in page]}
        if start + size < len(hits):
            payload["nextPageToken"] = str(start + size)
        return _Exec(payload)

    def get(self, userId, id, format):
        self.got.append(id)
        return _Exec({
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64(f"text {id}")}},
                    {"mimeType": "text/html", "body": {"data": _b64(f"<p>{id}</p>")}},
                ],
            }
        })


class _FakeService:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return self

    def messages(self):
        return self._messages


def _client(inbox):
    from gmail.client import GmailClient

    client = GmailClient.__new__(GmailClient)  # skip OAuth
    client.creds = None
    client._label_cache = None
    messages = _FakeMessages(inbox)
    client.service = _FakeService(messages)
    return client, messages


def _msg(id, days_ago, *, sender=SENDER, subject="18 new jobs for Marketing", labels=()):
    return {
        "id": id,
        "from": sender,
        "subject": subject,
        "labels": set(labels),
        "date": NOW - timedelta(days=days_ago),
    }


def _find(client, **kw):
    kw.setdefault("sender", SENDER)
    kw.setdefault("subject_contains", SUBJECT_MARKER)
    kw.setdefault("processed_label", LABEL)
    return client.find_unprocessed_alerts(**kw)


# ── Query shape ───────────────────────────────────────────────────────────

def test_query_includes_sender_subject_label_and_window():
    client, messages = _client([])
    _find(client, lookback_days=14)
    q = messages.queries[0]
    assert f"from:{SENDER}" in q
    assert f'subject:"{SUBJECT_MARKER}"' in q
    assert f"-label:{LABEL}" in q
    assert "newer_than:14d" in q


def test_lookback_defaults_to_14_days():
    client, messages = _client([])
    _find(client)
    assert "newer_than:14d" in messages.queries[0]


def test_query_sanitizes_sender():
    client, messages = _client([])
    _find(client, sender='ali@hiring.cafe" OR from:evil')
    assert '"' not in messages.queries[0].split("subject:")[0]


# ── Matching ──────────────────────────────────────────────────────────────

def test_new_format_digest_found_by_sender_alone():
    """Subject has no "HiringCafe"; only the sender identifies it."""
    client, _ = _client([_msg("new_fmt", 2, subject="18 new jobs for Marketing")])
    assert [a["id"] for a in _find(client)] == ["new_fmt"]


def test_forwarded_copy_still_found_by_subject():
    client, _ = _client([
        _msg("fwd", 1, sender="someone@example.com",
             subject="Fwd: Your HiringCafe job alert"),
    ])
    assert [a["id"] for a in _find(client)] == ["fwd"]


def test_unrelated_mail_not_matched():
    client, _ = _client([
        _msg("other", 1, sender="news@example.com", subject="18 new jobs for Marketing"),
    ])
    assert _find(client) == []


def test_already_labelled_excluded():
    client, _ = _client([
        _msg("done", 3, labels=[LABEL]),
        _msg("todo", 2),
    ])
    assert [a["id"] for a in _find(client)] == ["todo"]


def test_digest_outside_window_not_picked():
    """A March digest, unprocessed, is ignored rather than drained."""
    client, _ = _client([
        _msg("march", (NOW - datetime(2026, 3, 9, tzinfo=timezone.utc)).days),
        _msg("recent", 3),
    ])
    assert [a["id"] for a in _find(client, lookback_days=14)] == ["recent"]


def test_in_window_new_format_digest_picked():
    client, _ = _client([_msg("oct", 5, subject="7 new jobs for Marketing")])
    assert [a["id"] for a in _find(client, lookback_days=14)] == ["oct"]


# ── Ordering, paging, cap ─────────────────────────────────────────────────

def test_all_in_window_returned_oldest_first_across_pages():
    inbox = [_msg(f"d{age:02d}", age) for age in (1, 3, 5, 7, 9, 11, 13)]
    client, _ = _client(inbox)
    got = [a["id"] for a in _find(client, max_results=10)]
    assert got == ["d13", "d11", "d09", "d07", "d05", "d03", "d01"]


def test_cap_respected_and_takes_oldest():
    inbox = [_msg(f"d{age:02d}", age) for age in range(1, 13)]  # 12 in window
    client, messages = _client(inbox)
    got = [a["id"] for a in _find(client, max_results=4)]
    assert got == ["d12", "d11", "d10", "d09"]
    # Bodies fetched only for the capped selection, not the whole window.
    assert sorted(messages.got) == sorted(got)


def test_returned_alert_shape():
    client, _ = _client([_msg("one", 1)])
    (alert,) = _find(client)
    assert alert == {"id": "one", "html": "<p>one</p>", "text": "text one"}


# ── main(): drain loop ────────────────────────────────────────────────────

def _drive_main(monkeypatch, tmp_path, main_root_with_config, alerts,
                send_side_effect=None):
    import main as main_mod
    import gmail.client as gmail_client_mod

    gmail = MagicMock()
    gmail.find_unprocessed_alerts.return_value = alerts
    if send_side_effect is not None:
        gmail.send_digest.side_effect = send_side_effect
    monkeypatch.setattr(gmail_client_mod, "GmailClient", lambda: gmail)
    monkeypatch.setattr(
        main_mod, "parse_alert_email",
        lambda html_body, text_body, max_jobs: [
            {"title": "Engineer", "company": "Acme", "url": "https://example.com"}
        ],
    )
    monkeypatch.setattr(main_mod, "run_pipeline", lambda **kw: ([], [], []))
    main_root_with_config(monkeypatch, tmp_path)
    monkeypatch.setenv("MY_EMAIL", "jane@example.com")
    monkeypatch.setattr(sys, "argv", ["main.py"])
    main_mod.main()
    return gmail


def _alerts(n):
    return [{"id": f"a{i}", "html": "<html></html>", "text": ""} for i in range(1, n + 1)]


def _labelled(gmail):
    return [c.args[0] for c in gmail.mark_processed.call_args_list]


def test_main_processes_every_alert_in_one_run(monkeypatch, tmp_path, main_root_with_config):
    gmail = _drive_main(monkeypatch, tmp_path, main_root_with_config, _alerts(3))
    assert gmail.send_digest.call_count == 3
    assert _labelled(gmail) == ["a1", "a2", "a3"]


def test_main_failure_on_n_leaves_n_to_end_unlabelled(monkeypatch, tmp_path, main_root_with_config):
    gmail = _drive_main(
        monkeypatch, tmp_path, main_root_with_config, _alerts(4),
        send_side_effect=[None, Exception("SMTP timeout"), None, None],
    )
    assert _labelled(gmail) == ["a1"]
    # Stops at the failure: a3 and a4 are not even attempted this run.
    assert gmail.send_digest.call_count == 2


def test_main_passes_window_and_cap_from_config(monkeypatch, tmp_path, main_root_with_config):
    import yaml

    def _with_keys(mp, tp):
        main_root_with_config(mp, tp)
        cfg_path = tp / "config" / "settings.yaml"
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["gmail"]["alert_lookback_days"] = 30
        cfg["gmail"]["max_alerts_per_run"] = 3
        cfg_path.write_text(yaml.safe_dump(cfg))
        return tp

    gmail = _drive_main(monkeypatch, tmp_path, _with_keys, [])
    _, kwargs = gmail.find_unprocessed_alerts.call_args
    assert kwargs["sender"] == SENDER
    assert kwargs["lookback_days"] == 30
    assert kwargs["max_results"] == 3


def test_main_defaults_window_14_and_cap_10(monkeypatch, tmp_path, main_root_with_config):
    import yaml

    def _without_keys(mp, tp):
        main_root_with_config(mp, tp)
        cfg_path = tp / "config" / "settings.yaml"
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["gmail"].pop("alert_lookback_days", None)
        cfg["gmail"].pop("max_alerts_per_run", None)
        cfg_path.write_text(yaml.safe_dump(cfg))
        return tp

    gmail = _drive_main(monkeypatch, tmp_path, _without_keys, [])
    _, kwargs = gmail.find_unprocessed_alerts.call_args
    assert kwargs["lookback_days"] == 14
    assert kwargs["max_results"] == 10


# ── _authenticate: invalid_grant fallback ─────────────────────────────────

class _StaleCreds:
    valid = False
    expired = True
    refresh_token = "stale-refresh"

    def __init__(self, exc):
        self._exc = exc

    def refresh(self, request):
        raise self._exc


class _NewCreds:
    valid = True

    def to_json(self):
        return '{"token": "new"}'


def _invalid_grant():
    from google.auth.exceptions import RefreshError
    return RefreshError(
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )


@pytest.fixture
def stale_token(monkeypatch, tmp_path):
    import gmail.client as gc

    token = tmp_path / "creds" / "token.json"
    token.parent.mkdir()
    token.write_text('{"token": "old"}')
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "creds" / "credentials.json"))

    def _install(exc):
        monkeypatch.setattr(
            gc.Credentials, "from_authorized_user_file",
            staticmethod(lambda path, scopes: _StaleCreds(exc)),
        )
    return token, _install


def test_invalid_grant_headless_raises_auth_error_naming_it(monkeypatch, stale_token):
    import gmail.client as gc

    token, install = stale_token
    install(_invalid_grant())
    monkeypatch.setenv("HIRING_AGENT_HEADLESS", "1")
    flow = MagicMock()
    monkeypatch.setattr(gc, "InstalledAppFlow", flow)

    with pytest.raises(gc.AuthError, match="invalid_grant"):
        gc.GmailClient.__new__(gc.GmailClient)._authenticate()

    flow.from_client_secrets_file.assert_not_called()
    assert token.read_text() == '{"token": "old"}'  # untouched


def test_invalid_grant_interactive_falls_through_to_consent(monkeypatch, stale_token):
    import gmail.client as gc

    token, install = stale_token
    install(_invalid_grant())
    monkeypatch.delenv("HIRING_AGENT_HEADLESS", raising=False)
    monkeypatch.setenv("HIRING_AGENT_INTERACTIVE_OAUTH", "1")

    seen_at_consent = {}

    def _run_local_server(port):
        # The old token must still be on disk while the human consents.
        seen_at_consent["token"] = token.read_text()
        return _NewCreds()

    flow = MagicMock()
    flow.from_client_secrets_file.return_value.run_local_server.side_effect = _run_local_server
    monkeypatch.setattr(gc, "InstalledAppFlow", flow)

    creds = gc.GmailClient.__new__(gc.GmailClient)._authenticate()

    assert isinstance(creds, _NewCreds)
    assert seen_at_consent["token"] == '{"token": "old"}'
    assert token.read_text() == '{"token": "new"}'


def test_other_refresh_errors_still_propagate(monkeypatch, stale_token):
    """Only invalid_grant means 're-consent'. A transient token-endpoint
    failure must not open a browser or be relabelled."""
    import gmail.client as gc
    from google.auth.exceptions import RefreshError

    token, install = stale_token
    install(RefreshError("temporarily_unavailable", {"error": "temporarily_unavailable"}))
    monkeypatch.setenv("HIRING_AGENT_INTERACTIVE_OAUTH", "1")
    flow = MagicMock()
    monkeypatch.setattr(gc, "InstalledAppFlow", flow)

    with pytest.raises(RefreshError):
        gc.GmailClient.__new__(gc.GmailClient)._authenticate()
    flow.from_client_secrets_file.assert_not_called()
    assert token.read_text() == '{"token": "old"}'
