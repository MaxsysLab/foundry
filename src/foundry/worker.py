"""Build workers — the executor primitive.

A worker runs one task to completion in the workspace (create/edit files, run
commands) and returns its report. Two backends behind one interface:

  * AgentSdkWorker (default) — the Claude Agent SDK's in-process agent loop.
    Structured sessions, typed results/errors, and native permission/sandbox
    controls. The recommended native foundation.
  * CliBuildWorker — drives `claude -p` as a subprocess. The original, verified
    backend; kept as a fallback (FOUNDRY_WORKER_BACKEND=cli).

Both run as their own process and operate on a workspace directory; point workers
at a dedicated workspace (treat it as a sandbox). Select via FOUNDRY_WORKER_BACKEND.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from foundry import constants, procenv


class TransientWorkerError(RuntimeError):
    """A retryable worker failure — a transient API error (rate-limit/overload/5xx),
    timeout, or connection blip — as opposed to a genuine, deterministic failure.
    The engine retries these with backoff rather than failing the task outright."""


# Signatures of transient infrastructure failures in an error string. The Agent SDK
# reports an HTTP API error as a result with is_error=True and subtype "success"
# (its `api_error_status` carries the code), which it stringifies as the misleading
# "error result: success" — so that phrase is treated as transient too.
_TRANSIENT_MARKERS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "error result: success",
    "429",
    "500",
    "502",
    "503",
    "504",
    "529",
    "connection",
    "timed out",
    "timeout",
)


def _looks_transient(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


class Worker(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def execute(
        self, system: str, instruction: str, workspace: Path, seat: str = "generalist"
    ) -> str:
        """Run one task to completion in `workspace`; return the worker's report.
        `seat` selects the model tier allocated to that seat (role name, "review", "verify")."""


class AgentSdkWorker(Worker):
    name = "agent_sdk"

    def is_available(self) -> bool:
        # The SDK drives the local Claude Code binary, with its sign-in.
        return shutil.which("claude") is not None

    def execute(
        self, system: str, instruction: str, workspace: Path, seat: str = "generalist"
    ) -> str:
        # Engine runs tasks in parallel threads, so the model is resolved per call
        # (never stored on the shared worker); a fresh event loop per call is fine.
        model = constants.seat_model(seat, override=constants.WORKER_MODEL)
        # The SDK spawns `claude` with a copy of os.environ that its `env` option can
        # only add to, so the inherited GIT_* variables are dropped from the process.
        procenv.scrub_os_environ()
        return asyncio.run(self._run(system, instruction, Path(workspace), model))

    async def _run(self, system: str, instruction: str, workspace: Path, model: str) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKError,
            ResultMessage,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            system_prompt=system,
            cwd=str(workspace),
            permission_mode=constants.WORKER_PERMISSION_MODE,  # type: ignore[arg-type]
            allowed_tools=list(constants.WORKER_TOOLS),
            model=model or None,
            max_turns=constants.WORKER_MAX_TURNS,
        )

        result_text: str | None = None
        is_error = False
        api_status: int | None = None
        texts: list[str] = []

        async def consume() -> None:
            nonlocal result_text, is_error, api_status
            async for message in query(prompt=instruction, options=options):
                if isinstance(message, ResultMessage):
                    result_text = message.result
                    is_error = bool(message.is_error)
                    api_status = getattr(message, "api_error_status", None)
                elif isinstance(message, AssistantMessage):
                    texts.extend(b.text for b in message.content if isinstance(b, TextBlock))

        try:
            await asyncio.wait_for(consume(), timeout=constants.WORKER_TIMEOUT)
        except TimeoutError as exc:
            # A timeout is treated as transient — a retry may land in calmer conditions.
            raise TransientWorkerError(
                f"worker timed out after {constants.WORKER_TIMEOUT}s"
            ) from exc
        except ClaudeSDKError as exc:
            # The SDK already saw the structured result before raising the trailing
            # ProcessError; an api_error_status (or a transient-looking message) means a
            # transient API error (rate-limit/overload/5xx) — retryable, not a real failure.
            if api_status is not None or _looks_transient(str(exc)):
                raise TransientWorkerError(f"transient worker/API error: {str(exc)[:200]}") from exc
            raise RuntimeError(f"worker SDK error: {str(exc)[:200]}") from exc

        report = result_text if result_text is not None else "\n".join(texts)
        if is_error:
            if api_status is not None or _looks_transient(report or ""):
                raise TransientWorkerError(f"transient API error (HTTP {api_status or '?'})")
            raise RuntimeError(f"worker reported failure: {(report or '')[:300]}")
        return report or ""


class CliBuildWorker(Worker):
    name = "claude_cli"

    def __init__(self) -> None:
        self._bin = shutil.which("claude")

    def is_available(self) -> bool:
        return self._bin is not None

    def execute(
        self, system: str, instruction: str, workspace: Path, seat: str = "generalist"
    ) -> str:
        if self._bin is None:
            raise RuntimeError("claude CLI not found on PATH")
        model = constants.seat_model(seat, override=constants.WORKER_MODEL)
        cmd = [
            self._bin,
            "-p",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",  # the SDK backend replaces this with permission modes
            "--system-prompt",
            system,
        ]
        if model:
            cmd += ["--model", model]
        proc = subprocess.run(
            cmd,
            input=instruction,
            capture_output=True,
            text=True,
            timeout=constants.WORKER_TIMEOUT,
            cwd=str(workspace),
            env=procenv.clean_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"worker failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        return proc.stdout


def get_worker(backend: str = "") -> Worker:
    backend = backend or constants.WORKER_BACKEND
    if backend in ("sdk", "agent", "agent_sdk"):
        return AgentSdkWorker()
    if backend in ("cli", "subprocess", "claude_cli"):
        return CliBuildWorker()
    raise ValueError(f"Unknown worker backend: {backend!r}")
