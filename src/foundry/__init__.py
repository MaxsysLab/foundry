"""Foundry: takes a brief and builds a working web app with a team of Claude \
workers, through quality gates."""

from __future__ import annotations

__version__ = "0.1.0"

from foundry.engine import build
from foundry.models import BuildState, Plan, Task

__all__ = ["BuildState", "Plan", "Task", "__version__", "build"]
