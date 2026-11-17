"""Orchestration: planning, dependency-ordered execution, gating, resume, parallelism."""

from __future__ import annotations

import threading
import time

from foundry import constants, engine, planner
from foundry.models import BuildState, GateReport, Plan, Task, TaskStatus
from foundry.session import load, save
from foundry.worker import TransientWorkerError


def test_planner_parses_plan(fake_provider):
    p = planner.plan(fake_provider, "build a todo app")
    assert p.app_name == "todo" and len(p.tasks) == 3
    assert p.tasks[2].depends_on == ["t2"]
    assert p.tasks[0].verify == "true"


def test_ready_tasks_respects_dependencies():
    plan = Plan(
        app_name="a",
        stack="s",
        summary="",
        tasks=[
            Task(id="t1", title="a", role="architect", instruction="i"),
            Task(id="t2", title="b", role="backend", instruction="i", depends_on=["t1"]),
        ],
    )
    assert [t.id for t in plan.ready_tasks(set())] == ["t1"]  # t2 blocked
    assert [t.id for t in plan.ready_tasks({"t1"})] == ["t2"]  # t1 done → t2 ready


def test_build_runs_tasks_in_dependency_order(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
    )
    assert state.done is True
    assert state.done_ids == ["t1", "t2", "t3"]
    # executed in dependency order
    assert (
        fake_worker.index_of("do x")
        < fake_worker.index_of("do y")
        < fake_worker.index_of("test it")
    )
    # gates ran for the two verified tasks
    assert {g.name for g in state.gates} == {"scaffold", "verify all"}
    assert all(g.passed for g in state.gates)


def test_failed_gate_marks_build_incomplete(fake_provider, fake_worker, tmp_path):
    # Force the verify gates to fail (`false` exits 1); after retries the build is incomplete.
    plan = planner.plan(fake_provider, "x")
    for t in plan.tasks:
        if t.verify:
            t.verify = "false"
    state = BuildState(brief="x", workspace=str(tmp_path), plan=plan)
    sp = tmp_path / "s.json"
    save(sp, state)
    result = engine.build(
        "x",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        session_path=str(sp),
    )
    assert result.done is False
    assert any(not g.passed for g in result.gates)


def test_gate_retry_feeds_failure_back(monkeypatch, fake_worker, tmp_path):
    seq = [
        GateReport(name="t", command="c", passed=False, output="boom"),
        GateReport(name="t", command="c", passed=True),
    ]
    monkeypatch.setattr(engine, "run_gate", lambda *a, **k: seq.pop(0))
    task = Task(id="t1", title="t", role="backend", instruction="i", verify="c")
    ok, _report = engine._run_task(fake_worker, task, "ctx", tmp_path, lambda *a: None)
    assert ok is True
    assert task.attempts == 2  # failed once, fixed, passed
    assert len(fake_worker.calls) == 2  # worker re-invoked with the failure


class _ConcurrencyWorker:
    """Records peak concurrent executions, to prove parallelism."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.1)
        with self._lock:
            self.active -= 1
        self.calls.append(instruction)
        if "qa engineer verifying" in system.lower():
            return ('{"verdict": "pass", "summary": "ran", '
                    '"checks": [{"name": "starts", "ok": true, "detail": "200"}]}')
        if "final code review" in system.lower():
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
        return "ok"


def test_independent_tasks_run_in_parallel(fake_provider, tmp_path):
    # t2 and t3 both depend only on t1, so they form one parallel wave after t1.
    plan = Plan(
        app_name="a",
        stack="s",
        summary="",
        tasks=[
            Task(id="t1", title="scaffold", role="architect", instruction="scaffold"),
            Task(id="t2", title="fe", role="frontend", instruction="frontend", depends_on=["t1"]),
            Task(id="t3", title="be", role="backend", instruction="backend", depends_on=["t1"]),
        ],
    )
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    worker = _ConcurrencyWorker()
    state = engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=worker, session_path=str(sp)
    )
    assert state.done
    assert worker.max_active >= 2  # t2 and t3 executed concurrently


class _FlakyWorker:
    """Raises a transient error the first `fail_times` calls, then succeeds.
    Mirrors a transient API blip (rate-limit/overload) that should be retried."""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientWorkerError("API Overloaded (529)")
        return "ok"


def test_transient_worker_error_is_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "WORKER_RETRY_BACKOFF", 0)  # no real sleeping in tests
    worker = _FlakyWorker(fail_times=2)
    task = Task(id="t1", title="t", role="backend", instruction="i")
    ok, _ = engine._run_task(worker, task, "ctx", tmp_path, lambda *a: None)
    assert ok is True
    assert worker.calls == 3  # 2 transient failures retried, 3rd succeeded


def test_transient_error_fails_task_after_exhausting_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "WORKER_RETRY_BACKOFF", 0)
    monkeypatch.setattr(constants, "WORKER_RETRIES", 2)
    worker = _FlakyWorker(fail_times=99)  # never recovers
    task = Task(id="t1", title="t", role="backend", instruction="i")
    ok, _ = engine._run_task(worker, task, "ctx", tmp_path, lambda *a: None)
    assert ok is False
    assert worker.calls == 3  # initial try + 2 retries, then gives up


def test_resume_retries_a_failed_task(fake_provider, fake_worker, tmp_path):
    # A task FAILED in a prior run (not in done_ids) must be retried on resume,
    # not skipped — done_ids is the only source of truth for completion.
    plan = planner.plan(fake_provider, "x")
    plan.tasks[2].status = TaskStatus.FAILED  # t3 failed last run; t1,t2 done
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan, done_ids=["t1", "t2"]))
    state = engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=fake_worker, session_path=str(sp)
    )
    assert state.done is True
    assert set(state.done_ids) == {"t1", "t2", "t3"}
    assert "test it" in " ".join(fake_worker.calls)  # the previously-failed t3 was retried


def test_resume_skips_completed_tasks(fake_provider, fake_worker, tmp_path):
    plan = planner.plan(fake_provider, "x")
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan, done_ids=["t1"]))
    engine.build(
        "x",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        session_path=str(sp),
    )
    joined = " ".join(fake_worker.calls)
    assert "do x" not in joined  # t1 already done → not re-run
    assert "do y" in joined and "test it" in joined
    loaded = load(sp)
    assert loaded is not None and loaded.done and set(loaded.done_ids) == {"t1", "t2", "t3"}
