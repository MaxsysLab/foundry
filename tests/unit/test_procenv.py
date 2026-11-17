"""Every process Foundry spawns gets one scrubbed environment: inherited `GIT_*`
variables that relocate a repository, inject configuration or name a program for git
to run never reach a worker, a gate, the planning CLI, SkillWarden or Foundry's own
git commands."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from foundry import gates, procenv, security
from foundry import isolation as isolation_mod
from foundry.providers import claude_cli_provider
from foundry.worker import AgentSdkWorker, CliBuildWorker

# One representative of each class the policy covers, plus two the policy must keep.
_POISON = {
    "GIT_DIR": "/no/such/repo.git",  # location
    "GIT_WORK_TREE": "/no/such/tree",
    "GIT_INDEX_FILE": "/no/such/index",
    "GIT_OBJECT_DIRECTORY": "/no/such/objects",
    "GIT_CONFIG_GLOBAL": "/no/such/gitconfig",  # configuration injection
    "GIT_CONFIG_SYSTEM": "/no/such/system",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/no/such/hooks",
    "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/no/such/hooks'",
    "GIT_SSH_COMMAND": "/no/such/ssh",  # programs git would execute
    "GIT_ASKPASS": "/no/such/askpass",
    "GIT_EXEC_PATH": "/no/such/exec",
    "GIT_EDITOR": "/no/such/editor",
    "GIT_TEMPLATE_DIR": "/no/such/templates",
    "GIT_LITERAL_PATHSPECS": "1",  # pathspec semantics
}
_KEPT = {"GIT_AUTHOR_NAME": "Operator", "GIT_TERMINAL_PROMPT": "0", "FOUNDRY_KEEP": "1"}


@pytest.fixture
def poisoned(monkeypatch):
    for k, v in {**_POISON, **_KEPT}.items():
        monkeypatch.setenv(k, v)
    return set(_POISON)


def _assert_scrubbed(env: dict[str, str] | os._Environ[str]) -> None:
    leaked = sorted(k for k in env if k in _POISON)
    assert leaked == [], leaked
    for k, v in _KEPT.items():
        assert env.get(k) == v  # operator intent (identity, prompts, unrelated vars) survives


def test_clean_env_removes_exactly_the_policy_set(poisoned):
    _assert_scrubbed(procenv.clean_env())
    assert procenv.clean_env()["PATH"] == os.environ["PATH"]
    assert all(procenv.is_scrubbed(k) for k in poisoned)
    assert not any(procenv.is_scrubbed(k) for k in _KEPT)


def test_isolation_uses_the_shared_policy(poisoned):
    # The git-command scrub must not drift from the one every other subprocess gets.
    assert isolation_mod._git_env() == procenv.clean_env()
    assert procenv.SCRUBBED_VARS >= isolation_mod._GIT_LOCATION_VARS


class _Spy:
    """Captures the keyword arguments of every subprocess.run call."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self.stdout, self.returncode = stdout, returncode

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")

    @property
    def env(self) -> dict[str, str]:
        assert len(self.calls) == 1 and "env" in self.calls[0], self.calls
        env = self.calls[0]["env"]
        assert isinstance(env, dict)
        return env


def test_gate_subprocess_gets_the_scrubbed_env(monkeypatch, poisoned, tmp_path):
    spy = _Spy()
    monkeypatch.setattr(gates.subprocess, "run", spy)
    assert gates.run_gate("g", "true", tmp_path).passed
    _assert_scrubbed(spy.env)


def test_cli_worker_subprocess_gets_the_scrubbed_env(monkeypatch, poisoned, tmp_path):
    spy = _Spy(stdout="report")
    monkeypatch.setattr("foundry.worker.subprocess.run", spy)
    worker = CliBuildWorker()
    worker._bin = "/no/such/claude"
    assert worker.execute("sys", "do it", tmp_path) == "report"
    _assert_scrubbed(spy.env)


def test_planning_cli_subprocess_gets_the_scrubbed_env(monkeypatch, poisoned):
    spy = _Spy(stdout="{}")
    monkeypatch.setattr(claude_cli_provider.subprocess, "run", spy)
    provider = claude_cli_provider.ClaudeCliProvider()
    provider._bin = "/no/such/claude"
    assert provider.complete("sys", "plan") == "{}"
    _assert_scrubbed(spy.env)


def test_skillwarden_subprocesses_get_the_scrubbed_env(monkeypatch, poisoned, tmp_path):
    (tmp_path / "s" / "SKILL.md").parent.mkdir()
    (tmp_path / "s" / "SKILL.md").write_text("# s\n")
    monkeypatch.setattr(security.shutil, "which", lambda cmd: "/no/such/skillwarden")
    spy = _Spy(stdout='{"findings": [], "severity_label": "low", "risk_score": 0, "status": "complete"}')
    monkeypatch.setattr(security.subprocess, "run", spy)
    assert security.critical_findings(tmp_path) == []
    _assert_scrubbed(spy.env)
    spy.calls.clear()
    assert security.vet_library(tmp_path) is not None
    _assert_scrubbed(spy.env)


def test_sdk_worker_scrubs_the_process_env_the_sdk_inherits(monkeypatch, poisoned, tmp_path):
    # The Agent SDK spawns `claude` with a copy of os.environ (ClaudeAgentOptions.env
    # can only add), so the worker must drop the variables from the process itself.
    seen: dict[str, str] = {}

    class _Result:
        result = "ok"
        is_error = False

    async def fake_query(*, prompt, options):
        seen.update(os.environ)
        yield _Result()

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_agent_sdk, "ResultMessage", _Result)
    assert AgentSdkWorker().execute("sys", "do it", tmp_path) == "ok"
    _assert_scrubbed(seen)


def test_engine_scrubs_before_the_first_worker_and_gate(monkeypatch, poisoned, fake_provider, tmp_path):
    # End to end through the engine: a gate runs `env` and would print what leaked.
    from foundry import engine

    class EnvWorker:
        name = "env"

        def is_available(self) -> bool:
            return True

        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            return "ok"

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        f"bad = sorted(k for k in os.environ if k in {sorted(_POISON)!r})\n"
        "print('leaked', bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    from foundry.models import BuildState, Plan, Task
    from foundry.session import save

    plan = Plan(app_name="a", stack="s", summary="", tasks=[
        Task(id="t1", title="one", role="backend", instruction="one",
             verify=f"{sys.executable} {probe}"),
    ])
    session = tmp_path / "session.json"
    save(session, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    state = engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=EnvWorker(),
        session_path=str(session), discovery=False, use_archetypes=False, verify=False,
        review=False,
    )
    assert [g.passed for g in state.gates] == [True], state.gates[-1].output
    assert Path(state.workspace) == tmp_path


def test_every_name_in_the_policy_is_dropped(monkeypatch):
    # Not a sample: every listed name (and both config prefixes) is removed.
    names = [*sorted(procenv.SCRUBBED_VARS), "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_7"]
    for name in names:
        monkeypatch.setenv(name, "poison")
    env = procenv.clean_env()
    assert [n for n in names if n in env] == []
    for name in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ICASE_PATHSPECS", "GIT_ATTR_SOURCE",
                 "GIT_ATTR_NOSYSTEM", "GIT_CONFIG_PARAMETERS"):
        assert procenv.is_scrubbed(name), name


def test_clean_env_is_safe_while_another_thread_scrubs(monkeypatch):
    import threading
    for i in range(200):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", "x")
    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(200):
                procenv.clean_env()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(200):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", "y")
        procenv.scrub_os_environ()
    for t in threads:
        t.join()
    assert errors == []
