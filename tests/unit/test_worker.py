"""Worker backend resolution (Agent SDK default, CLI fallback)."""

from __future__ import annotations

import pytest

from foundry.worker import (
    AgentSdkWorker,
    CliBuildWorker,
    TransientWorkerError,
    Worker,
    _looks_transient,
    get_worker,
)


def test_default_backend_is_agent_sdk():
    assert isinstance(get_worker(), AgentSdkWorker)
    assert isinstance(get_worker("sdk"), AgentSdkWorker)


def test_cli_backend():
    assert isinstance(get_worker("cli"), CliBuildWorker)


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown worker backend"):
        get_worker("nope")


def test_workers_implement_interface():
    for w in (AgentSdkWorker(), CliBuildWorker()):
        assert isinstance(w, Worker)
        assert hasattr(w, "execute") and hasattr(w, "is_available")


def test_transient_error_is_a_runtime_error():
    # So existing `except Exception` paths still catch it, but the engine can
    # special-case it for retry.
    assert issubclass(TransientWorkerError, RuntimeError)


def test_looks_transient_matches_infra_failures():
    # The SDK stringifies a transient HTTP API error as "error result: success".
    assert _looks_transient("Claude Code returned an error result: success")
    assert _looks_transient("API Overloaded (529)")
    assert _looks_transient("rate limit exceeded")
    assert _looks_transient("worker timed out after 1200s")


def test_looks_transient_ignores_real_failures():
    assert not _looks_transient("SyntaxError: invalid syntax in app/main.py")
    assert not _looks_transient("test_api failed: assertion error")
