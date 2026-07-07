"""S2 shard — cross-shard dataclasses + `Status` Literal.

FROZEN CONTRACTS (do not widen/narrow without an amended S2 spec):
- `Status` — 8-value Literal from master-plan §4.1.
- `ApplyResult` — frozen dataclass, all fields except `status` default None.
- `SessionContext` — local vs browserbase discriminator (§4.3).
- `ApplyContext` — per-job context passed dispatcher → adapter → transport.
- `ApplyEvent` (S14) + `ApplyEventKind` — thin (kind, row) event handed to the
  digest by the review poller; see `src/gmail/digest.py`.

NOTE (S17 merge-time reconciliation): `FieldFill` used to live here in S2 as a
compat shim. Per spec §File-ownership the canonical location is
`src.apply.adapters._labels.FieldFill`. The S8 shape (selector, strategy,
value, label, required, source) is authoritative — S2's leaner 4-field shape
has been retired. `src.apply.FieldFill` continues to work via re-export.

Consumers: S3 (config validator), S5 (dedup writes), S8 (Greenhouse adapter),
S10 (transport swap), S12 (review_id), S13 (fast-path email), S14 (digest),
S17 (main.py seam), S18 (fixtures), S20 (Computer Use adapter).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # Import only for type-checking to avoid coupling runtime import order
    # between types.py and profile.py's future S1 impl.
    from src.apply.profile import CandidateProfile


# ---------------------------------------------------------------------------
# Status Literal (master-plan §4.1) — exactly 8 values, order matters for
# `typing.get_args` equality checks in tests + downstream tooling.
# ---------------------------------------------------------------------------

Status = Literal[
    "submitted",           # DOM-verified confirmation seen
    "review_required",     # filled, awaiting Gmail YES/NO
    "skipped",             # dedup / rate-limit / session-expired / adapter-mismatch
    "failed",              # navigation/upload/submit error surfaced to operator
    "already_applied",     # dedup DB primary-key hit
    "soft_dup_warn",       # soft-warn hit; still routes to review with warning
    "captcha_escalated",   # CAPTCHA detected; fast-path email fired
    "auto_declined",       # 72h passed with no reply
]


# ---------------------------------------------------------------------------
# ApplyResult (master-plan §4.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The value every adapter and the dispatcher returns.

    Only `status` is required; every other field defaults to None so callers
    can pass through partial results (e.g. a `skipped` result carries only a
    `reason`, an `already_applied` result carries only `ats` + `apply_url`).
    """

    status: Status
    ats: str | None = None
    apply_url: str | None = None
    application_id: str | None = None
    confirmation_screenshot: Path | None = None
    reason: str | None = None
    human_review_url: str | None = None      # Browserbase replay_url or local resumable
    submitted_at: str | None = None          # ISO8601 UTC — never `datetime.utcnow()` (L6)
    trace_path: Path | None = None           # local Playwright trace zip
    review_id: str | None = None             # uuid7 for the review row (if staged)


# ---------------------------------------------------------------------------
# SessionContext (master-plan §4.3) — Local vs Browserbase discriminator
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SessionContext:
    """Discriminator between S4's LocalTransport and S10's BrowserbaseTransport.

    S10 populates `replay_url` on Browserbase sessions; S4 populates
    `trace_path` on local sessions. `proxies_enabled` + `solve_captchas`
    default True on Browserbase (Q_BB2 + Q_BB3).
    """

    transport: Literal["local", "browserbase"]
    replay_url: str | None
    trace_path: Path | None
    proxies_enabled: bool
    solve_captchas: bool


# ---------------------------------------------------------------------------
# ApplyContext — per-job context passed through dispatcher → adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ApplyContext:
    """Everything an adapter needs to fill and (optionally) submit an application.

    `profile` embeds the S1 `CandidateProfile`. `mode` mirrors `apply.mode`
    but is snapshotted here so a config-mutation mid-run cannot flip an
    in-flight adapter from review to auto.
    """

    profile: "CandidateProfile"
    job: dict                                # raw scraper output (job_url, title, company, etc.)
    resume_path: Path | None                 # AUDIT WIDEN: dual-output renderer may return None
    cover_letter_path: Path | None
    config: dict
    applicant: str                           # single-user v1 per Q7; multi-user forward-compat
    dry_run: bool                            # if True, adapter fills + screenshots but never clicks submit
    mode: Literal["review", "auto"]
    resume_docx_path: Path | None = None     # AUDIT ADD: DOCX fallback for docx-only lane
    cover_letter_docx_path: Path | None = None  # AUDIT ADD: DOCX fallback for docx-only lane


# ---------------------------------------------------------------------------
# ApplyEvent (S14) — payload handed to `compose_digest(apply_events=...)`
# ---------------------------------------------------------------------------

ApplyEventKind = Literal[
    "submitted",
    "review_required",
    "auto_declined",
    "soft_dup",
    "bootstrap_needed",
]


@dataclass(frozen=True, slots=True)
class ApplyEvent:
    """Structured event emitted by S12's review poller for the daily digest.

    Keeping the payload as a plain ``dict`` (rather than a tighter per-kind
    dataclass) preserves S14's decoupling from S12's ``ReviewStore`` shape —
    the digest reads the fields it needs and ignores the rest.
    """

    kind: ApplyEventKind
    row: dict
