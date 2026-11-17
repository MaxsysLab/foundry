"""Test doubles: a scripted planner provider and a fake build worker, so the
orchestration runs deterministically offline (no model is called, no real coding)."""

from __future__ import annotations

import pytest

from foundry import constants
from foundry.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _no_isolation(monkeypatch):
    """Default tests to no git isolation (pure orchestration); the isolation tests
    opt back in explicitly."""
    monkeypatch.setattr(constants, "ISOLATION", "none")


@pytest.fixture(autouse=True, scope="session")
def _attested_sandbox():
    """The suite runs fakes only (no worker ever starts), under pytest's non-terminal
    stdin: attest a sandbox so the engine's trust boundary lets builds run. Session
    scoped with its own MonkeyPatch so a test's `monkeypatch.undo()` cannot drop it;
    the trust tests remove it explicitly (per test, restored afterwards)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("FOUNDRY_SANDBOX", "pytest-offline-fakes")
    yield
    mp.undo()

@pytest.fixture(autouse=True, scope="session")
def _api_sign_in():
    """The `build` command refuses without an API sign-in. The suite runs fakes only
    and calls no model, so it names one with a value that is not a key. The sign-in
    tests remove it explicitly."""
    mp = pytest.MonkeyPatch()
    mp.setenv("ANTHROPIC_API_KEY", "not-a-key-offline-tests")
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _never_the_real_worker(monkeypatch):
    """No test may reach Claude Code. `engine.build` picks the real worker and the real
    planner when a caller hands in none; here that fails at once, loudly, instead of
    starting a live build. A test that means to check the choice itself patches these
    names again."""
    from foundry import engine

    def refuse(*_a, **_k):
        raise AssertionError(
            "a test reached Foundry's real worker or planner: pass fakes to engine.build"
        )

    monkeypatch.setattr(engine, "get_worker", refuse)
    monkeypatch.setattr(engine, "get_provider", refuse)

    # The real worker classes can also be built by the command line or handed in by a
    # test. Whatever builds them, they cannot start Claude here: the SDK's entry point
    # fails, and the `claude` command is not found.
    import shutil

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", refuse)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd, *a, **k: None if cmd == "claude" else real_which(cmd, *a, **k),
    )


_PLAN = """{
  "app_name": "todo", "stack": "Python", "summary": "a todo app",
  "tasks": [
    {"id": "t1", "title": "scaffold", "role": "architect", "instruction": "do x", "depends_on": [], "verify": "true"},
    {"id": "t2", "title": "feature", "role": "backend", "instruction": "do y", "depends_on": ["t1"], "verify": ""},
    {"id": "t3", "title": "verify all", "role": "tester", "instruction": "test it", "depends_on": ["t2"], "verify": "true"}
  ]
}"""


_SPEC = """{
  "goal": "a todo app", "requirements": ["add todos", "list todos"],
  "acceptance_criteria": [
    {"id": "AC1", "statement": "A user can add a todo and see it in the list"},
    {"id": "AC2", "statement": "Todos persist across restarts"}
  ],
  "out_of_scope": ["auth"], "notes": ""
}"""


class FakeProvider(LLMProvider):
    name = "fake"

    def is_available(self) -> bool:
        return True

    def complete(self, system: str, user: str, seat: str = "planner") -> str:
        if "product analyst" in system.lower():
            return _SPEC
        return _PLAN


class FakeWorker:
    """Records the instructions it was asked to execute; pretends to do the work."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        self.calls.append(instruction)
        if "final code review" in system.lower():
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
        if "qa engineer verifying" in system.lower():
            return (
                '{"verdict": "pass", "summary": "ran fine", '
                '"checks": [{"name": "homepage loads", "ok": true, "detail": "200"}]}'
            )
        return "done (fake)"

    def index_of(self, marker: str) -> int:
        return next(i for i, s in enumerate(self.calls) if marker in s)


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def fake_worker() -> FakeWorker:
    return FakeWorker()
