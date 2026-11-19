"""The trust boundary of a build that is started without `foundry sandbox`.

Outside a sandbox, workers run `claude` with the operator's credentials, filesystem
and network (see README, "Before you run it"), so a build must not run on behalf of
input it cannot trust unless something provides a boundary. Two modes exist, and
nothing in between:

* **sandbox** — `foundry sandbox` (see `sandbox/`), or a container/VM wrapper of the
  operator's own, attests it with `--sandbox NAME` or `FOUNDRY_SANDBOX=NAME`. The
  name is recorded and reported; Foundry does not (and cannot) check it. Set it only
  from the wrapper, never in a shell profile.
* **trusted-operator** — an operator at a terminal runs a build of their own brief
  in their own workspace, accepting that the workers act as them.

Any unattended indicator without an attestation — stdin that is not a terminal
(cron, CI, a pipe, a service) or an explicit `--unattended` — is refused before a
worker is started or the workspace is written. `engine.build` resolves the decision
itself when the caller does not pass one, so library use is covered too.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal

SANDBOX_ENV = "FOUNDRY_SANDBOX"
README_SECTION = "README.md, 'Before you run it'"


class TrustRefused(RuntimeError):
    """The build would run unattended on a bare host; nothing was started."""


@dataclass(frozen=True)
class Trust:
    mode: Literal["sandbox", "trusted-operator"]
    sandbox: str = ""

    @property
    def statement(self) -> str:
        if self.mode == "sandbox":
            return (
                f"sandbox={self.sandbox} (attested by the wrapper; Foundry does not verify "
                "it): workers run inside that boundary with whatever it grants them"
            )
        return (
            "trusted-operator mode: workers run on this host with YOUR credentials, "
            "files and network, unsandboxed; only build briefs, plans and workspaces you "
            f"trust: untrusted input needs a sandbox wrapper (see {README_SECTION})"
        )


def _stdin_is_tty() -> bool:
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def resolve(
    *, sandbox: str | None = None, unattended: bool = False, interactive: bool | None = None
) -> Trust:
    """Decide the mode for this run or raise `TrustRefused`.

    `sandbox` (the `--sandbox` option) wins over `FOUNDRY_SANDBOX`; a blank value is
    no attestation. `interactive` defaults to whether stdin is a terminal."""
    name = (sandbox if sandbox is not None else os.environ.get(SANDBOX_ENV, "")).strip()
    if name:
        return Trust("sandbox", name)
    if interactive is None:
        interactive = _stdin_is_tty()
    if unattended or not interactive:
        why = "--unattended was given" if unattended else "stdin is not a terminal"
        raise TrustRefused(
            f"Foundry refuses to run unattended on a bare host ({why}, and no sandbox is "
            "attested): its workers would run with the operator's credentials, files and "
            "network. Run it with `foundry sandbox build`, or inside a container/VM wrapper "
            f"that sets --sandbox NAME or {SANDBOX_ENV}=NAME, or run it yourself at a "
            f"terminal as the trusted operator (see {README_SECTION}). Nothing was started "
            "and the workspace was not touched."
        )
    return Trust("trusted-operator")
