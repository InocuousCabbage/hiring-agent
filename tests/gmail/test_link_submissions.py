"""
tests/gmail/test_link_submissions.py — HALF 2 T7: the link-submission Gmail
reader.

Locks:
  - the query is MUTUALLY EXCLUSIVE with the alert query — it contains the link
    subject marker and the link processed-label, and NOT the alert subject
    marker (Global constraint 4 / plan R8).
  - each returned message dict surfaces `from`, `subject`, and BOTH the
    text/plain (`body_text`) and text/html (`html`) bodies, so T8 can reply to
    the sender and T6 can read links out of either body.
"""
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode()


class _FakeExecutable:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeMessages:
    """Records the query string and serves one canned full message."""

    def __init__(self, full_msg):
        self.last_query = None
        self._full_msg = full_msg

    def list(self, userId, q, maxResults):
        self.last_query = q
        return _FakeExecutable({"messages": [{"id": "msg_links_1"}]})

    def get(self, userId, id, format):
        return _FakeExecutable(self._full_msg)


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, messages):
        self._users = _FakeUsers(messages)

    def users(self):
        return self._users


def _make_client(full_msg):
    from gmail.client import GmailClient

    client = GmailClient.__new__(GmailClient)
    client.creds = None
    messages = _FakeMessages(full_msg)
    client.service = _FakeService(messages)
    client._label_cache = None
    return client, messages


def _full_msg():
    return {
        "id": "msg_links_1",
        "threadId": "thread_links_1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Jane Doe <jane@example.com>"},
                {"name": "Subject", "value": "[apply] please run these"},
            ],
            "parts": [
                {"mimeType": "text/plain",
                 "body": {"data": _b64("https://hiring.cafe/job/foo-acme-id123abc")}},
                {"mimeType": "text/html",
                 "body": {"data": _b64('<a href="https://hiring.cafe/job/foo-acme-id123abc">x</a>')}},
            ],
        },
    }


class TestFindLinkSubmissions:
    def test_query_is_mutually_exclusive_with_alert(self):
        client, messages = _make_client(_full_msg())
        client.find_link_submissions(
            subject_marker="[apply]",
            processed_label="hiring-agent-links-processed",
            max_results=10,
        )
        q = messages.last_query
        assert "[apply]" in q
        assert "-label:hiring-agent-links-processed" in q
        # Must NOT collide with the alert path's subject or label.
        assert "HiringCafe" not in q
        assert "hiring-agent-processed" not in q.replace(
            "hiring-agent-links-processed", ""
        )

    def test_returns_from_subject_and_bodies(self):
        client, _ = _make_client(_full_msg())
        out = client.find_link_submissions(
            subject_marker="[apply]",
            processed_label="hiring-agent-links-processed",
        )
        assert len(out) == 1
        msg = out[0]
        assert msg["id"] == "msg_links_1"
        assert msg["thread_id"] == "thread_links_1"
        assert msg["from"] == "Jane Doe <jane@example.com>"
        assert msg["subject"] == "[apply] please run these"
        assert "hiring.cafe/job/foo-acme-id123abc" in msg["body_text"]
        assert "hiring.cafe/job/foo-acme-id123abc" in msg["html"]
