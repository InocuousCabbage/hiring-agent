"""CandidateProfile: immutable, YAML-loaded candidate schema for auto-apply.

Loaded once per apply run and passed into every ATS adapter (S2+). This module
is the leaf of the auto-apply DAG — no other apply-shard imports precede it.

Landmine notes:
- L6: no datetime.utcnow() (this module does not need datetime at all).
- L7: the loader emits structural log events ONLY. Field values NEVER touch
  a log record — not even in exception messages surfaced to the logger.
  Validation errors reference the offending KEY PATH (e.g. "contact.email"),
  not its value.

Spec: .agent/one-big-feature/auto-apply-2026-07-06/03-specs/01-s1-profile-loader.md
Contract (master-plan §4.4): CandidateProfile.load() -> frozen dataclass,
                             legacy_contact_string() returns the pipe-delimited
                             back-compat string consumed later by S17.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog
import yaml

log = structlog.get_logger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────────
class ProfileValidationError(ValueError):
    """Raised on any schema violation. Message names the offending key path
    only — never the value — so that upstream loggers cannot leak PII (L7).
    """


# ── Sub-dataclasses ───────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Name:
    first: str
    last: str
    full: str


@dataclass(frozen=True, slots=True)
class Contact:
    email: str
    phone: str | None = None
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    github_url: str | None = None


@dataclass(frozen=True, slots=True)
class Address:
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class WorkAuthorization:
    us_authorized: bool | None = None
    requires_sponsorship: bool | None = None


@dataclass(frozen=True, slots=True)
class EEO:
    gender: str | None = None
    race_ethnicity: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    pronouns: str | None = None


@dataclass(frozen=True, slots=True)
class Compensation:
    desired_salary_usd: int | None = None
    earliest_start_date: str | None = None
    willing_to_relocate: bool | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    name: str
    relationship: str | None = None
    email: str | None = None
    phone: str | None = None


# ── Root profile ──────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class CandidateProfile:
    name: Name
    contact: Contact
    address: Address
    work_authorization: WorkAuthorization
    eeo: EEO
    compensation: Compensation
    references: tuple[Reference, ...] = field(default_factory=tuple)

    # Public API ---------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "CandidateProfile":
        """Load, validate, and freeze a CandidateProfile from a YAML file.

        Raises ProfileValidationError on any schema violation. Never logs
        field values — only structural events referencing key paths.
        """
        p = Path(path)
        try:
            with p.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.warning("profile.load_failed", reason="file_not_found")
            raise
        except yaml.YAMLError as exc:  # pragma: no cover — trivially raised
            log.warning("profile.load_failed", reason="yaml_parse_error")
            raise ProfileValidationError(f"invalid YAML: {exc.__class__.__name__}") from exc

        if not isinstance(raw, dict):
            log.warning("profile.validation_failed", key="<root>")
            raise ProfileValidationError("profile root must be a mapping")

        try:
            profile = _build_profile(raw)
        except ProfileValidationError as exc:
            # Log the KEY only (already embedded in exc.args[0]), never a value.
            log.warning("profile.validation_failed", key=str(exc))
            raise

        log.info("profile.loaded")
        return profile

    def legacy_contact_string(self) -> str:
        """Back-compat contact line consumed by src/pdf_gen/renderer.py:232-233
        (drop-in is deferred to S17). Format: '<full> | <email> | <phone_or_empty>'.
        """
        phone = self.contact.phone or ""
        return f"{self.name.full} | {self.contact.email} | {phone}"


# ── Internal validators / builders ────────────────────────────────────────────
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "contact",
        "address",
        "work_authorization",
        "eeo",
        "compensation",
        "references",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DIGIT_RE = re.compile(r"\D+")


def _require(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ProfileValidationError(f"missing required key: {path}")
    return mapping[key]


def _ensure_mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileValidationError(f"expected mapping at: {path}")
    return value


def _reject_unknown_subkeys(mapping: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        # Deterministic message; keys only, no values.
        first = sorted(unknown)[0]
        raise ProfileValidationError(f"unknown key: {path}.{first}")


def _validate_email(email: Any) -> str:
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise ProfileValidationError("contact.email")
    return email


def _validate_phone(phone: Any) -> str | None:
    if phone is None:
        return None
    if not isinstance(phone, str):
        raise ProfileValidationError("contact.phone")
    digits = _DIGIT_RE.sub("", phone)
    if len(digits) < 7:
        raise ProfileValidationError("contact.phone")
    return phone


# Per-field validators keyed by dotted path — extracted per REFACTOR guidance
# in the spec so future fields (E.164 phone, address country) can be added
# without touching _build_contact / _build_profile.
_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "contact.email": _validate_email,
    "contact.phone": _validate_phone,
}


def _build_name(block: Any) -> Name:
    m = _ensure_mapping(block, "name")
    first = _require(m, "first", "name.first")
    last = _require(m, "last", "name.last")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ProfileValidationError("name.first/name.last must be strings")
    full = m.get("full")
    if full is None:
        full = f"{first} {last}"
    elif not isinstance(full, str):
        raise ProfileValidationError("name.full must be a string")
    _reject_unknown_subkeys(m, frozenset({"first", "last", "full"}), "name")
    return Name(first=first, last=last, full=full)


def _build_contact(block: Any) -> Contact:
    m = _ensure_mapping(block, "contact")
    if "email" not in m:
        raise ProfileValidationError("missing required key: contact.email")
    email = _VALIDATORS["contact.email"](m["email"])
    phone = _VALIDATORS["contact.phone"](m.get("phone"))
    _reject_unknown_subkeys(
        m,
        frozenset({"email", "phone", "linkedin_url", "portfolio_url", "github_url"}),
        "contact",
    )
    return Contact(
        email=email,
        phone=phone,
        linkedin_url=m.get("linkedin_url"),
        portfolio_url=m.get("portfolio_url"),
        github_url=m.get("github_url"),
    )


def _build_address(block: Any) -> Address:
    m = _ensure_mapping(block, "address")
    _reject_unknown_subkeys(
        m, frozenset({"line1", "city", "state", "postal", "country"}), "address"
    )
    return Address(**{k: m.get(k) for k in ("line1", "city", "state", "postal", "country")})


def _build_work_authorization(block: Any) -> WorkAuthorization:
    m = _ensure_mapping(block, "work_authorization")
    _reject_unknown_subkeys(
        m, frozenset({"us_authorized", "requires_sponsorship"}), "work_authorization"
    )
    return WorkAuthorization(
        us_authorized=m.get("us_authorized"),
        requires_sponsorship=m.get("requires_sponsorship"),
    )


def _build_eeo(block: Any) -> EEO:
    m = _ensure_mapping(block, "eeo")
    _reject_unknown_subkeys(
        m,
        frozenset(
            {"gender", "race_ethnicity", "veteran_status", "disability_status", "pronouns"}
        ),
        "eeo",
    )
    return EEO(
        gender=m.get("gender"),
        race_ethnicity=m.get("race_ethnicity"),
        veteran_status=m.get("veteran_status"),
        disability_status=m.get("disability_status"),
        pronouns=m.get("pronouns"),
    )


def _build_compensation(block: Any) -> Compensation:
    m = _ensure_mapping(block, "compensation")
    _reject_unknown_subkeys(
        m,
        frozenset({"desired_salary_usd", "earliest_start_date", "willing_to_relocate"}),
        "compensation",
    )
    return Compensation(
        desired_salary_usd=m.get("desired_salary_usd"),
        earliest_start_date=m.get("earliest_start_date"),
        willing_to_relocate=m.get("willing_to_relocate"),
    )


def _build_references(block: Any) -> tuple[Reference, ...]:
    if block is None:
        return ()
    if not isinstance(block, list):
        raise ProfileValidationError("expected list at: references")
    refs: list[Reference] = []
    for idx, item in enumerate(block):
        m = _ensure_mapping(item, f"references[{idx}]")
        _reject_unknown_subkeys(
            m, frozenset({"name", "relationship", "email", "phone"}), f"references[{idx}]"
        )
        name = _require(m, "name", f"references[{idx}].name")
        if not isinstance(name, str):
            raise ProfileValidationError(f"references[{idx}].name must be a string")
        refs.append(
            Reference(
                name=name,
                relationship=m.get("relationship"),
                email=m.get("email"),
                phone=m.get("phone"),
            )
        )
    return tuple(refs)


def _build_profile(raw: dict[str, Any]) -> CandidateProfile:
    # Required top-level keys (per AC #3): name, contact.
    if "name" not in raw:
        raise ProfileValidationError("missing required key: name")
    if "contact" not in raw:
        raise ProfileValidationError("missing required key: contact")

    # Reject unknown top-level keys (AC #4).
    unknown = set(raw) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        first = sorted(unknown)[0]
        raise ProfileValidationError(f"unknown key: {first}")

    return CandidateProfile(
        name=_build_name(raw["name"]),
        contact=_build_contact(raw["contact"]),
        address=_build_address(raw.get("address")),
        work_authorization=_build_work_authorization(raw.get("work_authorization")),
        eeo=_build_eeo(raw.get("eeo")),
        compensation=_build_compensation(raw.get("compensation")),
        references=_build_references(raw.get("references")),
    )
