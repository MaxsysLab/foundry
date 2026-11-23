"""The planning provider that calls the `claude` command."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from foundry.providers import claude_cli_provider


def _provider_returning(monkeypatch, *, returncode: int, stdout: str, stderr: str):
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(claude_cli_provider.subprocess, "run", fake_run)
    provider = claude_cli_provider.ClaudeCliProvider()
    provider._bin = "/no/such/claude"
    return provider


def test_failure_carries_what_claude_printed_on_stdout(monkeypatch):
    provider = _provider_returning(
        monkeypatch, returncode=1, stdout="Invalid API key · Please run /login\n", stderr=""
    )
    with pytest.raises(RuntimeError) as err:
        provider.complete("sys", "plan")
    assert str(err.value) == "claude CLI failed (1): Invalid API key · Please run /login"


def test_failure_carries_stderr_and_stdout(monkeypatch):
    provider = _provider_returning(
        monkeypatch, returncode=2, stdout="from stdout", stderr="from stderr"
    )
    with pytest.raises(RuntimeError) as err:
        provider.complete("sys", "plan")
    assert "from stderr" in str(err.value)
    assert "from stdout" in str(err.value)


def test_failure_with_no_output_says_so(monkeypatch):
    provider = _provider_returning(monkeypatch, returncode=1, stdout="", stderr="  \n")
    with pytest.raises(RuntimeError) as err:
        provider.complete("sys", "plan")
    assert str(err.value) == "claude CLI failed (1): no output on stdout or stderr"


def test_long_failure_output_is_cut(monkeypatch):
    provider = _provider_returning(monkeypatch, returncode=1, stdout="x" * 1000, stderr="")
    with pytest.raises(RuntimeError) as err:
        provider.complete("sys", "plan")
    assert str(err.value) == "claude CLI failed (1): " + "x" * 300


def test_success_returns_stdout(monkeypatch):
    provider = _provider_returning(
        monkeypatch, returncode=0, stdout='{"tasks": []}', stderr="noise"
    )
    assert provider.complete("sys", "plan") == '{"tasks": []}'
