"""Planning LLM provider.

Used for plan/architecture generation (text in, text out). The build *executor*
is separate (worker.py) — it drives the headless `claude` CLI with file tools.
`auto` uses the API when a key is set, else the `claude` command.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def complete(self, system: str, user: str, seat: str = "planner") -> str:
        """One planning completion; `seat` selects the model tier allocated to that seat."""


def get_provider(name: str) -> LLMProvider:
    if name == "auto":
        from foundry.providers.anthropic_provider import AnthropicProvider
        from foundry.providers.claude_cli_provider import ClaudeCliProvider

        if os.getenv("ANTHROPIC_API_KEY"):
            return AnthropicProvider()
        cli = ClaudeCliProvider()
        return cli if cli.is_available() else AnthropicProvider()
    if name in ("anthropic", "claude", "api"):
        from foundry.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name in ("claude_cli", "cli"):
        from foundry.providers.claude_cli_provider import ClaudeCliProvider

        return ClaudeCliProvider()
    raise ValueError(f"Unknown provider: {name!r}")
