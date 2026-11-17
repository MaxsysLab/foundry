"""Human-in-the-loop plan approval.

The highest-leverage checkpoint: approve (or steer) the architecture and task
breakdown BEFORE the agency spends an hour building. Default approves (fully
autonomous); the CLI supplies an interactive reviewer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from foundry.models import Plan


@dataclass
class Revise:
    """Regenerate the plan with added guidance."""

    guidance: str


class Controller(ABC):
    @abstractmethod
    def review_plan(self, plan: Plan) -> Plan | Revise:
        """Return the approved (possibly edited) plan, or Revise to replan."""


class AutoController(Controller):
    name = "auto"

    def review_plan(self, plan: Plan) -> Plan | Revise:
        return plan
