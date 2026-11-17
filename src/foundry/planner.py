"""Planner — turns a brief into a dependency-ordered build Plan.

The architect role of the agency: pick a stack and decompose the work into
self-contained, verifiable tasks. Each task's instruction is all the worker sees
(plus the plan summary), so it must be detailed; each `verify` is how the gate
proves the task is actually done.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foundry.models import Plan, Spec, Task
from foundry.providers.base import LLMProvider
from foundry.roles import ROLES

_SYSTEM = f"""You are a principal software architect planning a fully autonomous build.
Given a product brief, choose a modern, appropriate web stack and produce a build PLAN as JSON ONLY \
(no prose, no code fence):
{{"app_name": "...", "stack": "the chosen stack, plus one clause on why THIS brief warrants it",
  "summary": "one paragraph on the app and approach",
  "tasks": [
    {{"id": "t1", "title": "short title", "role": "one of the roles",
      "instruction": "detailed, self-contained instruction for the worker",
      "depends_on": [], "verify": "shell command exiting 0 when correct, or empty string"}}
  ]}}
Rules:
- Roles available: {", ".join(ROLES)}.
- Choose the stack FROM THIS BRIEF. Derive it from what the requirements actually need \
— persistence shape, interactivity, auth, real-time, deployment target, scale — and name that \
reason in the `stack` field. Do not default to a familiar stack because it is familiar; a stack \
you cannot justify from the brief is the wrong stack. Boring and well-matched beats fashionable, \
and a smaller stack that meets every requirement beats a larger one that exceeds them.
- The FIRST task must be an `architect` task that scaffolds the project with a passing baseline (build + a trivial test).
- Order tasks by dependency via `depends_on` (task ids). Keep the graph correct.
- Each `instruction` is self-contained: the worker sees only it plus the plan summary.
- Prefer `verify` commands that truly check the work (build, typecheck, tests) using the project's own tooling. A final task should verify the whole app builds and its tests pass.
- Complete but minimal: every task moves toward a working, production-grade app.
- When a spec with acceptance criteria is given, the plan MUST cover every acceptance criterion — each criterion should be implemented by some task and provable by a `verify` command. Do not leave a criterion unaddressed."""


def _json(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        parsed: dict[str, Any] = json.loads(match.group(0))
        return parsed
    except json.JSONDecodeError:
        return {}


def plan(
    provider: LLMProvider,
    brief: str,
    guidance: str = "",
    spec: Spec | None = None,
    archetype_directives: str = "",
) -> Plan:
    user = f"Brief: {brief}" + (f"\n\nConstraints/guidance: {guidance}" if guidance else "")
    if spec is not None:
        user += f"\n\nDiscovery spec — plan to satisfy ALL of this:\n{spec.as_context()}"
    if archetype_directives:
        user += f"\n\nProject-type requirements (non-negotiable):\n{archetype_directives}"
    data = _json(provider.complete(_SYSTEM, user, seat="planner"))
    tasks: list[Task] = []
    for i, t in enumerate(data.get("tasks") or [], 1):
        instruction = str(t.get("instruction", "")).strip()
        if not instruction:
            continue
        tasks.append(
            Task(
                id=str(t.get("id") or f"t{i}"),
                title=str(t.get("title", "")).strip() or f"Task {i}",
                role=str(t.get("role", "generalist")).strip() or "generalist",
                instruction=instruction,
                depends_on=[str(d) for d in (t.get("depends_on") or [])],
                verify=str(t.get("verify", "")).strip(),
            )
        )
    return Plan(
        app_name=str(data.get("app_name", "")).strip() or "app",
        stack=str(data.get("stack", "")).strip() or "unspecified",
        summary=str(data.get("summary", "")).strip(),
        tasks=tasks,
    )
