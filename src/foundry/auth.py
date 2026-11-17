"""The sign-in a build may run on.

Foundry's workers are Claude Code, driven through the Claude Agent SDK. Anthropic asks
that tools built on that SDK use API key authentication and do not offer a claude.ai
sign-in or its rate limits (https://code.claude.com/docs/en/agent-sdk/overview). So the
`foundry build` command refuses to start unless the environment names one of the API
methods the SDK documents: an Anthropic API key, or one of the cloud providers.

Foundry does not read, store or check the key. It only looks at whether a method is
named, before any model is called or any file is written. `engine.build` makes the
same check when it picks the worker or the planner itself, and when it is handed one
of Foundry's own Claude workers or the `claude` command as planner. A caller that
brings a worker and a planner of its own chooses its own sign-in and is not checked.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

API_KEY_VAR = "ANTHROPIC_API_KEY"
# Switches that turn on a cloud provider in Claude Code and the Agent SDK.
PROVIDER_SWITCHES: tuple[str, ...] = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
#: Every variable that names an API method, the key first.
API_AUTH_VARS: tuple[str, ...] = (API_KEY_VAR, *PROVIDER_SWITCHES)

# The documented value is `1`; `true` is accepted as the same thing. Anything else is
# not a sign-in, so that a value Claude Code would ignore is never taken for one.
_ON = frozenset({"1", "true"})


class AuthRefused(RuntimeError):
    """No API sign-in is named in the environment; nothing was started."""


def require_api_auth(env: Mapping[str, str] | None = None) -> str:
    """The name of the API method the environment names, or raise `AuthRefused`.

    An API key counts when it is not blank. A provider switch counts when it is set to
    `1` (or `true`). The key is preferred when several are set."""
    env = os.environ if env is None else env
    if env.get(API_KEY_VAR, "").strip():
        return API_KEY_VAR
    for name in PROVIDER_SWITCHES:
        if env.get(name, "").strip().lower() in _ON:
            return name
    raise AuthRefused(
        "Foundry needs an API sign-in to build: set ANTHROPIC_API_KEY, or switch on one "
        f"of the cloud providers the Claude Agent SDK supports ({', '.join(PROVIDER_SWITCHES)}). "
        "Anthropic asks that tools built on the SDK do not run on a claude.ai sign-in "
        "(see README.md, 'Start'). Nothing was started and the workspace was not touched."
    )
