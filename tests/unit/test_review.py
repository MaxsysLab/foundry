"""The REVIEW phase: parsing, verdict, and the engine's review→fix gate."""

from __future__ import annotations

from foundry import engine, planner, review
from foundry.models import BuildState
from foundry.session import load, save

_APPROVE = '{"verdict": "approve", "score": 92, "axes": {"security": "ok"}, "findings": []}'
_CHANGES = """{"verdict": "changes_requested", "score": 55,
  "axes": {"security": "issue"},
  "findings": [
    {"axis": "security", "severity": "high", "file": "app.py", "issue": "SQL injection", "recommendation": "Parameterize the query"},
    {"axis": "readability", "severity": "low", "file": "x.py", "issue": "long function", "recommendation": "split it"}
  ]}"""


def test_parse_approve():
    r = review._parse(_APPROVE)
    assert r.approved and r.score == 92 and not r.findings


def test_parse_changes_and_blocking():
    r = review._parse(_CHANGES)
    assert not r.approved
    assert len(r.findings) == 2
    assert [f.severity for f in r.blocking()] == ["high"]  # only the high finding blocks


def test_parse_garbage_is_changes_requested():
    assert review._parse("not json at all").verdict == "changes_requested"


class _ChangesWorker:
    """Builds fine, but always requests changes on review (one high finding)."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        self.calls.append(instruction)
        return _CHANGES if "final code review" in system.lower() else "ok"


def test_review_blocks_done_and_runs_fix_loop(fake_provider, tmp_path):
    plan = planner.plan(fake_provider, "x")
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    worker = _ChangesWorker()
    state = engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=worker, session_path=str(sp)
    )
    assert state.review is not None
    assert state.review.verdict == "changes_requested"
    assert state.done is False  # unresolved high finding blocks 'done'
    assert state.review.rounds >= 1
    # the worker was given the blocking finding to fix
    assert any("SQL injection" in c for c in worker.calls)
    loaded = load(sp)
    assert loaded is not None and loaded.review is not None


def test_review_disabled_skips_phase(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        review=False,
    )
    assert state.review is None
    assert state.done is True  # tasks complete; no review gate
