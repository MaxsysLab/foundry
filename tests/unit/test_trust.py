"""The trust boundary: Foundry's workers run with the operator's credentials and
network, so a build either runs as the attending trusted operator (a terminal) or
inside a sandbox the container/VM wrapper attests. Anything else is refused before a
worker is started or the workspace is touched."""

from __future__ import annotations

import io
import sys
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from foundry import cli, engine, trust
from foundry.models import BuildState

runner = CliRunner()


@pytest.fixture
def bare_host(monkeypatch):
    """No sandbox attestation, stdin is not a terminal (cron, CI, a pipe)."""
    monkeypatch.delenv(trust.SANDBOX_ENV, raising=False)
    monkeypatch.setattr(trust, "_stdin_is_tty", lambda: False)


@pytest.fixture
def terminal(monkeypatch):
    """No sandbox attestation, an operator at a terminal."""
    monkeypatch.delenv(trust.SANDBOX_ENV, raising=False)
    monkeypatch.setattr(trust, "_stdin_is_tty", lambda: True)


# --- resolution rules


def test_sandbox_attestation_from_the_environment(bare_host, monkeypatch):
    monkeypatch.setenv(trust.SANDBOX_ENV, "lxc-foundry-7")
    t = trust.resolve()
    assert t.mode == "sandbox" and t.sandbox == "lxc-foundry-7"
    assert "lxc-foundry-7" in t.statement


def test_sandbox_attestation_from_the_option_wins_over_the_environment(bare_host, monkeypatch):
    monkeypatch.setenv(trust.SANDBOX_ENV, "env-name")
    assert trust.resolve(sandbox="vm-3").sandbox == "vm-3"
    assert trust.resolve(sandbox="vm-3", unattended=True).mode == "sandbox"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_attestation_is_no_attestation(bare_host, monkeypatch, blank):
    monkeypatch.setenv(trust.SANDBOX_ENV, blank)
    with pytest.raises(trust.TrustRefused):
        trust.resolve()
    with pytest.raises(trust.TrustRefused):
        trust.resolve(sandbox=blank, interactive=False)
    with pytest.raises(trust.TrustRefused):
        trust.resolve(sandbox=blank, interactive=True, unattended=True)


def test_unattended_without_attestation_is_refused(bare_host):
    with pytest.raises(trust.TrustRefused, match=r"(?s)unattended.*credentials.*FOUNDRY_SANDBOX"):
        trust.resolve()


def test_explicit_unattended_flag_is_refused_even_at_a_terminal(terminal):
    with pytest.raises(trust.TrustRefused, match="--unattended"):
        trust.resolve(unattended=True)


def test_operator_at_a_terminal_runs_in_trusted_operator_mode(terminal):
    t = trust.resolve()
    assert t.mode == "trusted-operator" and t.sandbox == ""
    assert "credentials" in t.statement and "network" in t.statement
    assert "Before you run it" in t.statement  # points at the README section


def test_closed_or_missing_stdin_counts_as_unattended(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert trust._stdin_is_tty() is False
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert trust._stdin_is_tty() is False


# --- the engine refuses first, before any worker runs or the workspace is written


def _spy_worker():
    class Spy:
        name = "spy"
        calls: ClassVar[list[str]] = []

        def is_available(self) -> bool:
            return True

        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            self.calls.append(instruction)
            return "ok"

    return Spy()


def test_engine_refuses_unattended_before_touching_anything(bare_host, fake_provider, tmp_path):
    ws = tmp_path / "ws"
    session = tmp_path / "session.json"
    worker = _spy_worker()
    with pytest.raises(trust.TrustRefused):
        engine.build("x", workspace=str(ws), provider=fake_provider, worker=worker,
                     session_path=str(session))
    assert not ws.exists() and not session.exists()
    assert worker.calls == []


def test_engine_refuses_before_any_llm_or_worker_call(bare_host, tmp_path):
    class NoProvider:
        name = "never"

        def is_available(self) -> bool:
            return True

        def complete(self, system, user, seat="planner") -> str:
            raise AssertionError("planning must not start before the trust check")

    with pytest.raises(trust.TrustRefused):
        engine.build("x", workspace=str(tmp_path / "ws"), provider=NoProvider(),
                     worker=_spy_worker())


def test_engine_runs_when_the_sandbox_is_attested_and_says_so(
    bare_host, monkeypatch, fake_provider, fake_worker, tmp_path
):
    monkeypatch.setenv(trust.SANDBOX_ENV, "lxc-1")
    events: list[tuple[str, str]] = []
    state = engine.build("x", workspace=str(tmp_path / "ws"), provider=fake_provider,
                         worker=fake_worker, on_event=lambda k, m: events.append((k, m)))
    assert state.done is True
    assert events[0][0] == "trust" and "lxc-1" in events[0][1]


def test_engine_accepts_an_explicit_trust_decision(bare_host, fake_provider, fake_worker, tmp_path):
    t = trust.resolve(sandbox="vm-9")
    state = engine.build("x", workspace=str(tmp_path / "ws"), provider=fake_provider,
                         worker=fake_worker, trust=t)
    assert state.done is True


# --- the CLI


def _spy_engine(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    def fake_build(brief, **kwargs):
        seen.update(kwargs)
        return BuildState(brief=brief, workspace=kwargs["workspace"], done=True)

    monkeypatch.setattr(cli, "build", fake_build)
    return seen


def test_cli_refuses_unattended_with_exit_2_before_calling_the_engine(bare_host, monkeypatch, tmp_path):
    ws = tmp_path / "ws"

    def never(*a, **k):
        raise AssertionError("engine must not run")

    monkeypatch.setattr(cli, "build", never)
    result = runner.invoke(cli.app, ["build", "a brief", "-w", str(ws), "--provider", "cli"])
    assert result.exit_code == 2, result.output
    assert "refuses" in result.output and "FOUNDRY_SANDBOX" in result.output
    assert not ws.exists()


def test_cli_unattended_flag_without_attestation_is_refused(terminal, monkeypatch, tmp_path):
    seen = _spy_engine(monkeypatch, tmp_path)
    result = runner.invoke(
        cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--unattended", "--provider", "cli"]
    )
    assert result.exit_code == 2, result.output
    assert "--unattended" in result.output and seen == {}


def test_cli_sandbox_option_attests_and_runs(bare_host, monkeypatch, tmp_path):
    seen = _spy_engine(monkeypatch, tmp_path)
    result = runner.invoke(
        cli.app,
        ["build", "a brief", "-w", str(tmp_path / "ws"), "--sandbox", "vm-2", "--unattended",
         "--provider", "cli"],
    )
    assert result.exit_code == 0, result.output
    assert "sandbox=vm-2" in result.output
    assert seen["trust"].mode == "sandbox" and seen["trust"].sandbox == "vm-2"


def test_cli_sandbox_env_attests_and_runs(bare_host, monkeypatch, tmp_path):
    monkeypatch.setenv(trust.SANDBOX_ENV, "container-5")
    seen = _spy_engine(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"])
    assert result.exit_code == 0, result.output
    assert seen["trust"].sandbox == "container-5"


def test_cli_at_a_terminal_prints_the_trusted_operator_statement(terminal, monkeypatch, tmp_path):
    seen = _spy_engine(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"])
    assert result.exit_code == 0, result.output
    assert "credentials" in result.output and "trusted-operator" in result.output
    assert seen["trust"].mode == "trusted-operator"
