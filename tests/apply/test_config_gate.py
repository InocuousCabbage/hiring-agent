"""
S3 config-gate tests — validator + apply.enabled soft-fail.

Covers the 13 RED-phase requirements from
`.agent/one-big-feature/auto-apply-2026-07-06/03-specs/03-s3-config-gate.md`.

The validator is invoked from `src/main.py::run_pipeline` before the S17
seam. When `apply.enabled: false`, it MUST no-op so a malformed apply block
cannot break the pre-Phase-3 pipeline.
"""

from __future__ import annotations

import copy
import logging
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from main import ConfigError, _validate_apply_config  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "apply"


# ── Helpers ────────────────────────────────────────────────────────────────


def _valid_apply_block(tmp_path: Path) -> dict:
    """A fully-valid apply block wired to writable dirs under tmp_path.

    The 21 top-level + 4 browserbase sub-keys from master-plan §4.7 (25 total)
    — no more, no fewer. (Earlier revisions of this fixture said "22 keys"
    but master-plan §4.7 enumerates 25; the S3 spec typo is patched in the
    S17 seam merge.)

    Points `profile_path` at the checked-in template so S1's real
    CandidateProfile.load() succeeds. Bare-stub `name: X\\nemail: Y` no
    longer validates now that S1's frozen 7-field dataclass owns the shape.
    """
    profile = (
        Path(__file__).resolve().parent.parent.parent
        / "templates"
        / "candidate_profile.yaml.example"
    )
    return {
        "enabled": True,
        "mode": "review",
        "allowed_ats": ["greenhouse"],
        "long_tail": "none",
        "dry_run": False,
        "timeout_seconds": 90,
        "navigation_retries": 2,
        "rate_limit_per_ats_per_day": 10,
        "review_timeout_hours": 72,
        "review_reping_hours": 24,
        "retention_days": 30,
        "screenshot_dir": str(tmp_path / "state" / "screenshots"),
        "trace_dir": str(tmp_path / "state" / "traces"),
        "storage_state_dir": str(tmp_path / "config" / "credentials" / "apply"),
        "dedup_db_path": str(tmp_path / "state" / "applied_jobs.db"),
        "captcha_action": "escalate",
        "captcha_transport": "browserbase",
        "profile_path": str(profile),
        "gmail_label_prefix": "hiring-agent/apply",
        "fast_path_recipient": "env:HIRING_AGENT_S3_TEST_EMAIL",
        "browserbase": {
            "enabled": True,
            "solve_captchas": True,
            "proxies": True,
            "block_ads": True,
        },
    }


def _valid_config(tmp_path: Path) -> dict:
    return {"apply": _valid_apply_block(tmp_path)}


@pytest.fixture
def valid_env(monkeypatch):
    """Set the env var the default fast_path_recipient points at."""
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")


# ── Soft-fail path ─────────────────────────────────────────────────────────


def test_validator_noop_when_disabled():
    """Malformed apply block with enabled=false must not raise —
    the pre-Phase-3 pipeline must remain undisturbed by the new gate."""
    cfg = yaml.safe_load((FIXTURES / "settings_disabled_malformed.yaml").read_text())
    # No exception, no side effects, no return value.
    assert _validate_apply_config(cfg) is None


# ── Happy path ─────────────────────────────────────────────────────────────


def test_validator_accepts_valid_enabled_config(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    assert _validate_apply_config(cfg) is None


# ── Unknown key ────────────────────────────────────────────────────────────


def test_validator_rejects_unknown_key(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["foo"] = "bar"
    with pytest.raises(ConfigError, match=r"unknown key: foo"):
        _validate_apply_config(cfg)


# ── Enum failures ──────────────────────────────────────────────────────────


def test_validator_rejects_bad_mode(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["mode"] = "yolo"
    with pytest.raises(ConfigError, match=r"mode"):
        _validate_apply_config(cfg)


def test_validator_rejects_empty_allowed_ats(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["allowed_ats"] = []
    with pytest.raises(ConfigError, match=r"allowed_ats"):
        _validate_apply_config(cfg)


def test_validator_rejects_unknown_ats(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["allowed_ats"] = ["linkedin"]
    with pytest.raises(ConfigError, match=r"linkedin"):
        _validate_apply_config(cfg)


# ── Relational range check ────────────────────────────────────────────────


def test_validator_rejects_reping_ge_timeout(tmp_path, valid_env):
    """review_reping_hours must precede review_timeout_hours — otherwise the
    24h re-ping fires AFTER the 72h auto-decline (spec master-plan Q5)."""
    cfg = _valid_config(tmp_path)
    cfg["apply"]["review_reping_hours"] = 96
    cfg["apply"]["review_timeout_hours"] = 72
    with pytest.raises(ConfigError, match=r"review_reping_hours"):
        _validate_apply_config(cfg)


# ── env: prefix parsing ───────────────────────────────────────────────────


def test_validator_rejects_missing_env_for_fast_path(tmp_path, monkeypatch):
    """`fast_path_recipient: env:UNSET_VAR_XYZ` when the var is unset →
    ConfigError. Prevents boot with a fast-path email that would silently
    resolve to empty (variation-B finding #13 — env allowlist parsing)."""
    monkeypatch.setenv("HIRING_AGENT_S3_TEST_EMAIL", "test@example.com")
    monkeypatch.delenv("UNSET_VAR_XYZ", raising=False)
    cfg = _valid_config(tmp_path)
    cfg["apply"]["fast_path_recipient"] = "env:UNSET_VAR_XYZ"
    with pytest.raises(ConfigError, match=r"UNSET_VAR_XYZ"):
        _validate_apply_config(cfg)


# ── Browserbase transport wiring ──────────────────────────────────────────


def test_validator_requires_browserbase_enabled_when_transport_selected(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["captcha_transport"] = "browserbase"
    cfg["apply"]["browserbase"]["enabled"] = False
    with pytest.raises(ConfigError, match=r"browserbase\.enabled"):
        _validate_apply_config(cfg)


# ── Warning-only opt-in ────────────────────────────────────────────────────


def test_validator_warns_on_computer_use_optin(tmp_path, valid_env, caplog):
    """long_tail=computer_use must NOT raise but MUST emit a warning
    so Ben's default-OFF stance is loudly opt-in."""
    cfg = _valid_config(tmp_path)
    cfg["apply"]["long_tail"] = "computer_use"
    with caplog.at_level(logging.WARNING):
        assert _validate_apply_config(cfg) is None
    assert any(
        "apply.long_tail.computer_use.enabled" in rec.getMessage()
        for rec in caplog.records
    ), f"expected warning event 'apply.long_tail.computer_use.enabled' in {[r.getMessage() for r in caplog.records]}"


# ── Purity: no dict mutation ──────────────────────────────────────────────


def test_validator_does_not_mutate_config(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    snapshot = copy.deepcopy(cfg)
    _validate_apply_config(cfg)
    assert cfg == snapshot, "validator must not mutate the config dict"


# ── Atomicity: no dirs created on validation failure ──────────────────────


def test_validator_creates_dirs_only_after_all_checks_pass(tmp_path, valid_env):
    """A bad enum AND a not-yet-existent path — the path directory must NOT
    materialize when validation fails (all-or-nothing atomicity)."""
    cfg = _valid_config(tmp_path)
    delayed = tmp_path / "delayed" / "screenshots"
    cfg["apply"]["screenshot_dir"] = str(delayed)
    cfg["apply"]["mode"] = "yolo"  # fails validation first
    with pytest.raises(ConfigError):
        _validate_apply_config(cfg)
    assert not delayed.exists(), "screenshot_dir must not be created on validation failure"


# ── Profile path existence ────────────────────────────────────────────────


def test_validator_rejects_profile_path_missing_file(tmp_path, valid_env):
    cfg = _valid_config(tmp_path)
    cfg["apply"]["profile_path"] = str(tmp_path / "nonexistent.yaml")
    with pytest.raises(ConfigError, match=r"profile_path"):
        _validate_apply_config(cfg)
