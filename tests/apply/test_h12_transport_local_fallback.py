"""H12: get_transport hard-fails with TransportConfigError when
browserbase is misconfigured, when it should degrade gracefully to
LocalTransport + emit an audit event.
"""
from __future__ import annotations

import structlog.testing

import pytest


def test_get_transport_falls_back_to_local_on_browserbase_error(monkeypatch):
    """RED: with kind='some_captcha', captcha_transport='browserbase',
    browserbase.enabled=True, but the browserbase resolver raises (e.g.
    TransportConfigError from missing env), get_transport must return a
    LocalTransport and log a fallback event — never propagate the error.
    """
    import src.apply.transport as tm
    from src.apply.transport import get_transport, LocalTransport, TransportConfigError

    def _boom(name):
        if name == "browserbase":
            raise TransportConfigError("BROWSERBASE_API_KEY missing")
        # Real resolve for 'local'.
        from src.apply.transport.local import LocalTransport as LT
        return LT()

    monkeypatch.setattr(tm, "_resolve_transport", _boom)

    config = {
        "apply": {
            "captcha_transport": "browserbase",
            "browserbase": {"enabled": True},
        }
    }

    with structlog.testing.capture_logs() as captured:
        t = get_transport(config, kind="cloudflare_turnstile")

    assert isinstance(t, LocalTransport), (
        f"H12: expected LocalTransport fallback, got {type(t).__name__}"
    )

    # A fallback log event must fire so operators can spot the degrade.
    fallback = [e for e in captured if "fallback" in e.get("event", "").lower()]
    assert fallback, (
        f"H12: no fallback event logged; captured events: {[e.get('event') for e in captured]}"
    )
