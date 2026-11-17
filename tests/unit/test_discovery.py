"""The DISCOVERY phase: spec parsing, and that the spec threads into plan & verify."""

from __future__ import annotations

from foundry import discovery, engine, planner, verify
from foundry.models import AcceptanceCriterion, Spec

_SPEC_JSON = """{
  "goal": "a bookmark manager for one user",
  "requirements": ["add a bookmark", "delete a bookmark"],
  "acceptance_criteria": [
    {"id": "AC1", "statement": "A user can add a bookmark and see it appear in the list"},
    {"statement": "Deleting a bookmark removes it from the list"}
  ],
  "out_of_scope": ["multi-user auth"],
  "notes": "single-page"
}"""


def test_run_discovery_parses_spec(fake_provider, monkeypatch):
    monkeypatch.setattr(fake_provider, "complete", lambda s, u, seat="planner": _SPEC_JSON)
    spec = discovery.run_discovery(fake_provider, "a bookmark app")
    assert spec.goal == "a bookmark manager for one user"
    assert len(spec.acceptance_criteria) == 2
    assert spec.acceptance_criteria[0].id == "AC1"
    assert spec.acceptance_criteria[1].id == "AC2"  # missing id auto-assigned
    assert spec.out_of_scope == ["multi-user auth"]


def test_discovery_garbage_falls_back_to_brief(fake_provider, monkeypatch):
    monkeypatch.setattr(fake_provider, "complete", lambda s, u, seat="planner": "not json")
    spec = discovery.run_discovery(fake_provider, "build X")
    assert spec.goal == "build X" and spec.acceptance_criteria == []


def test_spec_as_context_includes_criteria():
    spec = Spec(
        goal="g",
        requirements=["r1"],
        acceptance_criteria=[AcceptanceCriterion(id="AC1", statement="works")],
    )
    ctx = spec.as_context()
    assert "AC1" in ctx and "works" in ctx and "g" in ctx


def test_plan_prompt_carries_the_spec(fake_provider, monkeypatch):
    captured = {}

    def fake_complete(system, user, seat="planner"):
        captured["user"] = user
        return fake_provider.__class__.complete(fake_provider, system, user, seat)

    monkeypatch.setattr(fake_provider, "complete", fake_complete)
    spec = Spec(goal="g", acceptance_criteria=[AcceptanceCriterion(id="AC1", statement="must work")])
    planner.plan(fake_provider, "brief", spec=spec)
    assert "AC1" in captured["user"] and "must work" in captured["user"]


def test_verify_instruction_includes_acceptance_criteria(monkeypatch):
    captured = {}

    class _W:
        name = "w"

        def is_available(self):
            return True

        def execute(self, system, instruction, workspace, seat="generalist"):
            captured["instruction"] = instruction
            return '{"verdict": "pass", "checks": [{"name": "AC1", "ok": true, "detail": "ok"}]}'

    spec = Spec(acceptance_criteria=[AcceptanceCriterion(id="AC1", statement="loads fine")])
    verify.run_verify(_W(), "goal", workspace=None, spec=spec)  # type: ignore[arg-type]
    assert "AC1" in captured["instruction"] and "loads fine" in captured["instruction"]


def test_engine_runs_discovery_and_stores_spec(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app", workspace=str(tmp_path), provider=fake_provider, worker=fake_worker
    )
    assert state.spec is not None
    assert len(state.spec.acceptance_criteria) == 2  # from conftest _SPEC
    assert state.done is True


def test_engine_discovery_disabled(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        discovery=False,
    )
    assert state.spec is None
    assert state.done is True
