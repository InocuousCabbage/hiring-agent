"""src/apply/dedup.py — S5 SQLite-backed dedup DB.

Owns:
    * `DedupDB` class — hard-dedup + soft-warn + rate-limit surface.
    * `AlreadyAppliedError` — raised on hard-dup insert.
    * `normalize_company` / `normalize_role` — pure normalizers (importable by S8/S12).
    * CLI: `python -m src.apply.dedup --unblock <job_url>`.

Note: the ``review_pending`` table is CREATED by the 001 migration in this
module, but its Python-side CRUD lives in ``src/apply/state_store.ReviewStore``
— a single write-path avoids two drifting layers over one table (L3 audit).

Consumed by S8 (adapters), S12 (review loop), S14 (digest), S17 (seam-wiring).
See master-plan §4.6 (schema), §4.7 (config keys), §12 success criteria #4-5.

Design contracts:
    * All datetime writes are ISO-8601 UTC with a `+00:00` suffix. The naive
      (deprecated) UTC-now API is NEVER used (L6 landmine). Every timestamp
      goes through the module-level `_utcnow()` helper so tests can monkeypatch
      it.
    * Every SQL statement is parameterized — no f-string user data.
    * Connections use the `_connect()` helper. SQLite's `Connection.__exit__`
      commits/rolls back but does NOT close the connection — so `_connect()`
      wraps `sqlite3.connect(...)` in `contextlib.closing()` and each caller
      opens an inner `with conn:` block for the transaction. This guarantees
      both the transaction commit/rollback AND the FD close on every path.
    * The hard-dedup surface is enforced by a UNIQUE index on
      (company, ats_domain, ats_job_id). SQLite's default conflict resolution
      is ABORT, which raises `sqlite3.IntegrityError`; `record()` catches that
      and re-raises `AlreadyAppliedError`.
    * `count_today` computes the UTC-midnight boundary via `_utcnow()`.
    * The CLI resolves the DB path from `HIRING_AGENT_DEDUP_DB` env, or a
      `--db-path` flag, or the default `state/applied_jobs.db`. Relative
      values from any source anchor at REPO ROOT (never CWD) — see
      `_anchor_at_repo_root` for the split-brain-guard rationale.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    # ApplyResult lives in S2's `src/apply/types.py` which is NOT in this branch.
    # Kept as a forward-reference only — record() duck-types on `.status`,
    # `.ats`, `.apply_url`, `.application_id`, `.confirmation_screenshot`,
    # `.trace_path`, `.review_id`, `.submitted_at`.
    from .types import ApplyResult  # noqa: F401


# ── Exceptions ───────────────────────────────────────────────────────────────


class AlreadyAppliedError(Exception):
    """Raised by ``DedupDB.record`` when the HARD (company, ats_domain,
    ats_job_id) triple is already present. The caller should treat this
    as a signal to short-circuit the apply pipeline."""


# ── Time source (monkeypatch-friendly) ───────────────────────────────────────


def _utcnow() -> datetime:
    """Timezone-aware UTC now. All datetime writes go through this helper so
    tests can freeze time via `monkeypatch.setattr(dedup, '_utcnow', ...)`.

    L6 landmine discipline: this module never calls the deprecated naive UTC-now
    API; it always uses `datetime.now(timezone.utc)`.
    """
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    """ISO-8601 UTC string with `+00:00` suffix. Match the regex asserted in
    ``test_all_datetimes_are_utc_iso``."""
    return _utcnow().isoformat()


# ── Statuses that write vs. skip on record() ─────────────────────────────────


# From acceptance-criterion #6:
#   write on:  submitted, review_required, soft_dup_warn, auto_declined, captcha_escalated
#   skip on:   skipped, failed, already_applied
_STATUS_WRITE = frozenset({
    "submitted",
    "review_required",
    "soft_dup_warn",
    "auto_declined",
    "captcha_escalated",
})


# ── Normalizers ──────────────────────────────────────────────────────────────


# Legal suffixes stripped from company names (see acceptance-criterion #9).
_LEGAL_SUFFIXES = frozenset({
    "inc",
    "llc",
    "corp",
    "ltd",
    "gmbh",
    "co",
    "company",
})

# Seniority prefixes stripped from role titles.
_SENIORITY_PREFIXES = frozenset({
    "sr",
    "senior",
    "jr",
    "junior",
    "staff",
    "principal",
    "lead",
})

# Punctuation stripped before tokenizing. Kept as a compiled pattern for reuse.
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_company(s: str) -> str:
    """Lowercase, strip punctuation, drop trailing legal suffixes.

    Deterministic on any input (no locale dependency, no dict order surprises).
    """
    if s is None:
        return ""
    s = _PUNCT_RE.sub(" ", s.lower())
    tokens = s.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_role(s: str) -> str:
    """Lowercase, strip punctuation, drop leading seniority prefixes.

    Deterministic on any input.
    """
    if s is None:
        return ""
    s = _PUNCT_RE.sub(" ", s.lower())
    tokens = s.split()
    while tokens and tokens[0] in _SENIORITY_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)


# ── ATS extraction helpers ───────────────────────────────────────────────────


def _extract_ats_domain(apply_url: str | None) -> Optional[str]:
    """Return the host from an apply URL, or None if the URL is falsy/unparseable."""
    if not apply_url:
        return None
    try:
        host = urlparse(apply_url).netloc
    except (ValueError, AttributeError):
        return None
    return host or None


# Match Greenhouse (`.../jobs/12345`), Lever (`.../beta/abcdef`), Ashby
# (`.../company/abcd-ef`) — take the tail path segment as the job id.
_JOB_ID_RE = re.compile(r"/([^/?#]+)/?(?:[?#].*)?$")

# Suffix segments that are NOT the job id — they're form/apply pages appended
# to the real job id path. When we see these as the tail, fall back to the
# preceding path segment. Greenhouse uses `/application`; Lever uses `/apply`.
_JOB_ID_TAIL_SUFFIXES = frozenset({"application", "apply"})


def _extract_ats_job_id(apply_url: str | None) -> Optional[str]:
    """Return the last path segment as the ATS job id, or None."""
    if not apply_url:
        return None
    try:
        path = urlparse(apply_url).path
    except (ValueError, AttributeError):
        return None
    m = _JOB_ID_RE.search(path)
    if not m:
        return None
    seg = m.group(1)
    # Strip trailing "/application" (Greenhouse) or "/apply" (Lever) suffixes.
    if seg in _JOB_ID_TAIL_SUFFIXES:
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return parts[-2]
        return None
    return seg or None


# ── Repo root anchor (CWD split-brain guard) ─────────────────────────────────


# Repo root computed from this file's location: src/apply/dedup.py -> parents[2].
# Anchoring dedup DB fallback paths here (instead of CWD) prevents split-brain
# DBs when the pipeline is invoked from different working directories (e.g. a
# cron run with a different CWD vs. a manual repo-root invocation).
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Schema loader ────────────────────────────────────────────────────────────


_MIGRATION_SQL_PATH = Path(__file__).parent / "migrations" / "001_init.sql"
# Phase 1 (H4/M1/M12) additive columns. Kept as a separate migration file so
# `test_h1_schema_reconciliation.py`'s byte-shape of 001 remains stable.
_MIGRATION_002_SQL_PATH = Path(__file__).parent / "migrations" / "002_review_pending_paths.sql"
# Phase 2 (H9) normalized hard-dedup index. Drops raw company from the
# UNIQUE key so spelling variance ('Acme' vs 'Acme, Inc.') at the same
# (ats_domain, ats_job_id) posting can no longer slip through the hard
# gate. Kept as a separate migration so 001's byte-shape stays stable.
_MIGRATION_003_SQL_PATH = Path(__file__).parent / "migrations" / "003_normalized_hard_dedup.sql"


def _strip_sql_comments(raw: str) -> str:
    """Strip `--` line comments from a SQL script so per-statement parsing
    can safely split on `;` without a leading comment block masquerading as
    part of the first statement (SD1 audit fix).

    Only handles line comments — the migrations don't use `/* */` block
    comments, and this stays a simple filter (no need for a full SQL parser).
    """
    kept: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        kept.append(line)
    return "\n".join(kept)


def _execute_migrations(conn: sqlite3.Connection) -> None:
    """Run 001 as a script (idempotent CREATE IF NOT EXISTS), then run each
    ADD COLUMN statement from 002 individually so we can swallow the
    duplicate-column OperationalError that sqlite raises on ALREADY-added
    columns, then run 003 statement-by-statement (H9 hard-dedup index swap).
    Keeps the migration idempotent across cold + warm starts.
    """
    conn.executescript(_MIGRATION_SQL_PATH.read_text(encoding="utf-8"))
    if _MIGRATION_002_SQL_PATH.exists():
        raw = _MIGRATION_002_SQL_PATH.read_text(encoding="utf-8")
        # SD1 fix: strip `--` comments BEFORE splitting on `;`. The pre-fix
        # code split first and used `stmt.startswith("--")` as a skip-guard —
        # but the split delivered a chunk that contained the file's header
        # comments PLUS the first ALTER, whose first character was `-`. That
        # skipped `ADD COLUMN resume_path` entirely; only warm starts saw the
        # column (because ReviewStore.__init__ also ALTERed via its own path).
        stripped = _strip_sql_comments(raw)
        for stmt in (s.strip() for s in stripped.split(";")):
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                # Duplicate column on a warm start — expected + safe. Any
                # other OperationalError (locked DB, no such table, disk I/O,
                # malformed DB) is a real fault we DO want to see: re-raise
                # so the caller doesn't silently continue against a partially-
                # migrated schema.
                msg = str(exc).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    continue
                raise

    if not _MIGRATION_003_SQL_PATH.exists():
        return
    # 003 is H9: swap the HARD UNIQUE index from (company, ats_domain,
    # ats_job_id) to (ats_domain, ats_job_id). Statement-by-statement so
    # idempotent DDL (CREATE ... IF NOT EXISTS re-run, DROP ... IF EXISTS on
    # a fresh DB) is safe on both cold and warm starts. The DELETE step is a
    # one-shot cleanup — on a warm start it's effectively a no-op because
    # the new UNIQUE index (ux_applied_jobs_hard_v2) already prevents any
    # further (ats_domain, ats_job_id) collisions.
    raw_003 = _MIGRATION_003_SQL_PATH.read_text(encoding="utf-8")
    stripped_003 = _strip_sql_comments(raw_003)
    for stmt in (s.strip() for s in stripped_003.split(";")):
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # Idempotent-DDL guard: index already exists, no such index (DROP
            # on a fresh DB where old index never existed), or duplicate
            # column. Anything else is a real fault — re-raise so we don't
            # limp along with a broken schema.
            msg = str(exc).lower()
            if (
                "already exists" in msg
                or "no such index" in msg
                or "duplicate column" in msg
            ):
                continue
            raise


# ── DedupDB ──────────────────────────────────────────────────────────────────


class DedupDB:
    """SQLite-backed dedup + rate-limit surface.

    Owns hard-dedup (``applied_jobs`` UNIQUE index), soft-warn, and per-ATS
    rate-limit counts. The ``review_pending`` table lives in the same DB file
    but is written through ``src/apply/state_store.ReviewStore`` (L3 audit:
    two write paths for one table is a landmine).

    The class is intentionally small: each method opens its own connection via
    `with self._connect() as conn, conn:`. If the S8 fill loop shows this hot,
    switch to a class-level connection with `threading.Lock` — see the
    REFACTOR opportunities in §S5 spec.

    Never blocks the pipeline. Hard hits return ``already_applied`` at the
    caller layer; soft hits route to review with a warning surface. All
    exceptions are surfaced with SQL context so the caller can decide.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

        # Parent dir @ 0o700 (owner-only). We set the mode explicitly after
        # mkdir because `os.makedirs(mode=...)` respects the process umask.
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            # Don't hard-fail on filesystems that reject chmod (e.g. tmpfs on
            # some CI runners). The parent-dir chmod is a defense-in-depth
            # measure; the DB file chmod below is what actually matters.
            pass

        pre_existed = self.path.exists()

        # First connect creates the file. Guarantee close on all paths via
        # `_connect()` (`contextlib.closing` around sqlite3.connect).
        with self._connect() as conn, conn:
            # `_execute_migrations` runs 001 (idempotent CREATE IF NOT EXISTS)
            # then applies each 002 ALTER individually, swallowing
            # duplicate-column OperationalErrors so warm starts are safe.
            _execute_migrations(conn)

        if not pre_existed:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                # Same rationale as parent chmod — don't crash on quirky FS.
                pass

    def _connect(self) -> "contextlib.closing[sqlite3.Connection]":
        """Return a ``contextlib.closing`` wrapper around ``sqlite3.connect``.

        Python's SQLite ``Connection.__exit__`` commits or rolls back but does
        NOT close the connection. Callers should nest an inner ``with conn:``
        for the transaction, i.e. ``with self._connect() as conn, conn: ...``.
        This guarantees the FD is closed on every path.
        """
        return contextlib.closing(sqlite3.connect(str(self.path)))

    # ── read paths ──

    def was_applied(
        self,
        company: str,
        ats_domain: str | None,
        ats_job_id: str | None,
        job_url: str,
    ) -> bool:
        """True iff a prior row matches the HARD posting identity
        ``(ats_domain, ats_job_id)``, OR (fallback) the exact ``job_url`` when
        either part of the pair is None. Never raises.

        H9: the raw ``company`` argument is kept in the signature for
        backward-compat (all in-tree callers still pass it) but is NOT part
        of the primary predicate. The ATS posting identity is
        ``(ats_domain, ats_job_id)``; raw-company equality only weakens the
        key, letting spelling variance ('Acme' vs 'Acme, Inc.') at the same
        posting slip through the gate. The 003 UNIQUE index
        ``ux_applied_jobs_hard_v2`` enforces the same shape at the write
        site.
        """
        try:
            with self._connect() as conn, conn:
                if ats_domain and ats_job_id:
                    cur = conn.execute(
                        "SELECT 1 FROM applied_jobs "
                        "WHERE ats_domain = ? AND ats_job_id = ? "
                        "LIMIT 1",
                        (ats_domain, ats_job_id),
                    )
                else:
                    cur = conn.execute(
                        "SELECT 1 FROM applied_jobs WHERE job_url = ? LIMIT 1",
                        (job_url,),
                    )
                return cur.fetchone() is not None
        except sqlite3.Error:
            # Contract: never raises. If the DB is unavailable, treat as
            # "not applied" — the caller will hit a duplicate check downstream
            # via the UNIQUE index if the file becomes healthy again.
            return False

    def soft_warn_check(
        self,
        company_normalized: str,
        role_title_normalized: str,
    ) -> list[dict]:
        """Return prior applies matching the normalized (company, role) pair,
        ordered by ``applied_at`` DESC. Empty list on no match."""
        with self._connect() as conn, conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM applied_jobs "
                "WHERE company_normalized = ? AND role_title_normalized = ? "
                "ORDER BY applied_at DESC",
                (company_normalized, role_title_normalized),
            )
            return [dict(row) for row in cur.fetchall()]

    def count_today(self, ats_domain: str) -> int:
        """Rows in ``applied_jobs`` for ``ats_domain`` since UTC midnight today.

        L6: uses ``_utcnow()`` for the boundary (timezone-aware).
        """
        midnight = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as conn, conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM applied_jobs "
                "WHERE ats_domain = ? AND applied_at >= ?",
                (ats_domain, midnight.isoformat()),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ── write paths ──

    def record(
        self,
        result: "ApplyResult",
        applicant: str,
        company: str,
        role_title: str,
        job_url: str,
    ) -> None:
        """Insert one row into ``applied_jobs`` when ``result.status`` is a
        write-worthy status (see ``_STATUS_WRITE``).

        Raises:
            AlreadyAppliedError: when the HARD triple already exists (SQLite
                raises ``sqlite3.IntegrityError`` from the UNIQUE index; we
                catch and re-raise so the caller doesn't have to know about
                SQL error types).
        """
        status = getattr(result, "status", None)
        if status not in _STATUS_WRITE:
            return

        ats = getattr(result, "ats", None) or ""
        apply_url = getattr(result, "apply_url", None) or ""
        application_id = getattr(result, "application_id", None)
        confirmation_screenshot = getattr(result, "confirmation_screenshot", None)
        trace_path = getattr(result, "trace_path", None)
        review_id = getattr(result, "review_id", None)
        submitted_at = getattr(result, "submitted_at", None)

        ats_domain = _extract_ats_domain(apply_url)
        ats_job_id = _extract_ats_job_id(apply_url)

        # B2 fix: coerce Path values to str before binding. ``ApplyResult``
        # types these as ``Path | None``; sqlite3 raises
        # ``ProgrammingError: type PosixPath is not supported`` at bind time
        # (parameters 14 + 15). Idempotent when the caller already passes a
        # str; ``None`` and falsy values collapse to ``None``.
        confirmation_screenshot_str = (
            str(confirmation_screenshot) if confirmation_screenshot else None
        )
        trace_path_str = str(trace_path) if trace_path else None

        row = (
            applicant,
            company,
            normalize_company(company),
            role_title,
            normalize_role(role_title),
            ats,
            ats_domain,
            ats_job_id,
            job_url,
            apply_url,
            application_id,
            status,
            review_id,
            confirmation_screenshot_str,
            trace_path_str,
            _utcnow_iso(),
            submitted_at,
        )

        try:
            with self._connect() as conn, conn:
                conn.execute(
                    "INSERT INTO applied_jobs ("
                    "applicant, company, company_normalized, "
                    "role_title, role_title_normalized, "
                    "ats, ats_domain, ats_job_id, "
                    "job_url, apply_url, application_id, "
                    "status, review_id, "
                    "confirmation_screenshot, trace_path, "
                    "applied_at, submitted_at"
                    ") VALUES ("
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                    ")",
                    row,
                )
        except sqlite3.IntegrityError as exc:
            raise AlreadyAppliedError(
                f"already applied: company={company!r} "
                f"ats_domain={ats_domain!r} ats_job_id={ats_job_id!r}"
            ) from exc

    def unblock(self, job_url: str) -> int:
        """Delete every ``applied_jobs`` row matching ``job_url``; return the
        deletion count. Safe when no row matches (returns 0)."""
        with self._connect() as conn, conn:
            cur = conn.execute(
                "DELETE FROM applied_jobs WHERE job_url = ?",
                (job_url,),
            )
            return int(cur.rowcount)

# ── CLI: `python -m src.apply.dedup --unblock <job_url>` ─────────────────────


_DEFAULT_DB_RELATIVE = Path("state") / "applied_jobs.db"


def _is_sqlite_special_path(raw: str) -> bool:
    """True if ``raw`` is a SQLite non-filesystem path spec that must pass
    through unchanged: ``":memory:"`` (in-memory DB) or a ``file:...`` URI
    (see sqlite3.connect docs). Anchoring these at repo root would break
    SQLite's special-path handling — e.g. turn ``":memory:"`` into a real
    file literally named ``:memory:`` under the repo.
    """
    return raw == ":memory:" or raw.startswith("file:")


def _anchor_at_repo_root(raw: str | os.PathLike[str]) -> Path:
    """Return an absolute Path for ``raw``, anchoring relative values at repo
    root (never CWD). Absolute inputs pass through unchanged. Home-relative
    inputs (``~/...``) are expanded via ``Path.expanduser``.

    Bug guard: naive ``Path("state/applied_jobs.db")`` is CWD-relative — the
    same repo invoked from two working directories would create TWO separate
    SQLite DBs, causing split-brain dedup state and silent double-applies.
    Anchoring at repo root ensures every invocation sees the same DB.

    SQLite special paths (``":memory:"``, ``"file:..."``) pass through — see
    ``_is_sqlite_special_path``.
    """
    raw_str = os.fspath(raw)
    if _is_sqlite_special_path(raw_str):
        # Return a bare Path around the spec — SQLite consumes this via
        # ``sqlite3.connect(str(path))`` which preserves the special spec.
        return Path(raw_str)
    p = Path(raw_str).expanduser()
    if p.is_absolute():
        return p
    return _REPO_ROOT / p


def _resolve_db_path(config: dict) -> Path:
    """Resolve the dedup DB path from ``config``, falling back to a
    repo-root-anchored ``state/applied_jobs.db``.

    Accepts either the WRAPPED shape ``{"apply": {"dedup_db_path": ...}}`` (as
    used by ``src/apply/review.py`` and ``src/main.py``) or the UNWRAPPED apply
    block ``{"dedup_db_path": ...}`` (as used by ``src/apply/_seam.py``). This
    covers all in-tree callers without forcing them to reshape config first.

    Relative values — whether the config default or a user override — anchor
    at repo root, NEVER at CWD. See ``_anchor_at_repo_root`` for rationale.
    """
    apply_cfg: dict | None
    inner = config.get("apply") if isinstance(config, dict) else None
    if isinstance(inner, dict):
        apply_cfg = inner
    elif isinstance(config, dict):
        apply_cfg = config  # already the unwrapped apply block
    else:
        apply_cfg = None
    raw = None
    if isinstance(apply_cfg, dict):
        raw = apply_cfg.get("dedup_db_path")
    if not raw:
        raw = str(_DEFAULT_DB_RELATIVE)
    return _anchor_at_repo_root(raw)


def _resolve_cli_db_path(cli_override: str | None) -> Path:
    """CLI path resolution order: ``--db-path`` -> env ``HIRING_AGENT_DEDUP_DB``
    -> default ``state/applied_jobs.db`` anchored at repo root.

    Relative paths from any source anchor at repo root (never CWD) — same
    split-brain guard as ``_resolve_db_path``. See ``_anchor_at_repo_root``.
    """
    if cli_override:
        return _anchor_at_repo_root(cli_override)
    env = os.environ.get("HIRING_AGENT_DEDUP_DB")
    if env:
        return _anchor_at_repo_root(env)
    return _anchor_at_repo_root(_DEFAULT_DB_RELATIVE)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.apply.dedup",
        description="Dedup DB operator escape hatch. Currently: --unblock only.",
    )
    p.add_argument(
        "--unblock",
        metavar="JOB_URL",
        required=True,
        help="Remove every applied_jobs row for JOB_URL so a future apply can retry.",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help=(
            "Override the DB path. Default: HIRING_AGENT_DEDUP_DB env var, "
            "else ./state/applied_jobs.db"
        ),
    )
    return p


def _main(argv: Iterable[str] | None = None) -> int:
    parser = _build_parser()
    # argparse already exits 2 on a missing --unblock via SystemExit(2); we
    # don't intercept that.
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path = _resolve_cli_db_path(args.db_path)
    db = DedupDB(db_path)
    n = db.unblock(args.unblock)
    print(f"unblocked {n} row(s) for {args.unblock}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
