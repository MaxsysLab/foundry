"""Core data models for Foundry.

A Brief becomes a Plan (stack + an ordered, dependency-aware list of Tasks). Each
Task is executed by a role-specialized headless Claude worker in the workspace,
then verified by a quality gate; it isn't 'done' until its gate passes. BuildState
is the resumable record of the whole run.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    # The base may hold the task's changes (Foundry started writing them, or the
    # merge landed but differs from the checked one), or integration failed
    # unexpectedly: kept across resumes (never re-run or re-merged) until the
    # operator decides.
    UNVERIFIED = "unverified"


class AcceptanceCriterion(BaseModel):
    """A testable statement of what the finished product must do — the contract
    the VERIFY phase exercises against the running app."""

    id: str
    statement: str  # e.g. "A user can add a bookmark and see it appear in the list"


class Spec(BaseModel):
    """The Discovery output: the brief turned into explicit requirements and
    testable acceptance criteria, before any planning. Grounds the plan and drives
    VERIFY — build the *right* thing, and prove it does what was asked."""

    goal: str = ""
    requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    notes: str = ""

    def as_context(self) -> str:
        """Render the spec for injection into the planner / verifier prompts."""
        lines = [f"Product goal: {self.goal}"] if self.goal else []
        if self.requirements:
            lines.append("Requirements:")
            lines += [f"  - {r}" for r in self.requirements]
        if self.acceptance_criteria:
            lines.append("Acceptance criteria (the app MUST satisfy each):")
            lines += [f"  - [{c.id}] {c.statement}" for c in self.acceptance_criteria]
        if self.out_of_scope:
            lines.append("Out of scope:")
            lines += [f"  - {o}" for o in self.out_of_scope]
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        return "\n".join(lines)


class Task(BaseModel):
    id: str
    title: str
    role: str  # which role executes it (architect, frontend, backend, ...)
    instruction: str  # what the worker must accomplish
    depends_on: list[str] = Field(default_factory=list)
    verify: str = ""  # shell command that must exit 0 for the task to pass (optional)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    summary: str = ""  # worker's report of what it did


class Plan(BaseModel):
    app_name: str
    stack: str  # e.g. "Next.js 15 + TypeScript + Postgres"
    summary: str
    tasks: list[Task] = Field(default_factory=list)

    def ready_tasks(self, done_ids: set[str]) -> list[Task]:
        """Not-yet-done, non-failed tasks whose dependencies are all satisfied.
        `done_ids` is the source of truth for completion (status is bookkeeping)."""
        return [
            t
            for t in self.tasks
            if t.id not in done_ids
            and t.status == TaskStatus.PENDING
            and set(t.depends_on) <= done_ids
        ]


class GateReport(BaseModel):
    name: str
    command: str
    passed: bool
    output: str = ""


class ReviewFinding(BaseModel):
    axis: str  # correctness | readability | architecture | security | performance
    severity: str  # high | medium | low
    file: str = ""
    issue: str
    recommendation: str = ""


class Review(BaseModel):
    """A multi-axis code review of the built project (the REVIEW phase)."""

    verdict: str = "changes_requested"  # approve | changes_requested
    score: int = 0  # 0–100 health
    axes: dict[str, str] = Field(default_factory=dict)  # one-line summary per axis
    findings: list[ReviewFinding] = Field(default_factory=list)
    rounds: int = 0  # how many review→fix rounds ran
    # False when SkillWarden could not be run (absent, crashed, unparseable output),
    # so the security axis rests on the reviewer alone. Recorded rather than assumed:
    # an absent check is not a passed check.
    security_corroborated: bool = False

    @property
    def approved(self) -> bool:
        return self.verdict == "approve" and not self.blocking()

    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity == "high"]


class VerifyCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class VerifyReport(BaseModel):
    """Result of running the built app and exercising it (the VERIFY phase)."""

    verdict: str = "inconclusive"  # pass | fail | inconclusive | error
    summary: str = ""
    checks: list[VerifyCheck] = Field(default_factory=list)
    rounds: int = 0

    @property
    def blocks(self) -> bool:
        # A definitive failure (the app runs but is broken) blocks, and so does
        # "error": the verifier itself did not come back, so nothing was checked.
        # "inconclusive" (the verifier ran but could not start the app here, or had
        # no browser tooling) warns and does not block.
        return self.verdict in ("fail", "error")

    def failures(self) -> list[VerifyCheck]:
        return [c for c in self.checks if not c.ok]


class BuildState(BaseModel):
    """Resumable record of a build run."""

    schema_version: int = 1
    # Random id stamped into this build's merge commits, so a resume can find its own
    # merges in the base history even when the session could not be saved after one.
    build_id: str = ""
    brief: str
    workspace: str
    guidance: str = ""
    spec: Spec | None = None
    plan: Plan | None = None
    done_ids: list[str] = Field(default_factory=list)
    gates: list[GateReport] = Field(default_factory=list)
    verify: VerifyReport | None = None
    review: Review | None = None
    done: bool = False
