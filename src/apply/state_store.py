"""
src/apply/state_store.py — thin CRUD wrapper over the ``review_pending`` table.

S12 owns the CRUD; the schema is master-plan §4.6. In production, S5's
``001_init.sql`` migration creates the table; here we run
``CREATE TABLE IF NOT EXISTS`` so unit tests can point at ``:memory:`` or
an ephemeral ``tmp_path`` DB without depending on S5's migration runner.

Design contracts:
- Every SQL statement is parameterized (no string-interpolated user data).
- One persistent ``sqlite3.Connection`` per store instance, so ``:memory:``
  DBs survive across method calls in tests.
- All ISO-8601 timestamps go through the caller — this module never calls
  the deprecated naive UTC-now API; L6 is enforced end-to-end in review.py
  which is the sole timestamp source for every method here.
- ``mark_repinged`` atomically increments ``repings_sent`` and updates
  ``last_repinged_at`` in a single UPDATE.
- ``mark_resolved`` updates ``resolution`` + ``resolved_at`` together so
  ``list_open()`` cannot race a half-written row.

H1 reconciliation (2026-07-07): the ``001_init.sql`` migration is now the
SINGLE SOURCE OF TRUTH for the ``review_pending`` schema. ReviewStore uses
``CREATE TABLE IF NOT EXISTS`` with the SAME column definitions so tests can
still spin up ``:memory:`` or ``tmp_path`` databases without a separate
migration step, but if a DedupDB migration ran first the CREATE is a no-op
and the column names line up. See tests/apply/test_h1_schema_reconciliation.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


# ── Schema (must stay byte-identical to ``migrations/001_init.sql``) ────────

_CREATE_REVIEW_PENDING = """
CREATE TABLE IF NOT EXISTS review_pending (
    review_id           TEXT PRIMARY KEY,
    job_url             TEXT NOT NULL,
    apply_url           TEXT NOT NULL,
    company             TEXT NOT NULL,
    role_title          TEXT NOT NULL,
    ats                 TEXT NOT NULL,
    filled_at           TEXT NOT NULL,
    screenshot_path     TEXT NOT NULL,
    trace_path          TEXT,
    first_sent_at       TEXT NOT NULL,
    last_repinged_at    TEXT,
    repings_sent        INTEGER NOT NULL DEFAULT 0,
    gmail_thread_id     TEXT,
    resolution          TEXT,
    resolved_at         TEXT,
    resume_path         TEXT,
    cover_letter_path   TEXT,
    applicant           TEXT,
    clarified_at        TEXT
)
"""

# H4/M1/M12 additive columns: resume_path, cover_letter_path, applicant,
# clarified_at. Older DBs (created by migration 001) predate these columns —
# we ALTER them in on-open so both fresh CREATEs and old CREATEs converge on
# the same shape. ADDs are idempotent-ish: sqlite raises on duplicate columns,
# which we catch and ignore.
_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("resume_path", "TEXT"),
    ("cover_letter_path", "TEXT"),
    ("applicant", "TEXT"),
    ("clarified_at", "TEXT"),
)


_INSERT_COLUMNS: tuple[str, ...] = (
    "review_id",
    "job_url",
    "apply_url",
    "company",
    "role_title",
    "ats",
    "filled_at",
    "screenshot_path",
    "trace_path",
    "first_sent_at",
    "last_repinged_at",
    "repings_sent",
    "gmail_thread_id",
    "resolution",
    "resolved_at",
    "resume_path",
    "cover_letter_path",
    "applicant",
    "clarified_at",
)


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class ReviewStore:
    """Thin CRUD wrapper over the ``review_pending`` SQLite table.

    One persistent connection per instance; call ``close()`` when done
    (fixture teardown in tests, process-shutdown hook in production).
    """

    def __init__(self, db_path: str | Path):
        # ``sqlite3.connect`` accepts ":memory:" as-is; Path gets str-ified.
        self.db_path = str(db_path) if not isinstance(db_path, str) else db_path
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # ── lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            # Idempotent close — never raise from teardown.
            pass

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_REVIEW_PENDING)
            # Idempotent add of the H4/M1/M12 additive columns for DBs that
            # were created by an earlier CREATE (pre-Phase 1). Each ALTER
            # raises on duplicate — we catch and continue.
            for col, sqltype in _ADDITIVE_COLUMNS:
                try:
                    self._conn.execute(
                        f"ALTER TABLE review_pending ADD COLUMN {col} {sqltype}"
                    )
                except sqlite3.OperationalError:
                    # Column already exists — expected on Phase 1 fresh CREATEs.
                    pass

    # ── CRUD ───────────────────────────────────────────────────────

    def insert(self, row: dict) -> None:
        """Insert a fully-populated row. Missing columns default to NULL
        (except ``repings_sent`` which defaults to 0 via the schema)."""
        cols = list(_INSERT_COLUMNS)
        values = [row.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        col_list = ", ".join(cols)
        with self._conn:
            self._conn.execute(
                f"INSERT INTO review_pending ({col_list}) VALUES ({placeholders})",
                values,
            )

    def get(self, review_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM review_pending WHERE review_id = ?",
            (review_id,),
        )
        return _row_to_dict(cur.fetchone())

    def by_thread(self, thread_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM review_pending WHERE gmail_thread_id = ?",
            (thread_id,),
        )
        return _row_to_dict(cur.fetchone())

    def list_open(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM review_pending WHERE resolution IS NULL"
        )
        return [dict(r) for r in cur.fetchall()]

    def mark_repinged(self, review_id: str, at: str) -> None:
        """Atomically bump ``repings_sent`` and set ``last_repinged_at``."""
        with self._conn:
            self._conn.execute(
                "UPDATE review_pending "
                "SET last_repinged_at = ?, repings_sent = repings_sent + 1 "
                "WHERE review_id = ?",
                (at, review_id),
            )

    def mark_resolved(self, review_id: str, resolution: str, at: str) -> None:
        """Set ``resolution`` and ``resolved_at`` in a single UPDATE."""
        with self._conn:
            self._conn.execute(
                "UPDATE review_pending "
                "SET resolution = ?, resolved_at = ? "
                "WHERE review_id = ?",
                (resolution, at, review_id),
            )

    def set_thread_id(self, review_id: str, thread_id: str) -> None:
        """Post-insert helper: attach the Gmail thread id after ``send_with_labels``."""
        with self._conn:
            self._conn.execute(
                "UPDATE review_pending SET gmail_thread_id = ? WHERE review_id = ?",
                (thread_id, review_id),
            )

    def mark_clarified(self, review_id: str, at: str) -> None:
        """M12: record that we've sent a clarification reply on this thread so
        the next poll tick can skip the resend. Idempotent by design: the
        `clarified_at` column is a bare timestamp — the guard in review.py's
        AMBIGUOUS branch checks for non-NULL and short-circuits.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE review_pending SET clarified_at = ? WHERE review_id = ?",
                (at, review_id),
            )
