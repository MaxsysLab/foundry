"""The Foundry engine — orchestrates an autonomous build.

  brief
    → plan (architect): stack + dependency-ordered tasks   ── checkpoint: approve ──
    → execute tasks in dependency order, each by a role-specialized worker
        → quality gate (build/test/typecheck); on fail, feed output back and retry
    → BuildState (resumable)

Driving the headless `claude` worker per task is what makes it autonomous; the
gates are what keep it to the project's standard. With a session file, an interrupted
build resumes from completed tasks.
"""

from __future__ import annotations

import secrets
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path

from foundry import archetypes as archetypes_mod
from foundry import auth as auth_mod
from foundry import constants, fsprobe, planner, procenv
from foundry import constitution as constitution_mod
from foundry import discovery as discovery_mod
from foundry import review as review_mod
from foundry import session as session_mod
from foundry import trust as trust_mod
from foundry import verify as verify_mod
from foundry.control import AutoController, Controller, Revise
from foundry.gates import run_gate
from foundry.isolation import (
    HistoryUnreadableError,
    IntegrationError,
    MergeUnverifiedError,
    WorktreeManager,
    WorktreeRecord,
)
from foundry.models import (
    BuildState,
    GateReport,
    Plan,
    Review,
    Task,
    TaskStatus,
    VerifyReport,
)
from foundry.providers.base import LLMProvider, get_provider
from foundry.providers.claude_cli_provider import ClaudeCliProvider
from foundry.roles import system_prompt
from foundry.skills import SkillLibrary
from foundry.worker import (
    AgentSdkWorker,
    CliBuildWorker,
    TransientWorkerError,
    Worker,
    get_worker,
)

EventFn = Callable[[str, str], None]


def _noop(stage: str, detail: str) -> None:
    pass


def _execute_with_retry(
    worker: Worker,
    sysp: str,
    instruction: str,
    workspace: Path,
    on_event: EventFn,
    seat: str = "generalist",
) -> str:
    """Run the worker, retrying transient API/infra failures (rate-limit, overload,
    5xx, timeout) with linear backoff. A transient blip shouldn't fail a task that
    would otherwise succeed; genuine errors are not retried."""
    last: TransientWorkerError | None = None
    for i in range(constants.WORKER_RETRIES + 1):
        try:
            return worker.execute(sysp, instruction, workspace, seat=seat)
        except TransientWorkerError as exc:
            last = exc
            if i < constants.WORKER_RETRIES:
                wait = constants.WORKER_RETRY_BACKOFF * (i + 1)
                on_event(
                    "retry",
                    f"transient error (try {i + 1}/{constants.WORKER_RETRIES}); "
                    f"retrying in {wait:.0f}s: {str(exc)[:80]}",
                )
                time.sleep(wait)
    assert last is not None
    raise last


class _Retrying(Worker):
    """A worker whose passing failures are retried, for the verify and review calls."""

    name = "retrying"

    def __init__(self, worker: Worker, on_event: EventFn) -> None:
        self._worker = worker
        self._on_event = on_event

    def is_available(self) -> bool:
        return self._worker.is_available()

    def execute(
        self, system: str, instruction: str, workspace: Path, seat: str = "generalist"
    ) -> str:
        return _execute_with_retry(
            self._worker, system, instruction, workspace, self._on_event, seat=seat
        )


def _report_retained(rec: WorktreeRecord | None, on_event: EventFn) -> None:
    """Worktrees are never auto-deleted; surface each one for manual cleanup."""
    if rec is not None:
        on_event("worktree-retained", f"{rec.describe()} retained: clean up manually")


def _run_task(
    worker: Worker,
    task: Task,
    ctx: str,
    workspace: Path,
    on_event: EventFn,
    constitution: str = "",
) -> tuple[bool, GateReport | None]:
    """Execute a task; if it has a verify gate, run it and feed failures back to the
    worker (bounded retries). Returns (passed, last_gate_report)."""
    sysp = system_prompt(task.role, constitution)
    instruction = ctx
    report: GateReport | None = None
    for attempt in range(1, constants.MAX_TASK_ATTEMPTS + 1):
        task.attempts = attempt
        try:
            task.summary = _execute_with_retry(
                worker, sysp, instruction, workspace, on_event, seat=task.role
            )
        except Exception as exc:
            on_event("error", f"worker failed: {str(exc)[:100]}")
            return False, report
        if not task.verify:
            return True, None
        report = run_gate(task.title, task.verify, workspace)
        if report.passed:
            return True, report
        on_event("gate-fail", f"{task.title} (attempt {attempt}/{constants.MAX_TASK_ATTEMPTS})")
        instruction = (
            f"{ctx}\n\nThe verification command `{task.verify}` FAILED with:\n{report.output}\n\n"
            "Diagnose and fix the cause so the command exits 0. Then stop."
        )
    return False, report


def build(
    brief: str,
    *,
    workspace: str,
    provider: LLMProvider | None = None,
    worker: Worker | None = None,
    controller: Controller | None = None,
    guidance: str = "",
    skill_library: str | None = None,
    constitution_path: str | None = None,
    use_archetypes: bool = constants.ARCHETYPES,
    discovery: bool = constants.DISCOVERY,
    verify: bool = constants.VERIFY,
    review: bool = constants.REVIEW,
    isolation: str | None = None,
    session_path: str | None = None,
    on_event: EventFn = _noop,
    trust: trust_mod.Trust | None = None,
) -> BuildState:
    # The trust boundary comes first: an unattended run on a bare host without a
    # sandbox attestation is refused before any LLM call, worker or workspace write.
    trust = trust or trust_mod.resolve()
    on_event("trust", trust.statement)
    # Then the sign-in, when Foundry picks the worker or the planner itself: those are
    # Claude Code, which must run on an API method (see auth.py).
    if (
        worker is None
        or provider is None
        or isinstance(worker, AgentSdkWorker | CliBuildWorker)
        or isinstance(provider, ClaudeCliProvider)
    ):
        auth_mod.require_api_auth()
    # Once, on the main thread, before any worker can start: the Agent SDK copies this
    # process's environment, so the policy's GIT_* variables are dropped from it (an
    # embedding host loses them too; see README, "Before you run it").
    procenv.scrub_os_environ()
    # Refuse a judge-demoting configuration before a single seat is billed — a config
    # error must not surface as REVIEW exploding after a whole build is paid for.
    constants.check_protected_seats()
    provider = provider or get_provider(constants.PROVIDER)
    worker = worker or get_worker()
    controller = controller or AutoController()
    library = SkillLibrary.load(skill_library or constants.SKILL_LIBRARY)
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    # Case-insensitive filesystems are unsupported by design (see fsprobe): refused
    # before the session, the repo, staged skills or any worker touches the workspace.
    fsprobe.require_case_sensitive(ws)
    constitution = constitution_mod.load(constitution_path, ws)

    state = (session_mod.load(session_path) if session_path else None) or BuildState(
        brief=brief, workspace=str(ws), guidance=guidance
    )
    # A resumed build is run without the brief: the session holds it.
    if not brief.strip():
        brief = state.brief
    if not guidance.strip():
        guidance = state.guidance

    def checkpoint() -> None:
        if session_path:
            session_mod.save(session_path, state)

    if not state.build_id:
        # Saved before any task can merge: a resume finds this build's merges in the
        # base history by this id even if a later session save fails.
        state.build_id = secrets.token_hex(8)
        checkpoint()

    def _save_after_abort(
        task: Task, exc: BaseException, paths: list[str] | None, notes: list[tuple[str, str]]
    ) -> None:
        """Save a task the base may already hold before an abort propagates, and say
        so. Reports go out first (a second Ctrl-C during the save must not silence
        them); a failing event sink never masks `exc`, a failing save is reported."""
        if task.status is TaskStatus.DONE and task.id not in state.done_ids:
            state.done_ids = sorted({*state.done_ids, task.id})
        for kind, msg in notes:  # e.g. git's own reason for a failed write
            with suppress(Exception):
                on_event(kind, msg)
        if task.status is TaskStatus.UNVERIFIED:
            if paths is None:
                where = (f"integration failed unexpectedly ({exc!r}) before Foundry wrote "
                         "to the base; nothing was merged, but it is held for inspection")
            else:
                written = f" (paths: {', '.join(paths[:10])})" if paths else ""
                where = (f"integration stopped ({exc!r}); the base may hold this task's "
                         f"changes, staged or committed{written}: inspect the base repo")
            with suppress(Exception):
                on_event("integrate-unverified",
                         f"{task.title}: {where}; the task is not run again automatically")
        try:
            checkpoint()
        except BaseException as err:
            with suppress(Exception):
                on_event(
                    "session-not-saved",
                    f"{task.title}: could not save the session ({err!r}) although the base "
                    f"may already hold this task's merge; before resuming, mark task "
                    f"{task.id} done (done_ids) or unverified in {session_path}",
                )
            if not isinstance(err, Exception):
                raise

    # 1. Discovery — turn the brief into a spec with testable acceptance criteria,
    # which ground the plan and drive VERIFY (skipped on resume / when disabled).
    if discovery and state.spec is None:
        on_event("discovery", "deriving the spec & acceptance criteria")
        try:
            state.spec = discovery_mod.run_discovery(provider, brief, guidance)
            on_event(
                "spec", f"{len(state.spec.acceptance_criteria)} acceptance criteria"
            )
            checkpoint()
        except Exception as exc:
            on_event("skip", f"discovery failed, planning from the brief: {str(exc)[:80]}")

    # 2. Plan — built to satisfy the spec, with project-type non-negotiables guaranteed
    # by the archetype (directives steer the planner; enforce() appends any missing
    # required task as a hard floor). Checkpoint: approve / replan (skipped on resume).
    arch = archetypes_mod.detect(brief, state.spec) if use_archetypes else None
    if arch is not None:
        on_event("archetype", f"{arch.name}: guaranteeing its required tasks")
    directives = archetypes_mod.directives_text(arch) if arch else ""

    def make_plan(extra: str = "") -> Plan:
        g = f"{guidance} {extra}".strip()
        p = planner.plan(provider, brief, g, state.spec, directives)
        if arch is not None:
            for added in archetypes_mod.enforce(p, arch):
                on_event("archetype", f"added required task: {added}")
        return p

    if state.plan is None:
        on_event("plan", "planning the build")
        p = make_plan()
        while True:
            decision = controller.review_plan(p)
            if isinstance(decision, Revise):
                on_event("plan", "replanning with your guidance")
                p = make_plan(decision.guidance)
                continue
            p = decision
            break
        state.plan = p
        checkpoint()

    plan = state.plan
    assert plan is not None
    done = set(state.done_ids)
    # Reflect persisted completion onto the plan's task objects (resume). done_ids is
    # the source of truth: completed tasks stay DONE; everything else resets to PENDING
    # so a task that FAILED in a prior run (e.g. a transient blip) is retried on resume —
    # except UNVERIFIED (merged, result unverified): re-running it would merge it twice.
    for t in plan.tasks:
        if t.id in done:
            t.status = TaskStatus.DONE
        elif t.status == TaskStatus.UNVERIFIED:
            on_event(
                "task-unverified",
                f"{t.title}: may have been merged in an earlier run (unverified): inspect "
                "the base; "
                "add its id to done_ids to accept it (setting it back to pending would run "
                "the task again and merge its changes a second time)",
            )
        else:
            t.status = TaskStatus.PENDING

    def build_ctx(task: Task, target_ws: Path) -> str:
        """Build the task's instruction context and stage its skills into the task's
        workspace. Done in the main thread (before dispatch) so it doesn't race."""
        ctx = (
            f"App: {plan.app_name}\nStack: {plan.stack}\nGoal: {plan.summary}\n"
            f"Already completed: {sorted(done) or 'nothing yet'}\n\n"
            f"Your task: {task.instruction}"
        )
        if library is not None:
            chosen = library.select_for(
                task.role, f"{task.title} {task.instruction}", constants.MAX_SKILLS_PER_TASK
            )
            note = library.stage(chosen, target_ws)
            if note:
                ctx += "\n\n" + note
                on_event("skills", f"{task.title}: {', '.join(s.name for s in chosen)}")
        return ctx

    # Concurrency isolation: each task runs in its own git worktree, merged back on
    # success (serialized here in the main thread). Missing git falls back to a
    # serial shared workspace. Pairs with the native sandbox (security).
    iso = isolation if isolation is not None else constants.ISOLATION
    wt_mgr = WorktreeManager(ws) if iso == "worktree" else None
    isolation_on = bool(wt_mgr and wt_mgr.setup())
    if iso == "worktree" and not isolation_on:
        on_event("isolation", "git unavailable: running serially in shared workspace")
    if isolation_on and wt_mgr:
        wt_mgr.build_id = state.build_id
        # The base history is the record of what this build merged: a task whose merge
        # is there but whose outcome the session lost (e.g. a failed save) is held as
        # unverified, never run or merged again.
        try:
            merged = wt_mgr.merged_tasks(state.build_id)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            # Without the history, a task this build merged could run and merge again.
            raise HistoryUnreadableError(
                f"cannot read the base history to find this build's merges ({exc}); "
                "not resuming — fix the repository and run again"
            ) from exc
        for name in wt_mgr.unreadable_refs:
            on_event("worktree-warning", f"skipped ref {name}: its commit is missing (stale ref)")
        lost = [t for t in plan.tasks if t.status == TaskStatus.PENDING and t.id in merged]
        for t in lost:
            t.status = TaskStatus.UNVERIFIED
            on_event(
                "task-unverified",
                f"{t.title}: this build already merged it in commit {merged[t.id]}, but the "
                "session did not record it — inspect the base; add its id to done_ids to "
                "accept it (it is never run or merged again automatically)",
            )
        if lost:
            checkpoint()
        # Worktrees/branches left by earlier builds are reported for manual cleanup,
        # never deleted. The scans are informational: a failure only warns.
        try:
            legacy = wt_mgr.legacy_resources()
            for item in legacy["paths"] + legacy["refs"]:
                on_event("worktree-legacy", f"{item} left from an older Foundry; clean up manually")
        except Exception as exc:
            on_event("worktree-warning", f"could not scan for legacy worktrees: {exc}")
        try:
            for item in wt_mgr.leftover_resources():
                on_event("worktree-leftover", f"{item} left by an earlier build; clean up manually")
        except Exception as exc:
            on_event("worktree-warning", f"could not scan for leftover worktrees: {exc}")

    # 3. Execute tasks in dependency-ordered WAVES; tasks within a wave run concurrently.
    # State is mutated only here in the main thread as futures complete (no races);
    # the worker cap bounds load; worktrees keep parallel workers from colliding.
    max_parallel = max(1, constants.MAX_PARALLEL) if isolation_on or iso != "worktree" else 1
    reported_unowned = 0
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while True:
            ready = plan.ready_tasks(done)
            if not ready:
                break
            if len(ready) > 1:
                on_event("wave", f"{len(ready)} tasks in parallel (≤{max_parallel} at once)")
            # Prepare the whole wave (worktrees AND contexts) before dispatch: a later
            # failure must not leave an earlier worker running while the build aborts.
            # Whatever happens, every worktree still held is reported (never deleted);
            # each report is suppressed on its own so the original error propagates.
            task_workspaces: dict[str, Path] = {}
            futures: dict[Future[tuple[bool, GateReport | None]], Task] = {}
            try:
                if isolation_on and wt_mgr:
                    for task in ready:
                        task_workspaces[task.id] = wt_mgr.create(task.id)
                contexts: dict[str, str] = {}
                for task in ready:
                    on_event("task", f"[{task.role}] {task.title}")
                    contexts[task.id] = build_ctx(task, task_workspaces.get(task.id, ws))
                for task in ready:
                    task_ws = task_workspaces.get(task.id, ws)
                    fut = pool.submit(
                        _run_task, worker, task, contexts[task.id], task_ws, on_event, constitution
                    )
                    futures[fut] = task
                progressed = False
                for fut in as_completed(futures):
                    task = futures[fut]
                    ok, report = fut.result()
                    if report is not None:
                        state.gates.append(report)
                    unverified = False
                    # Reported only once the outcome is saved: reporting may fail or be
                    # interrupted, and a merged task must never be left PENDING (a resume
                    # would run and merge it again).
                    notes: list[tuple[str, str]] = []
                    saved = False
                    try:
                        if ok and isolation_on and wt_mgr:
                            try:
                                if not wt_mgr.integrate(task.id):
                                    ok = False  # merge conflict — fail loudly, never corrupt
                                    msg = f"{task.title}: changes conflict with the base"
                                    if wt_mgr.last_conflict:
                                        msg += f" ({wt_mgr.last_conflict})"
                                    notes.append(("merge-conflict", msg))
                            except MergeUnverifiedError as exc:
                                ok, unverified = False, True  # merged, but not as checked
                                notes.append(("integrate-unverified", f"{task.title}: {exc}"))
                            except IntegrationError as exc:
                                ok = False
                                # refused: the base was not written; failed: it was, and the
                                # user may commit what was applied — never retry it then.
                                unverified = exc.merge_ran
                                kind = "integrate-failed" if exc.merge_ran else "integrate-refused"
                                notes.append((kind, f"{task.title}: {exc}"))
                            except Exception:
                                # Unexpected: the merge may have landed. Saved as unverified
                                # (never re-run) below, then the build aborts as before.
                                task.status = TaskStatus.UNVERIFIED
                                raise
                        if ok:
                            done.add(task.id)  # done_ids first: resume trusts done_ids
                            state.done_ids = sorted(done)
                            task.status = TaskStatus.DONE
                            progressed = True
                        elif unverified:
                            task.status = TaskStatus.UNVERIFIED
                        else:
                            task.status = TaskStatus.FAILED
                        checkpoint()
                        saved = True
                    except BaseException as exc:
                        # Interrupted (e.g. Ctrl-C) or failed before the outcome was saved:
                        # a task Foundry began writing to the base is saved as done (if
                        # integrate returned) or unverified; any other task stays retryable.
                        if not saved:
                            paths = wt_mgr.written.get(task.id) if wt_mgr is not None else None
                            if paths is not None and task.status is not TaskStatus.DONE:
                                task.status = TaskStatus.UNVERIFIED
                            if task.status in (TaskStatus.DONE, TaskStatus.UNVERIFIED):
                                _save_after_abort(task, exc, paths, notes)
                        raise
                    for kind, msg in notes:
                        on_event(kind, msg)
                    if isolation_on and wt_mgr:
                        _report_retained(wt_mgr.discard(task.id), on_event)
                    if ok:
                        on_event("task-done", task.title)
                    elif unverified:
                        on_event("task-unverified", task.title)
                    else:
                        on_event("task-failed", task.title)
            finally:
                for fut in futures:
                    fut.cancel()  # queued workers must not start after an abort
                if isolation_on and wt_mgr:
                    for task_id in task_workspaces:
                        with suppress(Exception):
                            _report_retained(wt_mgr.discard(task_id), on_event)
                    while reported_unowned < len(wt_mgr.unowned):
                        rec = wt_mgr.unowned[reported_unowned]
                        reported_unowned += 1
                        with suppress(Exception):
                            on_event(
                                "worktree-unowned",
                                f"{rec.describe()} (creation failed; not owned): inspect manually",
                            )
            if not progressed:
                on_event("stuck", "no ready task could be completed; stopping")
                break

    build_complete = bool(plan.tasks) and all(t.status == TaskStatus.DONE for t in plan.tasks)
    goal = f"{plan.app_name}: {plan.summary}"

    # The verifier and the reviewer are called through the same retry as a task's
    # worker: a passing API failure must not fail a build that would have passed.
    checker = _Retrying(worker, on_event)

    # 4. VERIFY phase — run the app and exercise it (browser if available, else HTTP),
    # with a bounded verify→fix loop. A definitive failure (the app runs but is broken)
    # blocks the build, and so does a verifier that did not come back or gave no report;
    # "inconclusive" (it could not start the app here) warns but doesn't fail it.
    # At least one round always runs: a round count of 0 is not a way to skip the step.
    verify_rounds = max(1, constants.VERIFY_MAX_ROUNDS)
    review_rounds = max(1, constants.REVIEW_MAX_ROUNDS)
    if verify and build_complete:
        for rnd in range(1, verify_rounds + 1):
            on_event("verify", f"running & exercising the app (round {rnd})")
            try:
                vr = verify_mod.run_verify(checker, goal, ws, state.spec)
            except Exception as exc:
                # The verifier did not come back, so nothing was checked. Recorded as
                # an error, which blocks 'done'; there is nothing to hand a worker to
                # fix. Running the build again from its session repeats the step.
                state.verify = VerifyReport(
                    verdict="error", summary=f"verify could not run: {str(exc)[:200]}", rounds=rnd
                )
                checkpoint()
                on_event("verify-error", f"verify could not run: {str(exc)[:80]}")
                break
            vr.rounds = rnd
            state.verify = vr
            checkpoint()
            if vr.verdict == "error":
                # The verifier answered without a report: nothing was checked, and
                # there is nothing to hand a worker to fix.
                on_event("verify-error", f"verify gave no report: {vr.summary[:80]}")
                break
            on_event("verify-verdict", f"{vr.verdict} · {len(vr.failures())} failing check(s)")
            if not vr.blocks:
                break
            if rnd < verify_rounds:
                on_event("verify-fix", f"fixing {len(vr.failures())} failing check(s)")
                try:
                    # Retry transient blips here too: without this a rate-limit
                    # hiccup silently burned a whole verify round.
                    _execute_with_retry(
                        worker,
                        system_prompt("generalist", constitution),
                        verify_mod.fix_instruction(vr),
                        ws,
                        on_event,
                        seat="generalist",
                    )
                except Exception as exc:
                    on_event("skip", f"verify-fix failed: {str(exc)[:80]}")
            else:
                break

    # 5. REVIEW phase — multi-axis review with a bounded review→fix loop. A build
    # isn't 'done' until the review approves (no unresolved high-severity findings).
    if review and build_complete:
        for rnd in range(1, review_rounds + 1):
            on_event("review", f"multi-axis review (round {rnd})")
            try:
                rv = review_mod.run_review(checker, goal, ws, constitution)
            except Exception as exc:
                # The reviewer did not come back. Recorded as a review that did not
                # approve, the same as an answer that could not be read, so the build
                # is not 'done'. Running it again from its session repeats the step.
                state.review = Review(
                    verdict="changes_requested",
                    score=0,
                    axes={"review": f"could not run: {str(exc)[:200]}"},
                    rounds=rnd,
                )
                checkpoint()
                on_event("review-error", f"review could not run: {str(exc)[:80]}")
                break
            rv.rounds = rnd
            state.review = rv
            checkpoint()
            blocking = rv.blocking()
            verdict_msg = f"{rv.verdict} · score {rv.score} · {len(blocking)} blocking"
            if not rv.security_corroborated:
                # Surfaced, not just recorded: a field nobody reads is not a control.
                # The build is not blocked (the absence of one tool never hard-blocks
                # another) but the operator must know the axis rests on one opinion.
                verdict_msg += " · SECURITY UNCORROBORATED (SkillWarden did not run)"
            on_event("review-verdict", verdict_msg)
            if rv.approved:
                break
            if rnd < review_rounds and blocking:
                # Repair on the seat that matches the finding, not on whatever the
                # default happens to be — a blocking security finding goes to the
                # top-tier `security` role, which exists precisely for this.
                role = review_mod.fix_role(blocking)
                on_event("review-fix", f"[{role}] fixing {len(blocking)} blocking finding(s)")
                fix = review_mod.fix_instruction(blocking)
                try:
                    _execute_with_retry(
                        worker, system_prompt(role, constitution), fix, ws, on_event, seat=role
                    )
                except Exception as exc:
                    on_event("skip", f"review-fix failed: {str(exc)[:80]}")
            else:
                break

    state.done = (
        build_complete
        and (state.verify is None or not state.verify.blocks)
        and (state.review is None or state.review.approved)
    )
    checkpoint()
    on_event(
        "done",
        f"{len(done)}/{len(plan.tasks)} tasks · verify: "
        f"{state.verify.verdict if state.verify else 'skipped'} · review: "
        f"{state.review.verdict if state.review else 'skipped'}",
    )
    return state
