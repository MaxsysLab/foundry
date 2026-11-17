"""Skill library — curated expertise injected into the workers.

A library is a directory of `<category>/<skill>/SKILL.md` bundles (the Agent-Skills
format; e.g. an external library adopted and SkillWarden-vetted). For each task,
Foundry selects the skills relevant to the role + task and stages them into the
workspace so the worker can consult them — turning generalist workers into
specialists backed by the team's accumulated know-how.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from pydantic import BaseModel

# Which library categories each role should draw on (matched loosely by substring).
ROLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "architect": ("foundation", "architecture"),
    "frontend": ("frontend",),
    "backend": ("backend", "architecture"),
    "database": ("db", "database", "data"),
    "tester": ("testing", "test"),
    "security": ("security",),
    "devops": ("ci-cd", "ci_cd", "cicd", "performance", "devops"),
    "generalist": (),  # any category
}

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "with",
    "to",
    "of",
    "in",
    "on",
    "use",
    "using",
    "build",
    "builds",
    "set",
    "setup",
    "your",
    "this",
    "that",
    "skill",
}


class Skill(BaseModel):
    name: str
    description: str
    category: str
    path: str  # absolute path to the SKILL.md

    def keywords(self) -> set[str]:
        text = f"{self.name} {self.description}".lower()
        return {w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in _STOPWORDS}


class SkillLibrary(BaseModel):
    root: str
    skills: list[Skill] = []

    @classmethod
    def load(cls, root: str | Path) -> SkillLibrary | None:
        p = Path(root).expanduser()
        if not root or not p.is_dir():
            return None
        skills: list[Skill] = []
        for skill_md in p.rglob("SKILL.md"):
            if ".git" in skill_md.parts:
                continue
            name, desc = _parse_frontmatter(skill_md)
            rel = skill_md.relative_to(p).parts
            category = rel[0] if len(rel) > 1 else "general"
            skills.append(
                Skill(
                    name=name or skill_md.parent.name,
                    description=desc,
                    category=category,
                    path=str(skill_md),
                )
            )
        return cls(root=str(p), skills=skills)

    def select_for(self, role: str, task_text: str, limit: int) -> list[Skill]:
        cats = ROLE_CATEGORIES.get(role, ())
        pool = [s for s in self.skills if not cats or any(c in s.category.lower() for c in cats)]
        wanted = {w for w in re.findall(r"[a-z0-9]+", task_text.lower()) if len(w) > 2}
        scored = sorted(pool, key=lambda s: len(s.keywords() & wanted), reverse=True)
        # Keep only skills with some overlap; fall back to top category skills if none match.
        relevant = [s for s in scored if s.keywords() & wanted]
        return (relevant or scored)[:limit]

    def stage(self, skills: list[Skill], workspace: Path) -> str:
        """Copy selected skills into the workspace and return a note for the worker."""
        if not skills:
            return ""
        dest_root = workspace / ".foundry" / "skills"
        lines = [
            "Relevant skills are staged in `.foundry/skills/` — consult the ones "
            "below before implementing; follow their patterns and checklists:"
        ]
        for s in skills:
            dest = dest_root / s.name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s.path, dest / "SKILL.md")
            lines.append(f"  - .foundry/skills/{s.name}/SKILL.md — {s.description[:120]}")
        return "\n".join(lines)


def _parse_frontmatter(skill_md: Path) -> tuple[str, str]:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "", ""
    m = _FRONTMATTER.match(text)
    if not m:
        return "", ""
    name = desc = ""
    for line in m.group(1).splitlines():
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.lower().startswith("description:"):
            desc = line.split(":", 1)[1].strip()
    return name, desc
