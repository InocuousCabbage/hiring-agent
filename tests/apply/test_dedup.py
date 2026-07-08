"""tests/apply/test_dedup.py — S5 RED tests for the SQLite dedup DB.

These tests are the frozen contract for `src/apply/dedup.py`. Every test name
here is listed in the S5 spec §TDD test scaffolding and each corresponds to at
least one acceptance criterion.

The tests are import-time coupled to `src.apply.dedup`. To make the module
resolvable, we prepend the repo root to `sys.path`. The CLI test runs the
module via a subprocess and therefore relies on the same namespace layout
(implicit namespace package `src` + regular package `src.apply`).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.apply.dedup import (  # noqa: E402
    AlreadyAppliedError,
    DedupDB,
    normalize_company,
    normalize_role,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _apply_result(
    *,
    status: str,
    ats: str = "boards.greenhouse.io",
    apply_url: str = "https://boards.greenhouse.io/acme/jobs/12345",
    application_id: str | None = None,
    confirmation_screenshot: str | None = None,
    trace_path: str | None = None,
    review_id: str | None = None,
    submitted_at: str | None = None,
):
    """Duck-typed ApplyResult stand-in (Layer 1 types module is not in this branch)."""
    return SimpleNamespace(
        status=status,
        ats=ats,
        apply_url=apply_url,
        application_id=application_id,
        confirmation_screenshot=confirmation_screenshot,
        trace_path=trace_path,
        review_id=review_id,
        submitted_at=submitted_at,
    )


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "applied_jobs.db"


# ── Constructor / migration ──────────────────────────────────────────────────

def test_constructor_creates_db_and_sets_permissions(tmp_path):
    path = _db_path(tmp_path)
    DedupDB(path)

    assert path.exists(), "DB file should exist after DedupDB() constructor"

    mode = stat.S_IMODE(path.stat().st_mode)
    assert oct(mode) == "0o600", f"DB file mode should be 0o600 (got {oct(mode)})"

    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert oct(parent_mode) == "0o700", (
        f"parent dir mode should be 0o700 (got {oct(parent_mode)})"
    )


def test_migration_idempotent(tmp_path):
    path = _db_path(tmp_path)
    DedupDB(path)
    # Second construction must not fail; CREATE TABLE IF NOT EXISTS guarantees.
    DedupDB(path)


# ── Hard duplicate (ON CONFLICT ABORT via IntegrityError) ────────────────────

def test_hard_duplicate_raises_already_applied_error(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    result = _apply_result(
        status="submitted",
        ats="boards.greenhouse.io",
        apply_url="https://boards.greenhouse.io/acme/jobs/12345",
    )
    db.record(
        result=result,
        applicant="ben",
        company="Acme",
        role_title="Staff Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/12345",
    )

    with pytest.raises(AlreadyAppliedError):
        db.record(
            result=result,
            applicant="ben",
            company="Acme",
            role_title="Staff Engineer",
            job_url="https://boards.greenhouse.io/acme/jobs/12345",
        )


# ── was_applied ──────────────────────────────────────────────────────────────

def test_was_applied_true_on_triple_match_regardless_of_url(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    result = _apply_result(status="submitted")
    db.record(
        result=result,
        applicant="ben",
        company="Acme",
        role_title="Staff Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/12345",
    )

    # Now the *ats_domain + ats_job_id* uniquely identify the job even under
    # a different job_url. The row's ats_domain/ats_job_id are derived from
    # result.ats and result.apply_url — see record() contract.
    assert db.was_applied(
        company="Acme",
        ats_domain="boards.greenhouse.io",
        ats_job_id="12345",
        job_url="https://different.example/acme-repost",
    ) is True


def test_was_applied_falls_back_to_job_url_when_ats_job_id_none(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    result = _apply_result(
        status="review_required",
        ats="",  # unknown ATS
        apply_url="",
    )
    db.record(
        result=result,
        applicant="ben",
        company="Acme",
        role_title="Staff Engineer",
        job_url="https://direct-employer.example/careers/xyz",
    )

    # ats_domain / ats_job_id None → fall back to job_url match.
    assert db.was_applied(
        company="Acme",
        ats_domain=None,
        ats_job_id=None,
        job_url="https://direct-employer.example/careers/xyz",
    ) is True

    # Sanity: a different URL does NOT match.
    assert db.was_applied(
        company="Acme",
        ats_domain=None,
        ats_job_id=None,
        job_url="https://direct-employer.example/careers/other",
    ) is False


# ── soft_warn_check ──────────────────────────────────────────────────────────

def test_soft_warn_returns_prior_rows_ordered_desc(tmp_path, monkeypatch):
    """Two prior applies to the same normalized (company, role), different job_urls.
    The result must contain both, newest first."""
    import src.apply.dedup as dedup_mod

    db = DedupDB(_db_path(tmp_path))

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    times = iter([now - timedelta(days=2), now - timedelta(days=1)])
    monkeypatch.setattr(dedup_mod, "_utcnow", lambda: next(times))

    db.record(
        result=_apply_result(
            status="submitted",
            apply_url="https://boards.greenhouse.io/acme/jobs/11111",
        ),
        applicant="ben",
        company="Acme, Inc.",
        role_title="Sr. Staff Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/11111",
    )
    db.record(
        result=_apply_result(
            status="submitted",
            apply_url="https://boards.greenhouse.io/acme/jobs/22222",
        ),
        applicant="ben",
        company="Acme LLC",  # normalizes same
        role_title="Staff Engineer",  # normalizes same
        job_url="https://boards.greenhouse.io/acme/jobs/22222",
    )

    hits = db.soft_warn_check(
        company_normalized=normalize_company("Acme, Inc."),
        role_title_normalized=normalize_role("Sr. Staff Engineer"),
    )
    assert isinstance(hits, list), "soft_warn_check must return a list"
    assert len(hits) == 2, f"expected 2 soft-warn hits, got {len(hits)}"
    # DESC by applied_at: newest first.
    assert hits[0]["applied_at"] >= hits[1]["applied_at"], (
        f"expected DESC ordering; got {hits[0]['applied_at']} then {hits[1]['applied_at']}"
    )


def test_soft_warn_empty_when_normalized_pair_absent(tmp_path):
    db = DedupDB(_db_path(tmp_path))
    assert db.soft_warn_check("acme", "engineer") == []


# ── count_today ──────────────────────────────────────────────────────────────

def test_count_today_uses_utc_midnight_boundary(tmp_path, monkeypatch):
    import src.apply.dedup as dedup_mod

    db = DedupDB(_db_path(tmp_path))

    fixed_today = datetime(2026, 6, 15, 4, 0, 0, tzinfo=timezone.utc)

    # First insert: T-25h (yesterday, before UTC midnight).
    monkeypatch.setattr(dedup_mod, "_utcnow", lambda: fixed_today - timedelta(hours=25))
    db.record(
        result=_apply_result(
            status="submitted",
            ats="boards.greenhouse.io",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    # Second insert: T-1h (today, after UTC midnight).
    monkeypatch.setattr(dedup_mod, "_utcnow", lambda: fixed_today - timedelta(hours=1))
    db.record(
        result=_apply_result(
            status="submitted",
            ats="boards.greenhouse.io",
            apply_url="https://boards.greenhouse.io/acme/jobs/2",
        ),
        applicant="ben",
        company="Beta",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/2",
    )

    # Freeze "now" at fixed_today for count_today().
    monkeypatch.setattr(dedup_mod, "_utcnow", lambda: fixed_today)

    assert db.count_today("boards.greenhouse.io") == 1


def test_count_today_per_ats_domain_isolated(tmp_path, monkeypatch):
    import src.apply.dedup as dedup_mod

    db = DedupDB(_db_path(tmp_path))
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dedup_mod, "_utcnow", lambda: now)

    for i in range(3):
        db.record(
            result=_apply_result(
                status="submitted",
                ats="boards.greenhouse.io",
                apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            ),
            applicant="ben",
            company=f"GH-{i}",
            role_title="Engineer",
            job_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
        )
    for i in range(2):
        db.record(
            result=_apply_result(
                status="submitted",
                ats="jobs.lever.co",
                apply_url=f"https://jobs.lever.co/beta/{i}",
            ),
            applicant="ben",
            company=f"Lever-{i}",
            role_title="Engineer",
            job_url=f"https://jobs.lever.co/beta/{i}",
        )

    assert db.count_today("boards.greenhouse.io") == 3
    assert db.count_today("jobs.lever.co") == 2


# ── record() status gating ───────────────────────────────────────────────────

def test_record_skips_write_on_failed_status(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    db.record(
        result=_apply_result(status="failed"),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    # Row count should be unchanged.
    with sqlite3.connect(str(_db_path(tmp_path))) as conn:
        row_count = conn.execute("SELECT COUNT(*) FROM applied_jobs").fetchone()[0]
    assert row_count == 0


def test_record_writes_review_id_when_present(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    db.record(
        result=_apply_result(
            status="review_required",
            review_id="019078e0-abcd-7890-1234-56789abcdef0",
        ),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/9",
    )

    with sqlite3.connect(str(_db_path(tmp_path))) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT review_id FROM applied_jobs").fetchone()
    assert row["review_id"] == "019078e0-abcd-7890-1234-56789abcdef0"


# ── unblock ─────────────────────────────────────────────────────────────────

def test_unblock_removes_row_and_returns_count(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    url = "https://boards.greenhouse.io/acme/jobs/12345"
    db.record(
        result=_apply_result(
            status="submitted",
            ats="boards.greenhouse.io",
            apply_url=url,
        ),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url=url,
    )

    n = db.unblock(url)
    assert n == 1

    assert db.was_applied(
        company="Acme",
        ats_domain="boards.greenhouse.io",
        ats_job_id="12345",
        job_url=url,
    ) is False


def test_unblock_returns_zero_when_no_match(tmp_path):
    db = DedupDB(_db_path(tmp_path))
    assert db.unblock("https://nothing.example/") == 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_unblock_prints_and_exits_zero(tmp_path):
    """Boot the DB, record one row, invoke `python -m src.apply.dedup --unblock <url>`
    with HIRING_AGENT_DEDUP_DB pointing at the temp DB, and assert output +
    exit code."""
    db_path = _db_path(tmp_path)
    db = DedupDB(db_path)

    url = "https://boards.greenhouse.io/acme/jobs/12345"
    db.record(
        result=_apply_result(
            status="submitted",
            ats="boards.greenhouse.io",
            apply_url=url,
        ),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url=url,
    )

    env = os.environ.copy()
    env["HIRING_AGENT_DEDUP_DB"] = str(db_path)

    proc = subprocess.run(
        [sys.executable, "-m", "src.apply.dedup", "--unblock", url],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )

    assert proc.returncode == 0, (
        f"expected exit 0; got {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "unblocked 1 row(s)" in proc.stdout, (
        f"expected 'unblocked 1 row(s)' in stdout; got: {proc.stdout!r}"
    )


# ── Normalizers ──────────────────────────────────────────────────────────────

def test_normalize_company_strips_legal_suffix():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("  Acme LLC ") == "acme"
    assert normalize_company("Acme Corp") == "acme"


def test_normalize_role_strips_seniority_prefix():
    assert normalize_role("Sr. Staff Engineer") == "engineer"
    assert normalize_role("Senior Software Engineer") == "software engineer"
    assert normalize_role("Principal Engineer") == "engineer"


# ── Datetime discipline (L6) ─────────────────────────────────────────────────

def test_all_datetimes_are_utc_iso(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    db.record(
        result=_apply_result(
            status="submitted",
            ats="boards.greenhouse.io",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        applicant="ben",
        company="Acme",
        role_title="Engineer",
        job_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    with sqlite3.connect(str(_db_path(tmp_path))) as conn:
        applied_at = conn.execute("SELECT applied_at FROM applied_jobs").fetchone()[0]

    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$", applied_at
    ), f"applied_at must be ISO-8601 UTC with +00:00 suffix; got {applied_at!r}"


def test_no_naive_datetime_utcnow_in_module():
    """L6: assert `datetime.utcnow` never appears in dedup.py source."""
    dedup_src = (ROOT / "src" / "apply" / "dedup.py").read_text()
    assert "datetime.utcnow" not in dedup_src, (
        "L6 landmine: found `datetime.utcnow` in src/apply/dedup.py — "
        "use `datetime.now(timezone.utc)` (helper `_utcnow()`) instead"
    )


# ── review_pending roundtrip ────────────────────────────────────────────────

def test_review_pending_roundtrip(tmp_path):
    db = DedupDB(_db_path(tmp_path))

    review_id = "019078e0-abcd-7890-1234-56789abcdef0"
    db.insert_review_pending(
        review_id=review_id,
        job_url="https://boards.greenhouse.io/acme/jobs/99",
        apply_url="https://boards.greenhouse.io/acme/jobs/99/application",
        company="Acme",
        role_title="Staff Engineer",
        ats="boards.greenhouse.io",
        screenshot_path="/tmp/foo.png",
        trace_path=None,
        gmail_thread_id="thread_abc",
    )

    row = db.get_review_pending(review_id)
    assert row is not None, "get_review_pending returned None after insert"
    for key in (
        "review_id",
        "job_url",
        "apply_url",
        "company",
        "role_title",
        "ats",
        "screenshot_path",
    ):
        assert key in row, f"expected key {key!r} in review_pending row; got {row!r}"
    assert row["review_id"] == review_id
    assert row["job_url"] == "https://boards.greenhouse.io/acme/jobs/99"


# ── db_path resolution (CWD split-brain guard) ──────────────────────────────


def test_dedup_db_path_resolves_relative_default_against_repo_root(tmp_path, monkeypatch):
    """Regression guard: config-defaulted ``dedup_db_path`` must resolve against
    the repo root, not CWD.

    Bug: a naive ``Path("state/applied_jobs.db")`` is CWD-relative. Running the
    pipeline from repo root today and from a different CWD tomorrow (cron w/ a
    different working dir) creates TWO separate SQLite DBs — job applied via DB
    A can be re-submitted through DB B, silent double-apply.
    """
    from src.apply.dedup import _resolve_db_path

    monkeypatch.chdir(tmp_path)  # simulate running from a different CWD
    resolved = _resolve_db_path({"apply": {}})  # use default fallback

    # The resolved path must be ABSOLUTE and rooted at the repo, not at tmp_path.
    assert resolved.is_absolute(), f"expected absolute path; got {resolved!r}"
    assert resolved.name == "applied_jobs.db"
    assert resolved.parent.name == "state"
    # tmp_path (the simulated foreign CWD) must NOT appear in the resolved path.
    assert str(tmp_path) not in str(resolved), (
        f"resolved leaked CWD (would cause split-brain DB): {resolved}"
    )
    # Default fallback is 'state/applied_jobs.db' — 2 components — so the
    # repo-root anchor sits at parents[1] (parents[0] is 'state/').
    assert resolved.parents[1] == ROOT, (
        f"expected repo-root anchor {ROOT}; got parents[1]={resolved.parents[1]}"
    )


def test_dedup_db_path_absolute_config_value_is_preserved(tmp_path, monkeypatch):
    """Absolute paths in config must pass through unchanged (no repo-root prepend)."""
    from src.apply.dedup import _resolve_db_path

    monkeypatch.chdir(tmp_path)  # CWD irrelevant when config is absolute
    abs_path = tmp_path / "custom" / "dedup.db"
    config = {"apply": {"dedup_db_path": str(abs_path)}}
    resolved = _resolve_db_path(config)
    assert resolved == abs_path


def test_dedup_db_path_expanduser_on_home_relative(monkeypatch):
    """Home-relative paths in config expand ``~`` to the user home."""
    from src.apply.dedup import _resolve_db_path

    config = {"apply": {"dedup_db_path": "~/hiring-state/applied.db"}}
    resolved = _resolve_db_path(config)
    assert resolved.is_absolute()
    assert str(resolved).startswith(str(Path.home()))
    assert resolved.name == "applied.db"
    assert resolved.parent.name == "hiring-state"


def test_dedup_db_path_config_relative_value_anchored_to_repo_root(tmp_path, monkeypatch):
    """A config-supplied RELATIVE path (e.g. 'var/dedup.db') anchors at repo
    root, not CWD — same split-brain guard as the default fallback."""
    from src.apply.dedup import _resolve_db_path

    monkeypatch.chdir(tmp_path)
    config = {"apply": {"dedup_db_path": "var/dedup.db"}}
    resolved = _resolve_db_path(config)
    assert resolved.is_absolute()
    assert str(tmp_path) not in str(resolved)
    assert resolved.parents[1] == ROOT, (
        f"expected repo-root anchor {ROOT}; got parents[1]={resolved.parents[1]}"
    )
    assert resolved.name == "dedup.db"
    assert resolved.parent.name == "var"
