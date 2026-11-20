"""Vet a skill library with SkillWarden — the 'vet' in adopt → vet → upgrade.

Before Foundry adopts an external skill library, scan every skill bundle with
SkillWarden (https://github.com/MaxsysLab/skillwarden) and surface which are
clean vs risky. Decoupled via the `skillwarden` CLI; returns None (caller reports
it) if SkillWarden isn't installed, so a missing scanner never hard-blocks a build.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from foundry import constants, procenv
from foundry.models import ReviewFinding


class VetResult(BaseModel):
    skill: str
    path: str
    label: str  # info | low | medium | high | critical | error
    score: int
    complete: bool  # SkillWarden covered every input; anything less is not a clearance
    ok: bool  # safe to adopt: a complete scan below the high/critical bar


def skillwarden_available() -> bool:
    return shutil.which(constants.SKILLWARDEN_CMD) is not None


def _scan_complete(data: dict[str, object]) -> bool:
    """SkillWarden's own rule: only a `complete` scan is a clearance. An unreadable file,
    a failed analyzer or a dropped input makes it `incomplete`, and its label then rests
    on partial evidence. A missing status is treated as incomplete: fail closed."""
    return data.get("status") == "complete"


def _scan(binary: str, path: str | Path, *options: str) -> dict[str, Any] | None:
    """One SkillWarden scan of `path` as its JSON report, or None when it could not
    be made or its output is not a report."""
    try:
        proc = subprocess.run(
            [binary, "scan", str(path), "--no-llm", "--format", "json", *options],
            capture_output=True,
            text=True,
            timeout=constants.SKILLWARDEN_TIMEOUT,
            env=procenv.clean_env(),
        )
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def critical_findings(path: str | Path) -> list[ReviewFinding] | None:
    """SkillWarden's CRITICAL findings as security-axis review findings.

    CRITICAL-only keeps app-code false positives out (SkillWarden is tuned for skill
    bundles).

    Returns `None` when the scan could not be performed — SkillWarden absent, or it
    crashed, timed out, or emitted unparseable output. `[]` means it ran and found
    nothing. The distinction is the point: this used to return `[]` for both, so a
    crashed scanner was indistinguishable from a clean scan and the security axis
    quietly lost its corroboration. A security
    check that cannot run must say so, not report silence as safety.

    The same holds for a scan that ran but did not cover everything (SkillWarden
    `status: incomplete`, for example a file it could not read): with no CRITICAL
    finding it returns `None`, because the silence rests on partial evidence. CRITICAL
    findings it did make are still returned, since they block on their own.
    """
    binary = shutil.which(constants.SKILLWARDEN_CMD)
    if binary is None:
        return None
    # The workspace is the operator's own git checkout, so SkillWarden is asked to
    # leave out the `.git` folder at its top. A SkillWarden that does not know the
    # option prints no report, and is asked again without it.
    data = _scan(binary, path, "--skip-git-dir")
    if data is None:
        data = _scan(binary, path)
    if data is None:
        return None
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return None
    out: list[ReviewFinding] = []
    for f in findings:
        if str(f.get("severity")) != "critical":
            continue
        out.append(
            ReviewFinding(
                axis="security",
                severity="high",
                file=str(f.get("file") or ""),
                issue=f"SkillWarden {f.get('rule_id', '')}: {f.get('title', '')}",
                recommendation=str(f.get("remediation") or "Review and remediate."),
            )
        )
    if not out and not _scan_complete(data):
        return None
    return out


def committed_critical_findings(path: str | Path) -> list[ReviewFinding] | None:
    """Scan HEAD's blobs in isolation, never workspace edits or retained worktrees.

    Read blobs directly: git archive's export-ignore attributes can omit committed
    files. A symlink is scanned as its committed target text, never followed. A
    submodule or an unreadable tree cannot provide complete corroboration.
    """

    def git_bytes(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            timeout=constants.SKILLWARDEN_TIMEOUT,
            env=procenv.clean_env(),
        ).stdout

    try:
        tree = git_bytes("ls-tree", "-rz", "--full-tree", "HEAD")
        with tempfile.TemporaryDirectory(prefix="foundry-scan-") as directory:
            snapshot = Path(directory)
            for entry in tree.split(b"\0"):
                if not entry:
                    continue
                metadata, name = entry.split(b"\t", 1)
                _mode, kind, oid = metadata.split()
                if kind != b"blob":
                    return None
                relative = PurePosixPath(os.fsdecode(name))
                if relative.is_absolute() or ".." in relative.parts:
                    return None
                target = snapshot.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(git_bytes("cat-file", "blob", oid.decode("ascii")))
            findings = critical_findings(snapshot)
            if findings is not None:
                for finding in findings:
                    file = Path(finding.file)
                    if file.is_absolute() and file.is_relative_to(snapshot):
                        finding.file = file.relative_to(snapshot).as_posix()
            return findings
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def vet_library(root: str | Path) -> list[VetResult] | None:
    """Scan each skill bundle under `root` with SkillWarden. None if unavailable."""
    binary = shutil.which(constants.SKILLWARDEN_CMD)
    if binary is None:
        return None
    results: list[VetResult] = []
    for skill_md in sorted(Path(root).expanduser().rglob("SKILL.md")):
        if ".git" in skill_md.parts:
            continue
        bundle = skill_md.parent
        try:
            proc = subprocess.run(
                [binary, "scan", str(bundle), "--no-llm", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=constants.SKILLWARDEN_TIMEOUT,
                env=procenv.clean_env(),
            )
            data = json.loads(proc.stdout)
            label = str(data.get("severity_label", "error"))
            score = int(data.get("risk_score", 0))
            complete = _scan_complete(data)
        except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            label, score, complete = "error", 0, False
        results.append(
            VetResult(
                skill=bundle.name,
                path=str(bundle),
                label=label,
                score=score,
                complete=complete,
                ok=complete and label not in ("high", "critical", "error"),
            )
        )
    return results
