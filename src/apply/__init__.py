"""`src.apply` — auto-apply pipeline package (Phase 3 MVP).

Only the frozen cross-shard contracts are re-exported here. Everything else
(dedup, adapters, review-loop, retries) is accessed via its submodule so
downstream shards can't accidentally couple to unstable internals.

Cluster 3 exports: types + base + FieldFill (adapters landed).
Later cluster adds dispatcher.
"""

from src.apply.adapters._labels import FieldFill
from src.apply.base import AdapterNotFoundError, ATSAdapter, SessionExpiredError
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
]
