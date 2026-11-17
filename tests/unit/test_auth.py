"""A build starts only with an API sign-in.

Foundry's workers are Claude Code, driven through the Claude Agent SDK, and Anthropic
asks that tools built on that SDK use API key authentication and do not offer a
claude.ai sign-in. So the command refuses to build unless the environment names one of
the API methods the SDK documents.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from foundry import auth, cli


def test_no_api_method_is_refused_and_the_message_says_what_to_set():
    with pytest.raises(auth.AuthRefused) as err:
        auth.require_api_auth({})
    text = str(err.value)
    assert "ANTHROPIC_API_KEY" in text and "claude.ai" in text
    assert "Nothing was started" in text


@pytest.mark.parametrize("name", auth.API_AUTH_VARS)
def test_each_documented_method_is_accepted(name):
    assert auth.require_api_auth({name: "1"}) == name


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_api_key_is_no_sign_in(value):
    with pytest.raises(auth.AuthRefused):
        auth.require_api_auth({"ANTHROPIC_API_KEY": value})


@pytest.mark.parametrize(
    "value", ["", "0", "false", "no", "off", " ", "n", "disabled", "none", "2", "nope", "yes", "on"]
)
def test_a_provider_switch_that_is_off_is_no_sign_in(value):
    with pytest.raises(auth.AuthRefused):
        auth.require_api_auth({"CLAUDE_CODE_USE_BEDROCK": value})


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " 1 "])
def test_a_provider_switch_counts_only_at_the_documented_values(value):
    assert auth.require_api_auth({"CLAUDE_CODE_USE_VERTEX": value}) == "CLAUDE_CODE_USE_VERTEX"


def test_the_api_key_is_named_first_when_several_are_set():
    env = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_API_KEY": "k"}
    assert auth.require_api_auth(env) == "ANTHROPIC_API_KEY"


def _without_sign_in(monkeypatch):
    for name in auth.API_AUTH_VARS:
        monkeypatch.delenv(name, raising=False)


def test_the_build_command_refuses_without_a_sign_in(monkeypatch, tmp_path):
    _without_sign_in(monkeypatch)
    called = []
    monkeypatch.setattr(cli, "build", lambda *a, **k: called.append(1))
    ws = tmp_path / "ws"
    result = CliRunner().invoke(cli.app, ["build", "a brief", "-w", str(ws), "--provider", "cli"])
    assert result.exit_code == 2
    assert called == [] and not ws.exists()
    assert "ANTHROPIC_API_KEY" in result.output


def test_the_trust_refusal_comes_before_the_sign_in_check(monkeypatch, tmp_path):
    _without_sign_in(monkeypatch)
    monkeypatch.delenv("FOUNDRY_SANDBOX", raising=False)
    result = CliRunner().invoke(
        cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"]
    )
    assert result.exit_code == 2
    assert "unattended" in result.output and "ANTHROPIC_API_KEY" not in result.output


def test_vet_needs_no_sign_in(monkeypatch, tmp_path):
    # Vetting a skill library calls no model.
    _without_sign_in(monkeypatch)
    from foundry import security

    monkeypatch.setattr(security, "skillwarden_available", lambda: True)
    monkeypatch.setattr(security, "vet_library", lambda _p: [])
    result = CliRunner().invoke(cli.app, ["vet", str(tmp_path)])
    assert result.exit_code == 1  # no bundles found, and no refusal


def test_the_engine_checks_the_sign_in_when_it_picks_the_worker_itself(monkeypatch, tmp_path, fake_provider):
    # A caller that hands in its own worker and planner chooses its own sign-in. A
    # caller that lets Foundry pick them gets Claude Code, and is checked.
    from foundry import engine

    _without_sign_in(monkeypatch)
    ws = tmp_path / "ws"
    with pytest.raises(auth.AuthRefused):
        engine.build("a brief", workspace=str(ws), provider=fake_provider)
    assert not ws.exists()


def test_the_engine_does_not_check_a_caller_that_brings_both(monkeypatch, tmp_path, fake_provider, fake_worker):
    from foundry import engine

    _without_sign_in(monkeypatch)
    state = engine.build(
        "a brief", workspace=str(tmp_path / "ws"), provider=fake_provider, worker=fake_worker
    )
    assert state.done is True


def test_a_refused_model_setting_exits_2_without_a_traceback(monkeypatch, tmp_path):
    from foundry import constants

    def refuse(*_a, **_k):
        raise constants.JudgeDowngradeRefused("seat 'review' cannot run on 'haiku'")

    monkeypatch.setattr(cli, "build", refuse)
    result = CliRunner().invoke(
        cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"]
    )
    assert result.exit_code == 2 and "cannot run on" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_the_engine_checks_the_sign_in_for_a_real_worker_handed_in(monkeypatch, tmp_path, fake_provider):
    # A caller that passes Foundry's own Claude worker is using Claude Code too.
    from foundry import engine
    from foundry.worker import AgentSdkWorker, CliBuildWorker

    def never(*_a, **_k):
        raise AssertionError("the real worker was reached")

    monkeypatch.setattr(AgentSdkWorker, "execute", never)
    monkeypatch.setattr(CliBuildWorker, "execute", never)
    _without_sign_in(monkeypatch)
    for worker in (AgentSdkWorker(), CliBuildWorker()):
        ws = tmp_path / type(worker).__name__
        with pytest.raises(auth.AuthRefused):
            engine.build("a brief", workspace=str(ws), provider=fake_provider, worker=worker)
        assert not ws.exists()


def test_the_engine_checks_the_sign_in_for_the_claude_command_as_planner(monkeypatch, tmp_path, fake_worker):
    from foundry import engine
    from foundry.providers.claude_cli_provider import ClaudeCliProvider

    _without_sign_in(monkeypatch)
    ws = tmp_path / "ws"
    with pytest.raises(auth.AuthRefused):
        engine.build("a brief", workspace=str(ws), provider=ClaudeCliProvider(), worker=fake_worker)
    assert not ws.exists()
