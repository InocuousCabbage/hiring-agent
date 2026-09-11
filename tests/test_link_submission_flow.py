"""
tests/test_link_submission_flow.py — HALF 2 T8: orchestrate the link-submission
run and reply to the SENDER.

Hermetic: the Gmail service is a MagicMock and run_pipeline is STUBBED (the real
tailor/cover-letter/QA chain is NEVER run — it would spend Ben's Anthropic key).
extract_job_links / build_jobs_from_links / compose_digest are cheap + pure and
run for real.

World-asserts:
  - test_digest_replies_to_sender_not_my_email: the digest goes to the message
    sender via send_email(to=sender), NOT to MY_EMAIL.
  - test_disallowed_sender_is_skipped: a sender not on the allowlist produces
    NO pipeline run and NO reply (FAIL-CLOSED, plan R3 / weemeemee GATE).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import main as main_mod  # noqa: E402

_LINK = "https://hiring.cafe/job/consumer-insights-manager-acme-remote-id123abc"
_SENDER = "jane@example.com"


def _config(allowed):
    return {
        "gmail": {
            "link_submission_subject_contains": "[apply]",
            "link_submission_processed_label": "hiring-agent-links-processed",
            "link_submission_allowed_senders": allowed,
        },
        "jobs": {"max_per_run": 5, "max_per_submission": 10},
        "apply": {"enabled": False},
    }


def _submission(sender=_SENDER, body_text=None):
    return {
        "id": "msg_links_1",
        "thread_id": "thread_1",
        "from": f"Jane Doe <{sender}>",
        "subject": "[apply] run these please",
        "body_text": body_text if body_text is not None else _LINK,
        "html": "",
    }


def _make_gmail(submissions):
    gmail = MagicMock()
    gmail.find_link_submissions.return_value = submissions
    return gmail


def _stub_pipeline(monkeypatch, calls=None):
    """Stub run_pipeline so the real LLM chain never runs. Records calls."""
    def _fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return ([], [], [])  # (processed, skipped, apply_events)
    fake = MagicMock(side_effect=_fake)
    monkeypatch.setattr(main_mod, "run_pipeline", fake)
    return fake


def test_digest_replies_to_sender_not_my_email(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "ben@example.com")
    _stub_pipeline(monkeypatch)
    gmail = _make_gmail([_submission()])

    main_mod.run_link_submissions(
        gmail=gmail,
        config=_config([_SENDER]),
        project_bank=[],
        today="2026-09-09",
        dry_run=False,
    )

    gmail.send_email.assert_called_once()
    _, kwargs = gmail.send_email.call_args
    assert kwargs["to"] == _SENDER
    assert kwargs["to"] != "ben@example.com"
    assert kwargs["subject"].startswith("Re:")


def test_disallowed_sender_is_skipped(monkeypatch):
    # Sender NOT on the allowlist => zero compute, no reply. FAIL-CLOSED.
    fake_pipeline = _stub_pipeline(monkeypatch)
    gmail = _make_gmail([_submission(sender="mallory@evil.example")])

    main_mod.run_link_submissions(
        gmail=gmail,
        config=_config([_SENDER]),  # only jane is allowed
        project_bank=[],
        today="2026-09-09",
        dry_run=False,
    )

    fake_pipeline.assert_not_called()
    gmail.send_email.assert_not_called()


def test_empty_allowlist_fails_closed(monkeypatch):
    # env:MY_EMAIL with MY_EMAIL unset => empty allowlist => nobody allowed.
    monkeypatch.delenv("MY_EMAIL", raising=False)
    fake_pipeline = _stub_pipeline(monkeypatch)
    gmail = _make_gmail([_submission()])

    main_mod.run_link_submissions(
        gmail=gmail,
        config=_config(["env:MY_EMAIL"]),
        project_bank=[],
        today="2026-09-09",
        dry_run=False,
    )

    fake_pipeline.assert_not_called()
    gmail.send_email.assert_not_called()


def test_marks_link_processed_label_only(monkeypatch):
    _stub_pipeline(monkeypatch)
    gmail = _make_gmail([_submission()])

    main_mod.run_link_submissions(
        gmail=gmail,
        config=_config([_SENDER]),
        project_bank=[],
        today="2026-09-09",
        dry_run=False,
    )

    gmail.mark_processed.assert_called_once()
    args, _ = gmail.mark_processed.call_args
    assert args[1] == "hiring-agent-links-processed"
    assert args[1] != "hiring-agent-processed"


def test_dry_run_sends_nothing_and_marks_nothing(monkeypatch):
    fake_pipeline = _stub_pipeline(monkeypatch)
    gmail = _make_gmail([_submission()])

    main_mod.run_link_submissions(
        gmail=gmail,
        config=_config([_SENDER]),
        project_bank=[],
        today="2026-09-09",
        dry_run=True,
    )

    # Pipeline still runs (dry-run threads through), but nothing is sent/marked.
    fake_pipeline.assert_called_once()
    assert fake_pipeline.call_args.kwargs["dry_run"] is True
    gmail.send_email.assert_not_called()
    gmail.mark_processed.assert_not_called()
