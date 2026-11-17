"""Project archetypes — planner templates that guarantee non-negotiable tasks per
project type, so quality floors don't depend on the planner's non-determinism.

A live build proved the need: a run whose plan happened to omit a security task
shipped a stored-XSS that REVIEW then under-rated. Archetypes make secure-by-default
a *planning* guarantee — the directives steer the planner, and `enforce()` appends any
still-missing required task as a hard floor — with VERIFY/REVIEW as the safety net
rather than the only net.

Detection is keyword-based over the brief + spec; today there's one archetype
(web-app). Add more (CLI tool, data pipeline, library) as real needs appear.
"""

from __future__ import annotations

from dataclasses import dataclass

from foundry.models import Plan, Spec, Task


@dataclass(frozen=True)
class RequiredTask:
    """A task the archetype guarantees exists; appended if the planner omitted its role."""

    role: str
    title: str
    instruction: str
    verify: str = ""


@dataclass(frozen=True)
class Archetype:
    name: str
    keywords: tuple[str, ...]
    planning_directives: tuple[str, ...]  # injected into the planner prompt
    required_tasks: tuple[RequiredTask, ...] = ()  # appended if their role is missing


WEB_APP = Archetype(
    name="web-app",
    keywords=(
        "web app", "web application", "website", "web site", "frontend", "front-end",
        "fastapi", "flask", "django", "next", "react", "vue", "svelte", "express",
        "rest api", "http", "browser", "dashboard", "portal", "single-page", "spa",
    ),
    planning_directives=(
        "This is a WEB APPLICATION. Beyond the feature work, the plan MUST include:",
        "  - a dedicated security-hardening task (role 'security') that validates and sanitises ALL external "
        "input and protects against injection and XSS — reject unsafe URL schemes (allow only http/https), never "
        "render unsanitised user input into HTML or an href/src, and parameterise every database query;",
        "  - a final end-to-end verification task (role 'tester') that builds the app and runs the full test suite.",
    ),
    required_tasks=(
        RequiredTask(
            role="security",
            title="Security hardening & input sanitisation",
            instruction=(
                "Harden the application against common web vulnerabilities WITHOUT changing intended behaviour. "
                "On the server, validate and sanitise ALL external input: reject empty/whitespace values and unsafe "
                "URL schemes (allow only http/https), parameterise every database query, and ensure no user-supplied "
                "value is rendered into HTML or an href/src without sanitisation (prevent stored/reflected XSS). Add "
                "tests covering these protections (rejecting javascript: URLs and injection attempts, malformed "
                "input). This hardening is non-negotiable per the project constitution."
            ),
        ),
    ),
)

ARCHETYPES = (WEB_APP,)


def detect(brief: str, spec: Spec | None = None) -> Archetype | None:
    """Return the first archetype whose keywords appear in the brief (and spec)."""
    text = brief.lower()
    if spec is not None:
        text += " " + spec.as_context().lower()
    for arch in ARCHETYPES:
        if any(k in text for k in arch.keywords):
            return arch
    return None


def directives_text(arch: Archetype) -> str:
    return "\n".join(arch.planning_directives)


def enforce(plan: Plan, arch: Archetype) -> list[str]:
    """Guarantee the archetype's required tasks exist; append any whose role the planner
    omitted (depending on all existing tasks, so it runs last). Returns the titles added
    so the engine can LOG the augmentation — a quality floor is never applied silently.

    The appended task carries no gate command (the project's test tooling is unknown
    here); its work is exercised by the final verification task and the REVIEW gate,
    which now treats injection/XSS as blocking."""
    present = {t.role for t in plan.tasks}
    added: list[str] = []
    for req in arch.required_tasks:
        if req.role in present:
            continue
        plan.tasks.append(
            Task(
                id=f"t{len(plan.tasks) + 1}",
                title=req.title,
                role=req.role,
                instruction=req.instruction,
                depends_on=[t.id for t in plan.tasks],
                verify=req.verify,
            )
        )
        present.add(req.role)
        added.append(req.title)
    return added
