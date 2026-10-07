"""Allowed-employer list for the fabricated-company QA check.

Any company named in a resume bullet that is not on this list is treated as
a possible fabrication. The real list is personal, so it lives in the
gitignored config/settings.local.yaml (the same file that holds
user.company, see src/utils/user_company.py) under a top-level key:

    employers:
      - First Employer Name
      - Second Employer Name

user.company is added to the set when present.

Fail-loud semantics, same as load_user_company: a missing file, a missing
key, an empty list, or the example placeholder all raise EmployerListError.
An empty allow-list must never silently mean "allow everything" (or, just as
bad, "flag every real employer").

Loading happens on first use and is cached. Tests inject a list with
set_allowed_employers() and clear it with set_allowed_employers(None).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_REL = Path("config") / "settings.local.yaml"
_EXAMPLE_PREFIX = "YOUR_EMPLOYER"
_USER_COMPANY_PLACEHOLDER = "YOUR_COMPANY_NAME_HERE"

_cache: Optional[frozenset[str]] = None


class EmployerListError(RuntimeError):
    """The allowed-employer list is missing, empty, or still a placeholder."""


def _normalise(names: Iterable[object], where: str) -> frozenset[str]:
    cleaned = set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise EmployerListError(f"{where}: every employers entry must be a non-empty string, got {name!r}")
        if name.strip().upper().startswith(_EXAMPLE_PREFIX):
            raise EmployerListError(
                f"{where}: employers still contains the example placeholder {name.strip()!r}. "
                f"Replace it with a real employer name."
            )
        cleaned.add(name.strip().lower())
    if not cleaned:
        raise EmployerListError(
            f"{where}: the employers list is empty. Add every employer you have "
            f"actually worked at (see config/settings.local.example.yaml)."
        )
    return frozenset(cleaned)


def load_allowed_employers(repo_root: Path = _REPO_ROOT) -> frozenset[str]:
    """Read `employers:` (plus user.company) from config/settings.local.yaml.

    Returns lowercased names. Raises EmployerListError on a missing file,
    invalid YAML, a missing or empty `employers` key, or a placeholder entry.
    """
    path = repo_root / _SETTINGS_REL
    where = f"{_SETTINGS_REL} key 'employers'"
    if not path.is_file():
        raise EmployerListError(
            f"No {_SETTINGS_REL} found, so {where} cannot be read. Copy "
            f"config/settings.local.example.yaml to {_SETTINGS_REL} and fill in "
            f"the employers list."
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise EmployerListError(f"{_SETTINGS_REL} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise EmployerListError(f"{_SETTINGS_REL} must be a mapping with an 'employers' list")

    employers = raw.get("employers")
    if employers is None:
        raise EmployerListError(
            f"{where} is missing. Add a top-level 'employers:' list naming every "
            f"employer you have actually worked at."
        )
    if not isinstance(employers, list):
        raise EmployerListError(f"{where} must be a list of strings, got {type(employers).__name__}")

    allowed = _normalise(employers, where)
    user = raw.get("user")
    company = user.get("company") if isinstance(user, dict) else None
    if isinstance(company, str) and company.strip() and company.strip() != _USER_COMPANY_PLACEHOLDER:
        allowed = allowed | {company.strip().lower()}
    return allowed


def set_allowed_employers(names: Optional[Iterable[str]]) -> None:
    """Inject the allowed list (tests, callers with their own config).

    None clears the cache so the next get_allowed_employers() reloads from
    disk. An empty iterable raises, same as an empty list on disk.
    """
    global _cache
    _cache = None if names is None else _normalise(list(names), "injected employers")


def get_allowed_employers() -> frozenset[str]:
    """Return the cached allowed list, loading it on first use."""
    global _cache
    if _cache is None:
        _cache = load_allowed_employers()
    return _cache
