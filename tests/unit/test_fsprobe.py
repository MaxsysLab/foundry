"""Case-insensitive workspaces (macOS APFS/HFS+ defaults, Windows, some network
shares) are unsupported by design: Foundry's collision checks and git plumbing treat
`A` and `a` as different paths, so the engine probes the workspace filesystem at
start and refuses rather than run on one."""

from __future__ import annotations

import pytest

from foundry import engine, fsprobe


def test_probe_on_a_case_sensitive_filesystem_passes_and_leaves_nothing(tmp_path):
    assert fsprobe.workspace_is_case_insensitive(tmp_path) is False
    assert list(tmp_path.iterdir()) == []


def test_probe_detects_case_folding(monkeypatch, tmp_path):
    # Simulate a case-folding filesystem: the lower-case name resolves to the file
    # the probe created under the upper-case name.
    monkeypatch.setattr(fsprobe, "_resolves_to", lambda a, b: True)
    assert fsprobe.workspace_is_case_insensitive(tmp_path) is True
    assert list(tmp_path.iterdir()) == []  # the probe dir is cleaned up either way


def test_probe_names_use_the_documented_prefix(monkeypatch, tmp_path):
    seen: list[str] = []
    real = fsprobe._resolves_to

    def spy(a, b):
        seen.extend([a.name, b.name])
        assert a.parent == b.parent and a.parent.parent == tmp_path
        assert a.parent.name.startswith(".foundry-case-probe-")
        assert a.exists()  # the upper-case probe file is really there when checked
        return real(a, b)

    monkeypatch.setattr(fsprobe, "_resolves_to", spy)
    assert fsprobe.workspace_is_case_insensitive(tmp_path) is False
    assert seen == [".foundry-case-probe-A", ".foundry-case-probe-a"]


def test_require_case_sensitive_refuses_with_the_workspace_named(monkeypatch, tmp_path):
    monkeypatch.setattr(fsprobe, "workspace_is_case_insensitive", lambda ws: True)
    with pytest.raises(fsprobe.UnsupportedWorkspaceError, match=r"(?s)case-insensitive.*unsupported") as info:
        fsprobe.require_case_sensitive(tmp_path)
    assert str(tmp_path) in str(info.value)


def test_engine_refuses_a_case_insensitive_workspace_before_running_anything(
    monkeypatch, fake_provider, fake_worker, tmp_path
):
    monkeypatch.setattr(fsprobe, "workspace_is_case_insensitive", lambda ws: True)
    ws = tmp_path / "ws"
    session = tmp_path / "session.json"
    with pytest.raises(fsprobe.UnsupportedWorkspaceError):
        engine.build("x", workspace=str(ws), provider=fake_provider, worker=fake_worker,
                     session_path=str(session))
    assert fake_worker.calls == []
    assert not session.exists()
    assert not (ws / ".git").exists() and not (ws / ".foundry").exists()


def test_engine_runs_on_a_case_sensitive_workspace(monkeypatch, fake_provider, fake_worker, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(
        fsprobe, "workspace_is_case_insensitive", lambda ws: calls.append(str(ws)) or False
    )
    state = engine.build("x", workspace=str(tmp_path / "ws"), provider=fake_provider,
                         worker=fake_worker)
    assert state.done is True and calls == [str(tmp_path / "ws")]


def test_cli_reports_an_unsupported_workspace_cleanly(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from foundry import cli

    monkeypatch.setattr(fsprobe, "workspace_is_case_insensitive", lambda ws: True)
    result = CliRunner().invoke(
        cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"]
    )
    assert result.exit_code == 2, result.output
    assert "case-insensitive" in result.output
    assert "Traceback" not in result.output
