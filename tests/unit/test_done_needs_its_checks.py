"""A build is done only when the verify step and the review ran and did not block.

A check that could not run is not a check that passed. A worker that fails while
verifying or reviewing used to be reported as "skip", and the build could still be
called done.
"""

from __future__ import annotations

import pytest

from foundry import constants, engine
from foundry.models import VerifyReport
from foundry.worker import TransientWorkerError


class _FailsOn:
    """The usual fake worker, except that it fails when asked to verify or review."""

    name = "fails-on"

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.systems: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        self.systems.append(system)
        low = system.lower()
        if self.marker in low:
            raise RuntimeError("worker failed: the model call did not come back")
        if "final code review" in low:
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
        if "qa engineer verifying" in low:
            return ('{"verdict": "pass", "summary": "ran fine", '
                    '"checks": [{"name": "homepage loads", "ok": true, "detail": "200"}]}')
        return "done (fake)"


def _build(tmp_path, fake_provider, worker, **kw):
    events: list[tuple[str, str]] = []
    state = engine.build(
        "build a todo app",
        workspace=str(tmp_path),
        provider=fake_provider,
        worker=worker,
        on_event=lambda stage, detail: events.append((stage, detail)),
        **kw,
    )
    return state, events


def test_a_review_that_could_not_run_does_not_leave_the_build_done(fake_provider, tmp_path):
    state, events = _build(tmp_path, fake_provider, _FailsOn("final code review"))
    assert state.done_ids == ["t1", "t2", "t3"]  # every task passed
    assert state.done is False
    assert state.review is not None and state.review.approved is False
    assert "could not run" in " ".join(state.review.axes.values())
    assert [d for s, d in events if s == "review-error"]


def test_a_verify_that_could_not_run_does_not_leave_the_build_done(fake_provider, tmp_path):
    state, events = _build(tmp_path, fake_provider, _FailsOn("qa engineer verifying"))
    assert state.done is False
    assert state.verify is not None and state.verify.verdict == "error"
    assert state.verify.blocks is True
    assert "could not run" in state.verify.summary
    assert [d for s, d in events if s == "verify-error"]


def test_a_verify_that_could_not_run_is_not_sent_to_a_worker_to_fix(fake_provider, tmp_path):
    worker = _FailsOn("qa engineer verifying")
    _build(tmp_path, fake_provider, worker)
    verifier_calls = [s for s in worker.systems if "qa engineer verifying" in s.lower()]
    assert len(verifier_calls) == 1  # no second round, and no fix round, on an error


def test_a_step_skipped_on_purpose_still_lets_the_build_be_done(fake_provider, tmp_path):
    # --no-verify and --no-review are the operator's own choice, and are said out loud.
    worker = _FailsOn("never matches")
    state, events = _build(tmp_path, fake_provider, worker, verify=False, review=False)
    assert state.done is True and state.verify is None and state.review is None
    done = next(d for s, d in events if s == "done")
    assert "verify: skipped" in done and "review: skipped" in done


def test_the_build_can_be_resumed_after_a_review_that_could_not_run(fake_provider, tmp_path):
    session = tmp_path / "run.json"
    ws = tmp_path / "ws"
    state, _ = _build(ws, fake_provider, _FailsOn("final code review"), session_path=str(session))
    assert state.done is False
    again, _ = _build(ws, fake_provider, _FailsOn("never matches"), session_path=str(session))
    assert again.done is True and again.review is not None and again.review.approved


def test_an_inconclusive_verify_still_does_not_block():
    assert VerifyReport(verdict="inconclusive").blocks is False
    assert VerifyReport(verdict="fail").blocks is True
    assert VerifyReport(verdict="error").blocks is True


class _AnswersVerifyWith(_FailsOn):
    """The usual fake worker, with a fixed answer when asked to verify."""

    def __init__(self, answer: str) -> None:
        super().__init__("never matches")
        self.answer = answer

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        if "qa engineer verifying" in system.lower():
            self.systems.append(system)
            return self.answer
        return super().execute(system, instruction, workspace, seat)


def test_a_verifier_that_gives_no_report_does_not_leave_the_build_done(fake_provider, tmp_path):
    worker = _AnswersVerifyWith("Sorry, I ran out of turns.")
    state, _ = _build(tmp_path, fake_provider, worker)
    assert state.done is False
    assert state.verify is not None and state.verify.verdict == "error"


class _BlipsOnce(_FailsOn):
    """Fails once with a passing failure on the marked step, then works."""

    def __init__(self, marker: str) -> None:
        super().__init__("never matches")
        self.blip_on = marker
        self.blipped = False

    def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
        if self.blip_on in system.lower() and not self.blipped:
            self.blipped = True
            raise TransientWorkerError("transient API error (HTTP 529)")
        return super().execute(system, instruction, workspace, seat)


@pytest.mark.parametrize("step", ["qa engineer verifying", "final code review"])
def test_a_passing_failure_at_verify_or_review_is_retried(monkeypatch, fake_provider, tmp_path, step):
    monkeypatch.setattr(constants, "WORKER_RETRY_BACKOFF", 0.0)
    worker = _BlipsOnce(step)
    state, events = _build(tmp_path, fake_provider, worker)
    assert worker.blipped and state.done is True
    assert [d for s, d in events if s == "retry"]


@pytest.mark.parametrize("name", ["VERIFY_MAX_ROUNDS", "REVIEW_MAX_ROUNDS"])
def test_a_round_count_of_zero_does_not_skip_the_check(monkeypatch, fake_provider, tmp_path, name):
    monkeypatch.setattr(constants, name, 0)
    worker = _FailsOn("never matches")
    state, _ = _build(tmp_path, fake_provider, worker)
    assert state.verify is not None and state.review is not None
    assert state.verify.rounds == 1 and state.review.rounds == 1


def test_resuming_without_the_brief_uses_the_one_in_the_session(tmp_path):
    from foundry.models import BuildState
    from foundry.providers.base import LLMProvider
    from foundry.session import save

    seen: list[str] = []

    class Recording(LLMProvider):
        name = "recording"

        def is_available(self) -> bool:
            return True

        def complete(self, system: str, user: str, seat: str = "planner") -> str:
            seen.append(user)
            return "{}"

    session = tmp_path / "run.json"
    save(session, BuildState(brief="a recipe box", workspace=str(tmp_path / "ws"), guidance="keep it small"))
    engine.build(
        "", workspace=str(tmp_path / "ws"), provider=Recording(), worker=_FailsOn("never matches"),
        session_path=str(session),
    )
    assert seen and all("a recipe box" in user for user in seen)
    assert all("keep it small" in user for user in seen)


def test_a_verifier_with_no_report_is_not_sent_to_a_worker_to_fix(fake_provider, tmp_path):
    worker = _AnswersVerifyWith("Sorry, I ran out of turns.")
    state, events = _build(tmp_path, fake_provider, worker)
    assert state.done is False and state.verify is not None and state.verify.rounds == 1
    assert [d for s, d in events if s == "verify-error"]
    assert not [d for s, d in events if s == "verify-fix"]
    asked = [s for s in worker.systems if "qa engineer verifying" in s.lower()]
    assert len(asked) == 1  # the verifier was asked once, with no second round


def test_a_review_with_braces_in_its_prose_is_still_read():
    from foundry import review

    raw = 'I looked at {name} handling.\n{"verdict": "approve", "score": 88, "axes": {}, "findings": []}'
    parsed = review._parse(raw)
    assert parsed.verdict == "approve" and parsed.score == 88
