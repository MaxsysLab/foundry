"""Quality gates: the project's standard, enforced.

A task isn't 'done' until its verification command exits 0 (build succeeds, tests
pass, lint clean). On failure the engine feeds the gate output back to the worker
to fix and re-runs the gate. This is what separates Foundry from autonomous slop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from foundry import constants, procenv
from foundry.models import GateReport


def run_gate(name: str, command: str, workspace: Path) -> GateReport:
    """Run a verification command in the workspace; pass == exit 0."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=constants.GATE_TIMEOUT,
            cwd=str(workspace),
            env=procenv.clean_env(),
        )
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-2000:]
        return GateReport(name=name, command=command, passed=proc.returncode == 0, output=tail)
    except subprocess.TimeoutExpired:
        return GateReport(name=name, command=command, passed=False, output="gate timed out")
