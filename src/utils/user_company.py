"""User-company placeholder substitution.

The `templates/project_bank.yaml` file contains a `{{USER_COMPANY}}` token
where the user's real employer name should be rendered. The token is
kept in the checked-in project-bank so the file can ship publicly
without leaking the real employer name (per parsnip's private-terms
enumeration + inheritance-based-protection discipline). Substitution
happens at load time using the value from `config/settings.local.yaml`
(gitignored via `*.local.yaml`).

Fail-loud semantics per V1 fail-loud-halt architecture (same as GTM
strategy-sprint loader): missing config file OR missing user.company
OR unbound token in rendered output all raise `UnsubstitutedPlaceholderError`
rather than silently shipping the `{{USER_COMPANY}}` literal (which was
worse than the pre-fix "Acme Corp" placeholder — a raw token in a
cover letter is more embarrassing than the wrong-but-plausible name).

Bug this exists to fix: 2026-08-05 Sandpiper cover letter shipped
verbatim "At Acme Corp I built a company-wide marketing data platform
in Azure..." because there was no render-time substitution + the
project-bank content had literal "Acme Corp" strings the LLM inlined.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


USER_COMPANY_TOKEN = "{{USER_COMPANY}}"


class UnsubstitutedPlaceholderError(RuntimeError):
    """Raised when the user-company substitution cannot complete.

    Three shapes trigger this:
      1. config/settings.local.yaml missing
      2. settings.local.yaml exists but has no user.company field
      3. after substitution, the {{USER_COMPANY}} token still appears
         somewhere in the rendered content (defensive: catches a token
         in a place the substitution path missed)

    All three would silently ship a broken cover letter under the pre-fix
    behaviour. Fail-loud here surfaces the problem BEFORE the pipeline
    hands content to the applier.
    """


def load_user_company(repo_root: Path) -> str:
    """Read user.company from config/settings.local.yaml.

    The path is deliberately separate from settings.yaml so the real
    employer name never lives in a tracked file. The example lives at
    config/settings.local.example.yaml.

    Raises UnsubstitutedPlaceholderError on missing file or missing
    field. Returns the raw string on success (no length or shape check
    beyond non-empty; the user is responsible for spelling their own
    employer correctly).
    """
    path = repo_root / "config" / "settings.local.yaml"
    if not path.is_file():
        raise UnsubstitutedPlaceholderError(
            f"No {path.relative_to(repo_root)} found. Copy "
            f"config/settings.local.example.yaml to config/settings.local.yaml "
            f"and fill in user.company with your real employer name. The file "
            f"is gitignored ({path.name} matches *.local.yaml) so it won't "
            f"leak into the tree."
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise UnsubstitutedPlaceholderError(
            f"{path.relative_to(repo_root)} is not valid YAML: {exc}"
        ) from exc

    user_section = raw.get("user") or {}
    company = user_section.get("company")
    if not company or not isinstance(company, str) or not company.strip():
        raise UnsubstitutedPlaceholderError(
            f"{path.relative_to(repo_root)} has no user.company field, or the "
            f"field is empty. Fill it in with your real employer name (see "
            f"config/settings.local.example.yaml for the shape)."
        )
    if company.strip() == "YOUR_COMPANY_NAME_HERE":
        raise UnsubstitutedPlaceholderError(
            f"{path.relative_to(repo_root)} still contains the example "
            f"placeholder 'YOUR_COMPANY_NAME_HERE'. Replace with your real "
            f"employer name before running the pipeline."
        )
    return company.strip()


def substitute_user_company(content: Any, user_company: str) -> Any:
    """Recursively replace {{USER_COMPANY}} with the user's employer name.

    Walks arbitrary yaml-loaded structures (dicts, lists, strings) and
    replaces the token in every string leaf. Non-string leaves pass
    through untouched.

    Deliberately does NOT verify all tokens got replaced — the caller
    should invoke `assert_no_unsubstituted_tokens` afterwards. Two-step
    (substitute-then-assert) is cleaner than embedding the assertion:
    lets tests exercise "what happens when the substitution runs but
    a token elsewhere is unbound" as a distinct case.
    """
    if isinstance(content, str):
        return content.replace(USER_COMPANY_TOKEN, user_company)
    if isinstance(content, list):
        return [substitute_user_company(item, user_company) for item in content]
    if isinstance(content, dict):
        return {k: substitute_user_company(v, user_company) for k, v in content.items()}
    return content


def assert_no_unsubstituted_tokens(content: Any, context: str = "content") -> None:
    """Walk `content` and raise if any {{USER_COMPANY}} token remains.

    Called after `substitute_user_company` to catch tokens the substitution
    path might have missed (e.g., a nested structure the walker didn't
    handle, or a token added after substitution ran). Also serves as the
    regression guard from tests: fixture-authored content that accidentally
    contains a raw token fails HERE rather than in a shipped cover letter.

    `context` names WHAT is being checked so the error message is
    actionable (e.g., "project_bank after load", "generated cover letter").
    """
    tokens_found = _find_tokens(content)
    if tokens_found:
        raise UnsubstitutedPlaceholderError(
            f"{USER_COMPANY_TOKEN} token still present in {context} after "
            f"substitution (found at paths: {tokens_found[:5]}). This means "
            f"the substitution path missed a location OR a token was added "
            f"after substitution ran. Do NOT ship this content."
        )


def _find_tokens(content: Any, path: str = "$") -> list[str]:
    """Return every path at which USER_COMPANY_TOKEN appears in content."""
    found: list[str] = []
    if isinstance(content, str):
        if USER_COMPANY_TOKEN in content:
            found.append(path)
    elif isinstance(content, list):
        for i, item in enumerate(content):
            found.extend(_find_tokens(item, f"{path}[{i}]"))
    elif isinstance(content, dict):
        for k, v in content.items():
            found.extend(_find_tokens(v, f"{path}.{k}"))
    return found
