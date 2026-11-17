"""The REVIEW phase — multi-axis code review of the built project.

Adapted from Addy Osmani's code-review methodology to a project standard: a
reviewer worker inspects the workspace across five axes — correctness, readability,
architecture, security, performance — and returns a structured verdict. SkillWarden
corroborates the security axis (CRITICAL findings only). The engine drives a bounded
review→fix loop; an unresolved high-severity finding blocks the build from 'done'.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from foundry import security
from foundry.models import Review, ReviewFinding
from foundry.report import last_json_object
from foundry.worker import Worker

REVIEWER_SYSTEM = """You are a principal engineer performing the final code review of an autonomously-built \
project located in the current directory. Review across five axes: correctness, readability & simplicity, \
architecture, security, and performance.

Standard: approve when the code achieves the goal and follows the project's conventions, even if imperfect. \
Request changes only for real problems — bugs, broken build/tests, security holes, or significant design flaws — \
not style nits. Mark a finding "high" only if it should block merge.

Security severity is NOT negotiable: any vulnerability that allows code execution, injection (SQL, command, \
XSS/stored-or-reflected script), authentication/authorization bypass, path traversal, SSRF, or exposure of \
secrets/sensitive data is ALWAYS "high" and blocking — even if the surrounding code is otherwise good and even \
if it isn't named in the acceptance criteria. Unsanitised external input that reaches a sink (a rendered href/HTML, \
a query, a shell, a filesystem path) is a high-severity finding. Do not downgrade these to medium.

Inspect the codebase with your tools (read files; run the build/tests if useful). Do NOT modify any files — \
review only. Respond with JSON ONLY (no prose, no code fence):
{"verdict": "approve" | "changes_requested", "score": 0-100,
 "axes": {"correctness": "...", "readability": "...", "architecture": "...", "security": "...", "performance": "..."},
 "findings": [{"axis": "...", "severity": "high|medium|low", "file": "...", "issue": "...", "recommendation": "..."}]}"""


def run_review(worker: Worker, goal: str, workspace: Path, constitution: str = "") -> Review:
    system = REVIEWER_SYSTEM
    if constitution:
        system += (
            "\n\nThe project is bound by this constitution — check adherence as part of every "
            f"axis, and raise a finding for any violation:\n\n{constitution}"
        )
    raw = worker.execute(system, f"Review this project. Goal: {goal}", workspace, seat="review")
    review = _parse(raw)
    # Corroborate the security axis with SkillWarden's CRITICAL findings (tools
    # composing). None = the scan could not be performed, which is NOT a clean scan:
    # record that the corroboration is missing so the verdict is read with that in
    # mind, rather than silently crediting an absent check.
    sw = security.critical_findings(workspace)
    if sw is None:
        review.security_corroborated = False
    else:
        review.security_corroborated = True
        if sw:
            review.findings.extend(sw)
            review.verdict = "changes_requested"  # a real critical can't be "approve"
    return review


def _parse(raw: str) -> Review:
    data = last_json_object(raw)
    if data is None:
        return Review(verdict="changes_requested", score=0)
    findings = [
        ReviewFinding(
            axis=str(f.get("axis", "")),
            severity=str(f.get("severity", "low")).lower(),
            file=str(f.get("file", "")),
            issue=str(f.get("issue", "")),
            recommendation=str(f.get("recommendation", "")),
        )
        for f in (data.get("findings") or [])
        if str(f.get("issue", "")).strip()
    ]
    verdict = "approve" if str(data.get("verdict")) == "approve" else "changes_requested"
    axes = {str(k): str(v) for k, v in (data.get("axes") or {}).items()}
    try:
        score = int(data.get("score", 0))
    except (ValueError, TypeError):
        score = 0
    return Review(verdict=verdict, score=score, axes=axes, findings=findings)


def fix_instruction(blocking: list[ReviewFinding]) -> str:
    lines = ["Code review requested changes. Fix these blocking findings, then stop:"]
    for f in blocking:
        loc = f" in {f.file}" if f.file else ""
        lines.append(f"  - [{f.axis}/{f.severity}]{loc}: {f.issue} → {f.recommendation}")
    return "\n".join(lines)


def fix_role(blocking: list[ReviewFinding]) -> str:
    """The role that should repair these findings — NOT always the generalist.

    A top-tier reviewer raising a blocking security finding used to hand it to a
    mid-tier worker wearing the generalist prompt, at the one moment quality matters
    most: `security` is a top-tier, never-downgrade seat and was simply not used on
    the fix path.

    Only the security axis gets special routing, deliberately. It is the one axis
    with a dedicated top-tier never-downgrade seat and the one whose severity the
    reviewer prompt forbids negotiating down. Mapping the other axes to specialists
    was tried and reverted: `tester`'s specialty is *writing tests*, so routing a
    correctness bug there invites a test for the bug rather than a fix. `generalist`
    — full-stack, implements across whatever layers the task touches — is the right
    repairer for correctness, readability, architecture and performance work, and
    guessing otherwise trades a real seat mismatch for an invented one.
    """
    if any(f.axis.strip().lower() == "security" for f in blocking):
        return "security"
    return "generalist"


# Progress sink type (matches the engine's EventFn).
EventFn = Callable[[str, str], None]
