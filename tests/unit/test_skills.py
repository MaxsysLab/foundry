"""Skill library: loading, role/keyword selection, staging, engine injection."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry import engine
from foundry.skills import SkillLibrary


def _make_library(root: Path) -> None:
    skills = {
        "frontend/page-layout-builder": (
            "page-layout-builder",
            "Generate dashboard and auth page layouts with routing and state.",
        ),
        "frontend/form-builder": ("form-builder", "Build accessible forms with validation."),
        "backend/rest-api-scaffolder": (
            "rest-api-scaffolder",
            "Scaffold REST API endpoints with validation and error handling.",
        ),
        "testing/unit-test-writer": (
            "unit-test-writer",
            "Write unit tests with good edge-case coverage.",
        ),
    }
    for rel, (name, desc) in skills.items():
        d = root / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\nbody\n"
        )


@pytest.fixture
def library(tmp_path) -> SkillLibrary:
    _make_library(tmp_path / "lib")
    lib = SkillLibrary.load(tmp_path / "lib")
    assert lib is not None
    return lib


def test_load_parses_skills_and_categories(library):
    assert len(library.skills) == 4
    cats = {s.category for s in library.skills}
    assert cats == {"frontend", "backend", "testing"}
    page = next(s for s in library.skills if s.name == "page-layout-builder")
    assert "dashboard" in page.description.lower()


def test_load_missing_returns_none(tmp_path):
    assert SkillLibrary.load("") is None
    assert SkillLibrary.load(tmp_path / "nope") is None


def test_select_filters_by_role_category(library):
    chosen = library.select_for("backend", "create the REST API endpoints", limit=3)
    assert chosen and all(s.category == "backend" for s in chosen)


def test_select_ranks_by_keyword_overlap(library):
    chosen = library.select_for("frontend", "build the dashboard auth pages", limit=1)
    assert chosen[0].name == "page-layout-builder"  # best keyword match in frontend


def test_stage_copies_skills_and_returns_note(library, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    chosen = library.select_for("frontend", "dashboard layout", limit=2)
    note = library.stage(chosen, ws)
    assert ".foundry/skills/" in note
    staged = list((ws / ".foundry" / "skills").rglob("SKILL.md"))
    assert len(staged) == len(chosen)


def test_engine_injects_skills_into_worker(fake_provider, fake_worker, tmp_path):
    _make_library(tmp_path / "lib")
    engine.build(
        "build a todo app",
        workspace=str(tmp_path / "ws"),
        provider=fake_provider,
        worker=fake_worker,
        skill_library=str(tmp_path / "lib"),
    )
    # The worker's instructions should reference the staged skills.
    assert any(".foundry/skills/" in call for call in fake_worker.calls)


def test_no_library_means_no_injection(fake_provider, fake_worker, tmp_path):
    engine.build(
        "build a todo app",
        workspace=str(tmp_path / "ws"),
        provider=fake_provider,
        worker=fake_worker,
    )
    assert not any(".foundry/skills/" in call for call in fake_worker.calls)
