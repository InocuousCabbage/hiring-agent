"""H1: state_store schema drifts with dedup migration.

RED test: state_store declares 15 columns (first_sent_at / repings_sent /
filled_at / resolved_at); migration creates 12 columns with DIFFERENT names
(has `created_at`, not `first_sent_at`). DedupDB migration runs first → state_store
writes crash with `no such column: first_sent_at` on first prod use.

Fix: DedupDB migration owns the CREATE TABLE with the FULL 15-column schema.
state_store just does CRUD against the migrated schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.apply.dedup import DedupDB
from src.apply.state_store import ReviewStore


def test_review_store_writes_row_matches_dedup_migration_schema(tmp_path: Path):
    """RED: create a fresh DB via DedupDB.migrate first, then use ReviewStore
    on the SAME DB. ReviewStore.insert should not raise a schema error.

    Before the fix: DedupDB creates a 12-col table (created_at, no repings_sent,
    filled_at, resolved_at, first_sent_at). ReviewStore.insert tries to write
    first_sent_at → sqlite3.OperationalError: no such column: first_sent_at.
    """
    db_path = tmp_path / "shared.db"

    # DedupDB migrates the schema FIRST — this is the production ordering.
    DedupDB(db_path)

    # ReviewStore opens the same DB. Its CREATE TABLE IF NOT EXISTS is a no-op
    # because DedupDB already created the review_pending table.
    store = ReviewStore(db_path)
    try:
        rid = "0195c5a0-1234-7abc-8def-999999999999"
        now = datetime.now(timezone.utc).isoformat()
        # This insert MUST NOT raise. Before the fix, it raises
        # sqlite3.OperationalError because the migrated table lacks
        # first_sent_at, repings_sent, filled_at, resolved_at.
        store.insert(
            {
                "review_id": rid,
                "job_url": "https://acme.com/jobs/1",
                "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                "company": "AcmeCorp",
                "role_title": "Senior Engineer",
                "ats": "greenhouse",
                "filled_at": now,
                "screenshot_path": "/tmp/ss.png",
                "trace_path": None,
                "first_sent_at": now,
                "last_repinged_at": None,
                "repings_sent": 0,
                "gmail_thread_id": "THREAD_777",
                "resolution": None,
                "resolved_at": None,
            }
        )
        # Round-trip: fetching by review_id returns the row we wrote.
        row = store.get(rid)
        assert row is not None
        assert row["review_id"] == rid
        assert row["first_sent_at"] == now
        assert row["filled_at"] == now
        assert row["repings_sent"] == 0
        assert row["resolved_at"] is None
    finally:
        store.close()


def test_dedup_migration_review_pending_has_all_state_store_columns(tmp_path: Path):
    """The migrated review_pending table must include EVERY column the
    state_store CRUD writes. If either side adds a column, this test tells us
    they've drifted again.
    """
    db_path = tmp_path / "shared.db"
    DedupDB(db_path)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("PRAGMA table_info(review_pending)")
        col_names = {row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    # Every column the state_store's insert writes must exist.
    required = {
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
    }
    missing = required - col_names
    assert not missing, f"migration schema missing columns state_store needs: {missing}"
