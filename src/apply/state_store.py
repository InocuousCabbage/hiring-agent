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

# NOTE: the review_pending schema is owned by ``src/apply/migrations/``
# (001_init.sql + 002_review_pending_paths.sql) — see ``_ensure_schema``
# below which delegates to ``dedup._execute_migrations`` so this module and
# DedupDB stay byte-consistent by construction (Phase 1 audit finding SG1/SG7:
# split-brain schemas cause hard-to-debug column-missing errors).

# xhigh-H5/H12 sentinel: distinguishes "no guard requested" from an
# explicit ``expected_resolution=None`` (which means "only overwrite from
# the NULL/open state"). Never leaked outside the module.
_UNSET: object = object()


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
    "initial_msg_id",
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
        # Match DedupDB's behavior: mkdir the parent so callers can pass a
        # nested path (e.g. ``tmp_path / "state" / "applied_jobs.db"``) even
        # when DedupDB hasn't been instantiated first. Skip for ``:memory:``.
        if self.db_path != ":memory:":
            parent = Path(self.db_path).parent
            if str(parent) and str(parent) != ".":
                parent.mkdir(parents=True, exist_ok=True)
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
        # SG1/SG7 fix: delegate to the canonical migration runner so this
        # module and DedupDB stay byte-consistent by construction. Applies
        # 001_init.sql (creates review_pending) + 002_review_pending_paths.sql
        # (H4/M1/M12 additive columns) + 003_review_pending_initial_msg_id.sql
        # (SE3 exact-msg-id self-filter anchor). Idempotent across cold + warm
        # starts — see ``dedup._execute_migrations`` for the duplicate-column
        # OperationalError handling.
        from src.apply.dedup import _execute_migrations
        with self._conn:
            _execute_migrations(self._conn)

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

    def mark_resolved(
        self,
        review_id: str,
        resolution: str,
        at: str,
        *,
        expected_resolution: "str | None | object" = _UNSET,
    ) -> bool:
        """Set ``resolution`` and ``resolved_at`` in a single UPDATE.

        Returns True iff exactly one row was updated (compare-and-swap
        semantics when ``expected_resolution`` is provided).

        xhigh-H5/H12: when ``expected_resolution`` is passed, the UPDATE only
        fires if the current ``resolution`` matches. This closes the
        clobber race for concurrent YES/NO on the same row and for stale
        ticks arriving after auto_decline. Semantics:

            expected_resolution=None   → only overwrite from NULL (open row)
            expected_resolution='X'    → only overwrite from resolution='X'
            expected_resolution=_UNSET → unguarded (backwards-compat)

        The unguarded default preserves the legacy write-last-wins behaviour
        so existing callers continue to work; new callers should pass an
        expected value.
        """
        with self._conn:
            if expected_resolution is _UNSET:
                cur = self._conn.execute(
                    "UPDATE review_pending "
                    "SET resolution = ?, resolved_at = ? "
                    "WHERE review_id = ?",
                    (resolution, at, review_id),
                )
            elif expected_resolution is None:
                cur = self._conn.execute(
                    "UPDATE review_pending "
                    "SET resolution = ?, resolved_at = ? "
                    "WHERE review_id = ? AND resolution IS NULL",
                    (resolution, at, review_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE review_pending "
                    "SET resolution = ?, resolved_at = ? "
                    "WHERE review_id = ? AND resolution = ?",
                    (resolution, at, review_id, expected_resolution),
                )
            return cur.rowcount == 1

    def mark_resolved_from_open(
        self, review_id: str, resolution: str, at: str
    ) -> bool:
        """xhigh-H5/H12 convenience: resolve iff the row is currently open
        (resolution IS NULL). Returns True on success. Callers on the NO
        branch use this to protect against overwriting a completed
        resolution set by a concurrent YES handler on the same row.
        """
        return self.mark_resolved(
            review_id, resolution, at, expected_resolution=None
        )

    def mark_resolved_from_claiming(
        self, review_id: str, resolution: str, at: str
    ) -> bool:
        """xhigh-H5/H12 convenience: resolve iff the row is currently in the
        interim 'claiming' state. Used by the YES branch after ``try_claim``
        succeeds — guarantees mark_resolved cannot clobber a row that has
        been reset (release_claim) or completed by another handler.
        """
        return self.mark_resolved(
            review_id, resolution, at, expected_resolution="claiming"
        )

    def try_claim(self, review_id: str, at: str) -> bool:
        """H10: atomically claim a review row for the YES-branch submit.

        Sets ``resolution='claiming'`` iff the row's current ``resolution``
        is NULL. Returns True on success (this caller won the race), False
        otherwise. Single UPDATE on a single connection — the row-change
        count is the authoritative signal, no check-then-act window between
        two SQLite transactions.

        xhigh-H4/MEDIUM: ``resolved_at`` is NOT written here. Pre-fix set
        ``resolved_at=at`` during the interim 'claiming' state which polluted
        compliance dashboards querying ``WHERE resolved_at IS NOT NULL``
        with mid-claim rows. ``resolved_at`` now only lands at the final
        ``mark_resolved_from_claiming`` call.

        The interim 'claiming' resolution is a placeholder that must be
        overwritten by ``mark_resolved_from_claiming`` on submit success, or
        cleared by ``release_claim`` on submit failure so a retry can
        happen next tick.

        The ``at`` parameter is retained for backward-compat with existing
        callers and future extension (e.g. a claim_at column) but is unused
        at present.
        """
        _ = at  # xhigh-H4: intentionally not written to resolved_at.
        with self._conn:
            cur = self._conn.execute(
                "UPDATE review_pending "
                "SET resolution = 'claiming' "
                "WHERE review_id = ? AND resolution IS NULL",
                (review_id,),
            )
            return cur.rowcount == 1

    def release_claim(self, review_id: str) -> None:
        """H10: release a 'claiming' interim claim so a retry can happen.

        Clears ``resolution`` back to NULL. Called from ``_handle_yes`` when
        the adapter re-run fails — without this the row would stay stuck in
        'claiming' forever and the operator would see a phantom half-
        resolved review row.

        xhigh-H4/MEDIUM: ``resolved_at`` is unconditionally reset to NULL
        even though ``try_claim`` no longer sets it — defense-in-depth so
        any legacy 'claiming' rows persisted by the pre-fix code cannot
        leak into compliance dashboards after this fix ships.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE review_pending "
                "SET resolution = NULL, resolved_at = NULL "
                "WHERE review_id = ? AND resolution = 'claiming'",
                (review_id,),
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
