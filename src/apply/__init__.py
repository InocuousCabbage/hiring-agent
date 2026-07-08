"""`src.apply` — auto-apply pipeline package (Phase 3 MVP).

Only the frozen cross-shard contracts are re-exported here. Everything else
(dedup, adapters, review-loop, retries) is accessed via its submodule so
downstream shards can't accidentally couple to unstable internals.

FieldFill placement (S17 merge-time reconciliation):
    Canonical location per S8 spec §File-ownership is
    `src.apply.adapters._labels.FieldFill`. S2's earlier `types.py` shim has
    been retired at merge; the S8 shape is authoritative and re-exported here
    so `from src.apply import FieldFill` continues to work.

    FieldFill import is DEFENSIVE (try/except) so dispatcher-tests that stub
    `sys.modules['src.apply.adapters']` with an empty ModuleType don't crash
    __init__.py during re-execution.
"""

try:  # pragma: no cover — defensive; only fails when adapters/ is stubbed by a test
    from src.apply.adapters._labels import FieldFill
except ModuleNotFoundError:  # pragma: no cover
    FieldFill = None  # type: ignore[assignment]

from src.apply.base import AdapterNotFoundError, ATSAdapter, SessionExpiredError
from src.apply.dispatcher import apply_to_job, dispatch
from src.apply.types import (
    ApplyContext,
    ApplyEvent,
    ApplyEventKind,
    ApplyResult,
    SessionContext,
    Status,
)

__all__ = [
    "AdapterNotFoundError",
    "ApplyContext",
    "ApplyEvent",
    "ApplyEventKind",
    "ApplyResult",
    "ATSAdapter",
    "FieldFill",
    "SessionContext",
    "SessionExpiredError",
    "Status",
    "apply_to_job",
    "dispatch",
]
