"""
Test fixture: a duck-typed `ApplyContext` for the fast-path emailer (S13).

`sample_apply_context()` returns a `SimpleNamespace` that mimics the fields
S13's notify functions actually read off ctx (`.ats`, `.company`,
`.role_title`, `.job_url`, `.apply_url`, `.profile`).

Kept intentionally minimal and duck-typed — the real
`src.apply.types.ApplyContext` (S2) is a frozen dataclass with a wider
shape; S13 never imports it at runtime (only under `TYPE_CHECKING`), so
tests are free to use any object exposing the same attributes.

S18 (integration fixtures) is expected to re-export this factory.
"""

from __future__ import annotations

from types import SimpleNamespace


def sample_apply_context(**overrides) -> SimpleNamespace:
    """
    Return a minimal duck-typed ApplyContext for S13 tests.

    Defaults:
        ats           = "greenhouse"
        company       = "AcmeCo"
        role_title    = "Senior Backend Engineer"
        job_url       = "https://boards.greenhouse.io/acme/jobs/12345"
        apply_url     = "https://boards.greenhouse.io/acme/jobs/12345#app"
        profile.email = "candidate@example.com"
        profile.phone = "+15551234567"

    Any field can be overridden via kwargs; nested `profile.*` overrides are
    keyed as `profile=SimpleNamespace(email=..., phone=...)` by the caller.
    """
    defaults = {
        "ats": "greenhouse",
        "company": "AcmeCo",
        "role_title": "Senior Backend Engineer",
        "job_url": "https://boards.greenhouse.io/acme/jobs/12345",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/12345#app",
        "profile": SimpleNamespace(
            email="candidate@example.com",
            phone="+15551234567",
            first_name="Candace",
            last_name="Applicant",
        ),
        "config": {"apply": {"fast_path_recipient": "env:MY_EMAIL"}},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)
