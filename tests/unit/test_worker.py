"""Worker backend resolution (Agent SDK default, CLI fallback)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from foundry import worker as worker_module
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


def _cli_worker_returning(monkeypatch, *, returncode: int, stdout: str, stderr: str):
    """A command-line worker whose `claude` call is a stand-in. Nothing is started."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)
    worker = CliBuildWorker()
    worker._bin = "/no/such/claude"
    return worker


def test_cli_worker_failure_carries_what_claude_printed_on_stdout(monkeypatch, tmp_path):
    worker = _cli_worker_returning(
        monkeypatch, returncode=1, stdout="Invalid API key · Please run /login\n", stderr=""
    )
    with pytest.raises(RuntimeError) as err:
        worker.execute("sys", "do the task", tmp_path)
    assert str(err.value) == "worker failed (1): Invalid API key · Please run /login"


def test_cli_worker_failure_with_no_output_says_so(monkeypatch, tmp_path):
    worker = _cli_worker_returning(monkeypatch, returncode=1, stdout="", stderr="")
    with pytest.raises(RuntimeError) as err:
        worker.execute("sys", "do the task", tmp_path)
    assert str(err.value) == "worker failed (1): no output on stdout or stderr"


def test_cli_worker_success_returns_stdout(monkeypatch, tmp_path):
    worker = _cli_worker_returning(monkeypatch, returncode=0, stdout="done", stderr="noise")
    assert worker.execute("sys", "do the task", tmp_path) == "done"
