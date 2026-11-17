"""The DISCOVERY phase — turn a brief into an explicit, testable spec.

Before any planning, an analyst turns the (often terse) brief + guidance into a
structured Spec: the concrete goal, the requirements, and **testable acceptance
criteria** — the contract for what "done and correct" means for the *product*. The
plan is then built to satisfy the spec, and the VERIFY phase exercises the running
app against those same acceptance criteria. This closes the loop between intent and
verification: build the right thing, and prove it does what was asked.

Uses the planning LLM provider (not a build worker) — it's analysis, not coding.
"""

from __future__ import annotations

import json
import re
from typing import Any

from foundry.models import AcceptanceCriterion, Spec
from foundry.providers.base import LLMProvider

_SYSTEM = """You are a product analyst running discovery on a software brief before any build planning. \
Turn the brief (which may be terse) into a precise, buildable specification. Infer sensible, conventional \
defaults for anything unstated rather than leaving gaps — but do not invent scope the brief doesn't imply.

Produce JSON ONLY (no prose, no code fence):
{"goal": "one-sentence statement of what the product is and who it's for",
 "requirements": ["concrete functional/non-functional requirements the build must meet"],
 "acceptance_criteria": [
   {"id": "AC1", "statement": "a TESTABLE, observable statement of behaviour — something that can be checked \
against the running app, e.g. 'A user can submit the form and see the new item appear without a page reload'"}
 ],
 "out_of_scope": ["things a reader might expect but that are explicitly NOT part of this build"],
 "notes": "any assumptions or decisions worth recording"}

Make acceptance criteria specific and verifiable (observable behaviour, status codes, validation, persistence) — \
they are the contract the app will be tested against. Keep the set complete but minimal: cover the core flows, \
not every edge. Aim for 3-8 criteria for a small app."""


def _json(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        parsed: dict[str, Any] = json.loads(match.group(0))
        return parsed
    except json.JSONDecodeError:
        return {}


def run_discovery(provider: LLMProvider, brief: str, guidance: str = "") -> Spec:
    user = f"Brief: {brief}" + (f"\n\nConstraints/guidance: {guidance}" if guidance else "")
    data = _json(provider.complete(_SYSTEM, user, seat="discovery"))
    criteria: list[AcceptanceCriterion] = []
    for i, c in enumerate(data.get("acceptance_criteria") or [], 1):
        statement = str(c.get("statement", "")).strip()
        if not statement:
            continue
        criteria.append(AcceptanceCriterion(id=str(c.get("id") or f"AC{i}"), statement=statement))
    return Spec(
        goal=str(data.get("goal", "")).strip() or brief.strip(),
        requirements=[str(r).strip() for r in (data.get("requirements") or []) if str(r).strip()],
        acceptance_criteria=criteria,
        out_of_scope=[str(o).strip() for o in (data.get("out_of_scope") or []) if str(o).strip()],
        notes=str(data.get("notes", "")).strip(),
    )
