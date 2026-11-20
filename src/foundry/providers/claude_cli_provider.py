"""Planning provider via the `claude` command (text only, no tools)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile

from foundry import constants, procenv
from foundry.providers.base import LLMProvider


class ClaudeCliProvider(LLMProvider):
    name = "claude_cli"

    def __init__(self) -> None:
        self._bin = shutil.which("claude")

    def is_available(self) -> bool:
        return self._bin is not None

    def complete(self, system: str, user: str, seat: str = "planner") -> str:
        if self._bin is None:
            raise RuntimeError("claude CLI not found on PATH")
        cmd = [self._bin, "-p", "--output-format", "text", "--system-prompt", system]
        model = constants.seat_model(seat, override=constants.CLI_MODEL)
        if model:
            cmd += ["--model", model]
        with tempfile.TemporaryDirectory(prefix="foundry-plan-") as workdir:
            proc = subprocess.run(
                cmd,
                input=user,
                capture_output=True,
                text=True,
                timeout=constants.PLAN_TIMEOUT,
                cwd=workdir,
                env=procenv.clean_env(),
            )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({proc.returncode}): {_failure_reason(proc)}")
        return proc.stdout


def _failure_reason(proc: subprocess.CompletedProcess[str]) -> str:
    """What `claude` said when it failed.

    `claude -p` writes some failures, such as an expired sign-in, to its standard
    output and leaves its error stream empty, so both streams are read.
    """
    parts = [text.strip() for text in (proc.stderr, proc.stdout) if text and text.strip()]
    if not parts:
        return "no output on stdout or stderr"
    return " | ".join(parts)[:300]
