"""The VERIFY phase — run the built app and exercise it.

A web-app agency must verify the *running* app, not just that unit tests pass. A
verifier worker starts the app, exercises it (in a real browser if the tooling is
available, else at the HTTP level), watches for console/network/runtime errors, then
stops the server — and returns a structured verdict:

  - "pass"          → started and exercised successfully
  - "fail"          → started but is broken (a real defect) — blocks the build, drives a fix loop
  - "inconclusive"  → couldn't be exercised (no start command, no browser/tooling) — warns, doesn't block

"fail" gates the build, and so does "error" (the verifier did not come back, or gave
no report that could be read: nothing was checked). "inconclusive" degrades gracefully
so a build is never falsely failed for an environment that can't run a browser. Converges the browser-
verification ideas from gstack browser-skills, addyosmani browser-testing, and the
agency-agents evidence-collector, raised to the project's standard. The engine drives a
bounded verify→fix loop, mirroring REVIEW.
"""

from __future__ import annotations

from pathlib import Path

from foundry.models import Spec, VerifyCheck, VerifyReport
from foundry.report import last_json_object
from foundry.worker import Worker

VERIFIER_SYSTEM = """You are a QA engineer verifying that an autonomously-built web application in the current \
directory actually RUNS and WORKS — not just that its unit tests pass. Gather evidence by exercising the real app.

Procedure:
1. Determine how to start the app (inspect package.json scripts, README, or the framework's convention). Install \
deps if needed.
2. Start it in the background (capture its logs); wait for it to come up; note the URL/port.
3. Exercise it. If browser-automation tooling is available (Playwright/Puppeteer/a browser MCP), drive the real UI: \
load the main pages, click the primary flows, and capture console errors, failed network requests, and runtime \
exceptions. If no browser tooling is available, fall back to HTTP: request the key routes and check status codes \
and that responses aren't error pages.
4. Stop the server and any processes you started. Leave no orphaned processes or servers running.

Judge by EVIDENCE you actually collected, never by assumption. Verdicts:
  - "pass": the app started and the exercised flows worked (no blocking errors).
  - "fail": the app started but is broken — failed to load, console/runtime errors, broken primary flow, 5xx/error \
pages on key routes. Each defect is a check with ok=false.
  - "inconclusive": you could NOT start or exercise it (no start command, missing tooling, won't boot here). Do not \
guess — report inconclusive with the reason.

Do NOT modify project source files — verify only. Respond with JSON ONLY (no prose, no code fence):
{"verdict": "pass" | "fail" | "inconclusive", "summary": "what you did and found",
 "checks": [{"name": "server starts" | "homepage loads" | "<route/flow>" | ..., "ok": true|false, "detail": "evidence"}]}"""


def run_verify(worker: Worker, goal: str, workspace: Path, spec: Spec | None = None) -> VerifyReport:
    instruction = f"Verify this running app. Goal: {goal}"
    if spec is not None and spec.acceptance_criteria:
        criteria = "\n".join(f"  - [{c.id}] {c.statement}" for c in spec.acceptance_criteria)
        instruction += (
            "\n\nThe app MUST satisfy these acceptance criteria — exercise the running app to "
            "check EACH one and report a check per criterion (name it by its id), marking ok "
            f"only on collected evidence. A criterion you cannot satisfy is a failure:\n{criteria}"
        )
    raw = worker.execute(VERIFIER_SYSTEM, instruction, workspace, seat="verify")
    return _parse(raw)


def _parse(raw: str) -> VerifyReport:
    # No report at all is not "inconclusive" (a verifier saying it could not start the
    # app here): nothing was checked, so it is an error, and an error blocks.
    data = last_json_object(raw)
    if data is None:
        return VerifyReport(verdict="error", summary="the verifier gave no report")
    checks = [
        VerifyCheck(
            name=str(c.get("name", "")),
            ok=bool(c.get("ok", False)),
            detail=str(c.get("detail", "")),
        )
        for c in (data.get("checks") or [])
        if str(c.get("name", "")).strip()
    ]
    raw_verdict = str(data.get("verdict", "")).lower()
    # A report without one of the three verdicts says nothing: an error, like no report.
    verdict = raw_verdict if raw_verdict in ("pass", "fail", "inconclusive") else "error"
    # A failed check is a failure whatever the verdict beside it says: trust the evidence.
    if verdict != "error" and any(not c.ok for c in checks):
        verdict = "fail"
    return VerifyReport(verdict=verdict, summary=str(data.get("summary", "")), checks=checks)


def fix_instruction(report: VerifyReport) -> str:
    lines = [
        "Verification of the running app FAILED. The app builds but does not work correctly. "
        "Fix the cause of these failures so the app runs and the flows work, then stop:"
    ]
    for c in report.failures():
        lines.append(f"  - {c.name}: {c.detail}")
    if report.summary:
        lines.append(f"\nVerifier summary: {report.summary}")
    return "\n".join(lines)
