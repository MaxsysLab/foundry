"""Planning provider via the Claude API (production path)."""

from __future__ import annotations

import os
from typing import Any

from foundry import constants
from foundry.providers.base import LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        self._client: Any = None

    def is_available(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY"))

    def complete(self, system: str, user: str, seat: str = "planner") -> str:
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic()
        resp = self._client.messages.create(
            model=constants.seat_model(seat, api=True, override=constants.ANTHROPIC_MODEL),
            max_tokens=constants.LLM_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")
