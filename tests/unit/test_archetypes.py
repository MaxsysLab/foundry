"""Planner archetypes: detection, and the guarantee that required tasks are present."""

from __future__ import annotations

from foundry import archetypes, engine
from foundry.models import Plan, Spec, Task


def test_detect_web_app():
    assert archetypes.detect("a REST API with a React frontend").name == "web-app"
    assert archetypes.detect("a small website with a contact form").name == "web-app"


def test_detect_non_web_returns_none():
    assert archetypes.detect("a command-line file de-duplication utility") is None


def test_detect_uses_spec_text():
    spec = Spec(goal="a dashboard served over http with a login page")
    assert archetypes.detect("internal tool", spec) is not None


def _plan(*roles: str) -> Plan:
    tasks = [
        Task(id=f"t{i}", title=f"t{i}", role=r, instruction="i", depends_on=[f"t{i - 1}"] if i > 1 else [])
        for i, r in enumerate(roles, 1)
    ]
    return Plan(app_name="a", stack="s", summary="", tasks=tasks)


def test_enforce_appends_missing_security_task():
    plan = _plan("architect", "backend", "tester")  # no security role
    added = archetypes.enforce(plan, archetypes.WEB_APP)
    assert added == ["Security hardening & input sanitisation"]
    sec = [t for t in plan.tasks if t.role == "security"]
    assert len(sec) == 1
    # runs last — depends on every prior task
    assert set(sec[0].depends_on) == {"t1", "t2", "t3"}


def test_enforce_is_noop_when_security_present():
    plan = _plan("architect", "security", "tester")
    assert archetypes.enforce(plan, archetypes.WEB_APP) == []
    assert len(plan.tasks) == 3


def test_engine_guarantees_security_task_for_web_app(fake_provider, fake_worker, tmp_path):
    # conftest's planner returns roles architect/backend/tester (no security); a web brief
    # must get a security task appended and still build to done.
    state = engine.build(
        "a todo web app with a page",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
    )
    assert state.plan is not None
    assert any(t.role == "security" for t in state.plan.tasks)
    assert state.done is True
    # the appended task actually executed
    assert any("non-negotiable" in c for c in fake_worker.calls)


def test_engine_archetypes_disabled(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "a todo web app with a page",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        use_archetypes=False,
    )
    assert state.plan is not None
    assert not any(t.role == "security" for t in state.plan.tasks)
