"""One environment policy for every process Foundry spawns.

Workers (`claude`), task gates (`sh -c <verify>`), the planning CLI, SkillWarden and
Foundry's own git plumbing all inherit the environment of whatever launched Foundry
(a shell, a git hook, a CI step). Three classes of `GIT_*` variable in it would
silently change what those processes do, so they are dropped:

* **location** — `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, ... point every git
  command at another repository or index; the working directory decides instead.
* **configuration injection** — `GIT_CONFIG_GLOBAL`/`_SYSTEM`/`_NOSYSTEM`, the
  `GIT_CONFIG_COUNT` + `GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` family and
  `GIT_CONFIG_PARAMETERS` set arbitrary config (e.g. `core.hooksPath`, `core.fsmonitor`)
  that git would then act on, and would undo the merge settings Foundry pins.
* **programs git executes** — `GIT_SSH`/`GIT_SSH_COMMAND`, `GIT_ASKPASS`,
  `GIT_EXEC_PATH`, `GIT_EDITOR`/`GIT_SEQUENCE_EDITOR`/`GIT_PAGER`, `GIT_EXTERNAL_DIFF`,
  `GIT_PROXY_COMMAND`, `GIT_TEMPLATE_DIR` (hooks copied by `git init`) name a command
  git runs with the operator's credentials.
* **pathspec semantics** — `GIT_LITERAL_PATHSPECS`, `GIT_GLOB_PATHSPECS`,
  `GIT_NOGLOB_PATHSPECS`, `GIT_ICASE_PATHSPECS` change what a path argument means.

Everything else is kept on purpose: identity (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`),
`GIT_TERMINAL_PROMPT`, `GIT_SSL_*`, `GIT_TRACE*` and non-git variables express the
operator's intent. A git installation that needs `GIT_EXEC_PATH` must provide a
wrapper `git` on PATH. The policy is deliberately a denylist, defined once here;
`isolation._git_env()` and every `subprocess.run` in Foundry use `clean_env()`,
and the Agent SDK worker (which can only add to the inherited process environment)
calls `scrub_os_environ()` before spawning `claude`.
"""

from __future__ import annotations

import os
import threading

_GIT_LOCATION_VARS: frozenset[str] = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE",
})
_GIT_CONFIG_VARS: frozenset[str] = frozenset({
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
})
_GIT_CONFIG_PREFIXES: tuple[str, ...] = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_GIT_COMMAND_VARS: frozenset[str] = frozenset({
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_EXEC_PATH", "GIT_EDITOR",
    "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_EXTERNAL_DIFF", "GIT_PROXY_COMMAND",
    "GIT_TEMPLATE_DIR",
})
_GIT_PATHSPEC_VARS: frozenset[str] = frozenset({
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
})
# Attributes read from another tree (git >= 2.40) or without the system file would
# change merge drivers and filters under the checks Foundry ran.
_GIT_ATTR_VARS: frozenset[str] = frozenset({"GIT_ATTR_SOURCE", "GIT_ATTR_NOSYSTEM"})

#: Exact names the policy drops (plus the `GIT_CONFIG_KEY_n`/`_VALUE_n` prefixes).
SCRUBBED_VARS: frozenset[str] = (
    _GIT_LOCATION_VARS | _GIT_CONFIG_VARS | _GIT_COMMAND_VARS | _GIT_PATHSPEC_VARS
    | _GIT_ATTR_VARS
)

_lock = threading.Lock()


def is_scrubbed(name: str) -> bool:
    """Whether the policy drops environment variable `name`."""
    return name in SCRUBBED_VARS or name.startswith(_GIT_CONFIG_PREFIXES)


def clean_env(base: os._Environ[str] | dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `base` (default: the process environment) without the policy's
    variables — pass it as `env=` to every subprocess Foundry starts."""
    if base is not None:
        return {k: v for k, v in base.items() if not is_scrubbed(k)}
    with _lock:  # `scrub_os_environ` may be removing names from another thread
        return {k: v for k, v in os.environ.items() if not is_scrubbed(k)}


def scrub_os_environ() -> list[str]:
    """Drop the policy's variables from this process's own environment, for
    spawners that copy `os.environ` themselves (the Agent SDK). Idempotent and safe
    from parallel worker threads. Returns the names removed."""
    with _lock:
        removed = sorted(k for k in os.environ if is_scrubbed(k))
        for name in removed:
            os.environ.pop(name, None)
    return removed
