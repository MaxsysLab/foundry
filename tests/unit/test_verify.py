"""The VERIFY phase: parsing, verdict semantics, and the engine's verify→fix gate."""

from __future__ import annotations

import pytest

from foundry import engine, planner, verify
from foundry.models import BuildState
from foundry.session import load, save

_PASS = '{"verdict": "pass", "summary": "ran", "checks": [{"name": "home", "ok": true, "detail": "200"}]}'
_FAIL = """{"verdict": "fail", "summary": "broken",
  "checks": [
    {"name": "homepage loads", "ok": false, "detail": "500 error page"},
    {"name": "server starts", "ok": true, "detail": "booted on :3000"}
  ]}"""
_INCONCLUSIVE = '{"verdict": "inconclusive", "summary": "no start script found", "checks": []}'


def test_parse_pass():
    r = verify._parse(_PASS)
    assert r.verdict == "pass" and not r.blocks and not r.failures()


def test_parse_fail_blocks():
    r = verify._parse(_FAIL)
    assert r.verdict == "fail" and r.blocks
    assert [c.name for c in r.failures()] == ["homepage loads"]


def test_parse_inconclusive_does_not_block():
    r = verify._parse(_INCONCLUSIVE)
    assert r.verdict == "inconclusive" and not r.blocks


def test_parse_pass_with_failed_check_is_coerced_to_fail():
    # A claimed "pass" that has a failed check is really a failure — trust the evidence.
    raw = '{"verdict": "pass", "checks": [{"name": "x", "ok": false, "detail": "broke"}]}'
    assert verify._parse(raw).verdict == "fail"


def test_an_answer_that_cannot_be_read_is_an_error_and_blocks():
    # A verifier that gave no report checked nothing. That is not "inconclusive",
    # which is a verifier saying it could not start the app here.
    for raw in ("not json", "Sorry, I ran out of turns.", '{"verdict": "pass", '):
        report = verify._parse(raw)
        assert report.verdict == "error" and report.blocks


class _BrokenAppWorker:
    """Builds fine, but the running app is broken on verification."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        self.calls.append(instruction)
        if "qa engineer verifying" in system.lower():
            return _FAIL
        if "final code review" in system.lower():
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
        return "ok"


def test_verify_blocks_done_and_runs_fix_loop(fake_provider, tmp_path):
    plan = planner.plan(fake_provider, "x")
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    worker = _BrokenAppWorker()
    state = engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=worker, session_path=str(sp)
    )
    assert state.verify is not None
    assert state.verify.verdict == "fail"
    assert state.done is False  # a broken running app blocks 'done'
    assert state.verify.rounds >= 1
    # the worker was handed the failing check to fix
    assert any("homepage loads" in c for c in worker.calls)
    loaded = load(sp)
    assert loaded is not None and loaded.verify is not None


def test_verify_pass_allows_done(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app", workspace=str(tmp_path), provider=fake_provider, worker=fake_worker
    )
    assert state.verify is not None and state.verify.verdict == "pass"
    assert state.done is True


def test_verify_inconclusive_does_not_block(fake_provider, tmp_path):
    class _InconclusiveWorker(_BrokenAppWorker):
        def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
            self.calls.append(instruction)
            if "qa engineer verifying" in system.lower():
                return _INCONCLUSIVE
            if "final code review" in system.lower():
                return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
            return "ok"

    state = engine.build(
        "build a todo app", workspace=str(tmp_path), provider=fake_provider, worker=_InconclusiveWorker()
    )
    assert state.verify is not None and state.verify.verdict == "inconclusive"
    assert state.done is True  # couldn't verify here, but not a definitive failure


def test_verify_disabled_skips_phase(fake_provider, fake_worker, tmp_path):
    state = engine.build(
        "build a todo app",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=fake_worker,
        verify=False,
    )
    assert state.verify is None
    assert state.done is True


_PASS = '{"verdict": "pass", "summary": "ok", "checks": [{"name": "starts", "ok": true}]}'


@pytest.mark.parametrize(
    "raw", ["{}", '{"verdict": "ok"}', '{"summary": "ran out of turns"}', '{"verdict": 7}']
)
def test_a_report_without_a_known_verdict_is_an_error(raw):
    report = verify._parse(raw)
    assert report.verdict == "error" and report.blocks


def test_a_failed_check_is_a_failure_whatever_the_verdict_says():
    raw = '{"verdict": "inconclusive", "checks": [{"name": "login", "ok": false}]}'
    report = verify._parse(raw)
    assert report.verdict == "fail" and report.blocks


def test_braces_in_the_prose_before_the_report_do_not_spoil_it():
    report = verify._parse("I started it on {port} and set {HOST}.\n\n" + _PASS)
    assert report.verdict == "pass" and not report.blocks


def test_the_last_report_in_the_answer_is_the_one_that_counts():
    earlier = '{"verdict": "fail", "checks": [{"name": "starts", "ok": false}]}'
    report = verify._parse(f"First try: {earlier}\nAfter a restart: {_PASS}")
    assert report.verdict == "pass"


def test_a_json_example_after_the_report_does_not_replace_it():
    report = verify._parse(_PASS + '\nFor the record, one item looked like {"title": "a"}.')
    assert report.verdict == "pass"
