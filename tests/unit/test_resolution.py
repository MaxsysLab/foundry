"""Provider resolution + role prompts."""

from __future__ import annotations

import pytest

from foundry.providers.anthropic_provider import AnthropicProvider
from foundry.providers.base import get_provider
from foundry.providers.claude_cli_provider import ClaudeCliProvider
from foundry.roles import ROLES, system_prompt


def test_named_providers():
    assert isinstance(get_provider("anthropic"), AnthropicProvider)
    assert isinstance(get_provider("claude_cli"), ClaudeCliProvider)


def test_auto_prefers_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(get_provider("auto"), AnthropicProvider)


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("nope")


def test_role_prompt_includes_specialty_and_base():
    p = system_prompt("backend")
    assert "Foundry, an automated software agency" in p  # base operating rules
    assert "server/API" in p  # backend specialty
    # unknown role falls back to generalist
    assert "full-stack" in system_prompt("does-not-exist")


def test_every_role_has_a_prompt():
    for role in ROLES:
        assert system_prompt(role).strip()
