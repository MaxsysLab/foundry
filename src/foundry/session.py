"""Resumable build state — a long autonomous build can resume after interruption."""

from __future__ import annotations

import os
from pathlib import Path

from foundry.models import BuildState


def load(path: str | Path) -> BuildState | None:
    p = Path(path)
    if not p.exists():
        return None
    return BuildState.model_validate_json(p.read_text(encoding="utf-8"))


def save(path: str | Path, state: BuildState) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, p)  # atomic
