"""The project constitution: resolution, worker injection, review enforcement."""

from __future__ import annotations

from foundry import constitution
from foundry.roles import system_prompt


def test_default_when_nothing_provided():
    assert constitution.load() == constitution.DEFAULT_CONSTITUTION
    assert "non-negotiable" in constitution.DEFAULT_CONSTITUTION.lower()


def test_override_path_wins(tmp_path):
    f = tmp_path / "constitution.md"
    f.write_text("# Custom\nOnly rule: ship Fridays.")
    assert "ship Fridays" in constitution.load(override_path=str(f))


def test_workspace_constitution_used(tmp_path):
    (tmp_path / "constitution.md").write_text("# Project rules\nUse Postgres only.")
    assert "Postgres only" in constitution.load(workspace=tmp_path)


def test_empty_file_falls_back_to_default(tmp_path):
    (tmp_path / "constitution.md").write_text("   \n  ")
    assert constitution.load(workspace=tmp_path) == constitution.DEFAULT_CONSTITUTION


def test_system_prompt_injects_constitution():
    base = system_prompt("backend")
    assert "constitution" not in base.lower().split("constitution")[0]  # no constitution by default
    withc = system_prompt("backend", "RULE: tabs not spaces.")
    assert "RULE: tabs not spaces." in withc
    assert "server/API" in withc  # role specialty still present


def test_review_includes_constitution_in_prompt():
    # The reviewer must be told to check adherence; capture the system prompt it's given.
    from pathlib import Path

    from foundry import review

    captured = {}

    class CaptureWorker:
        name = "cap"

        def is_available(self) -> bool:
            return True

        def execute(self, system: str, instruction: str, workspace, seat: str = "generalist") -> str:
            captured["system"] = system
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'

    review.run_review(CaptureWorker(), "goal", Path("/tmp"), "RULE: no eval().")
    assert "RULE: no eval()." in captured["system"]
    assert "adherence" in captured["system"].lower()
