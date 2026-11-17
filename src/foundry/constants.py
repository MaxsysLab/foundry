"""Tunable constants. Env vars override where noted."""

from __future__ import annotations

import os

# Planning LLM: the API when a key is set, else the `claude` command.
PROVIDER = os.getenv("FOUNDRY_PROVIDER", "auto")

# ── Model allocation: the right model for the load ──
# Every LLM seat is classified and assigned a tier. FOUNDRY_MODEL /
# FOUNDRY_CLI_MODEL / FOUNDRY_WORKER_MODEL and the per-tier vars are the manual
# "multiple trips" lever for degraded operation (decompose + verify harder).
#
# The lever has a FLOOR. It used to override ALL seats on its
# path: the comment here said "judge seats should wait" while
# FOUNDRY_WORKER_MODEL bypassed the whole table and put review, verify and
# security on one model, and FOUNDRY_MODEL_TOP redefined what "top" even meant.
# Judge, adjudicator and synthesis seats must wait rather than degrade; a rule
# an env var can violate is only advisory, so `seat_model` enforces the floor.
MODEL_TIERS = {  # alias form: the SDK worker and the `claude` command
    "top": os.getenv("FOUNDRY_MODEL_TOP", "opus"),
    "mid": os.getenv("FOUNDRY_MODEL_MID", "sonnet"),
    "cheap": os.getenv("FOUNDRY_MODEL_CHEAP", "haiku"),
}
API_MODEL_TIERS = {  # explicit ids: API provider (prod path)
    # Was pinned to claude-opus-4-8, which is superseded: a pinned model id goes
    # stale, so keep these current.
    "top": os.getenv("FOUNDRY_API_MODEL_TOP", "claude-opus-5-5"),
    "mid": os.getenv("FOUNDRY_API_MODEL_MID", "claude-sonnet-5-5"),
    "cheap": os.getenv("FOUNDRY_API_MODEL_CHEAP", "claude-haiku-5-5"),
}
SEAT_TIERS = {
    # planning-provider seats
    "discovery": "top",  # spec synthesis — never downgrade
    "planner": "top",  # planning judgment — never downgrade
    # build-worker seats by role: executors run mid with sharp role prompts,
    # compensated by task gates + verify/review loops (calibrated pattern)
    "architect": "top",  # foundation-setting
    "security": "top",  # adversarial judgment — never downgrade
    "frontend": "mid",
    "backend": "mid",
    "database": "mid",
    "tester": "mid",
    "devops": "mid",
    "generalist": "mid",
    # post-build judge seats — never downgrade
    "review": "top",
    "verify": "top",
}


# Tiers whose seats the standard forbids degrading. "top" holds discovery, planner,
# architect, security, review and verify — the judgment seats, which is where the
# measured degradation cliff is.
PROTECTED_TIERS: frozenset[str] = frozenset({"top"})

# Models and aliases known to BE top tier. An override for a protected seat has to
# appear here; anything else cannot be shown not to be a downgrade, and "we could
# not prove this is not a downgrade" is no basis on which to seat a judge. Add to
# this set when the top tier genuinely moves forward — a reviewed code change.
KNOWN_TOP: frozenset[str] = frozenset(
    {
        "opus",
        "best",
        "fable",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-fable-5-2",
        "claude-fable-5-1",
        "claude-fable-5",
    }
)


class JudgeDowngradeRefused(RuntimeError):
    """Raised when configuration would run a protected seat below its tier.

    Loud rather than silent on purpose: a quietly demoted reviewer still returns a
    verdict and a score, and nothing downstream can tell it apart from a real one.
    """


def seat_model(seat: str, api: bool = False, override: str = "") -> str:
    """Resolve a seat to its model, with the never-downgrade rule enforced.

    `override` is the caller's global lever (FOUNDRY_WORKER_MODEL, FOUNDRY_MODEL,
    FOUNDRY_CLI_MODEL). Unknown seats get the mid tier. Unprotected seats honour
    both the override and the per-tier env vars exactly as before — degrading the
    executor seats is legitimate and operators need it. Protected seats accept a
    change only when it is provably still top tier.
    """
    tier = SEAT_TIERS.get(seat, "mid")
    table = API_MODEL_TIERS if api else MODEL_TIERS
    candidate = override or table[tier]
    if tier not in PROTECTED_TIERS or candidate in KNOWN_TOP:
        return candidate
    source = "override (FOUNDRY_WORKER_MODEL / FOUNDRY_MODEL / FOUNDRY_CLI_MODEL)"
    if not override:
        source = f"FOUNDRY_{'API_' if api else ''}MODEL_{tier.upper()}"
    raise JudgeDowngradeRefused(
        f"seat '{seat}' is a protected '{tier}'-tier seat and cannot run on "
        f"{candidate!r} (from {source}). Judge, adjudicator and "
        "synthesis seats wait rather than degrade: degrade the executor seats "
        "(FOUNDRY_MODEL_MID / _CHEAP) or park the build instead. If this model IS "
        "top tier, add it to constants.KNOWN_TOP in a reviewed change."
    )


def check_protected_seats() -> None:
    """Fail fast on a configuration that would demote a judge seat.

    Called before a build starts, so an operator learns from a config error rather
    than from REVIEW blowing up after a full build has been paid for.
    """
    for seat, tier in SEAT_TIERS.items():
        if tier not in PROTECTED_TIERS:
            continue
        for api, override in ((False, WORKER_MODEL), (False, CLI_MODEL), (True, ANTHROPIC_MODEL)):
            seat_model(seat, api=api, override=override)


# Global per-path overrides (empty = use the seat allocation above).
ANTHROPIC_MODEL = os.getenv("FOUNDRY_MODEL", "")
CLI_MODEL = os.getenv("FOUNDRY_CLI_MODEL", "")
LLM_MAX_TOKENS = int(os.getenv("FOUNDRY_LLM_MAX_TOKENS", "8000"))
PLAN_TIMEOUT = float(os.getenv("FOUNDRY_PLAN_TIMEOUT", "300"))

# Build worker (Claude executing tasks in the workspace).
# Backend: "sdk" = Claude Agent SDK (default, native foundation); "cli" = `claude -p` fallback.
WORKER_BACKEND = os.getenv("FOUNDRY_WORKER_BACKEND", "sdk")
WORKER_MODEL = os.getenv("FOUNDRY_WORKER_MODEL", "")  # empty = per-seat allocation
WORKER_TIMEOUT = float(os.getenv("FOUNDRY_WORKER_TIMEOUT", "1200"))  # a task can take a while
WORKER_MAX_TURNS = int(os.getenv("FOUNDRY_WORKER_MAX_TURNS", "60"))  # SDK agent-loop cap per task
# SDK permission mode (replaces the CLI's --dangerously-skip-permissions). Tighten
# toward allow/deny + sandbox as the safety hardening lands (see docs/ROADMAP.md).
WORKER_PERMISSION_MODE = os.getenv("FOUNDRY_WORKER_PERMISSION", "bypassPermissions")
WORKER_TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
MAX_TASK_ATTEMPTS = int(os.getenv("FOUNDRY_MAX_TASK_ATTEMPTS", "3"))  # gate-fail → fix → retry
# Transient worker/API failures (rate-limit, overload, 5xx, timeout) are retried with
# backoff before the task is failed — a blip shouldn't abort an otherwise-good build.
WORKER_RETRIES = int(os.getenv("FOUNDRY_WORKER_RETRIES", "3"))
WORKER_RETRY_BACKOFF = float(os.getenv("FOUNDRY_WORKER_RETRY_BACKOFF", "15"))  # seconds, ×attempt
GATE_TIMEOUT = float(os.getenv("FOUNDRY_GATE_TIMEOUT", "600"))
# Independent tasks within a dependency wave run concurrently, up to this many.
# Conservative default for modest hardware; raise it on bigger build machines.
MAX_PARALLEL = int(os.getenv("FOUNDRY_MAX_PARALLEL", "2"))

# Planner archetypes: guarantee non-negotiable tasks per project type (e.g. a
# security-hardening task for web apps) regardless of the planner's non-determinism.
ARCHETYPES = os.getenv("FOUNDRY_ARCHETYPES", "1").lower() not in ("0", "false", "no")

# DISCOVERY phase: turn the brief into a spec with testable acceptance criteria that
# ground the plan and drive VERIFY (build the right thing, prove it does what was asked).
DISCOVERY = os.getenv("FOUNDRY_DISCOVERY", "1").lower() not in ("0", "false", "no")

# VERIFY phase: run the built app and exercise it (browser if available, else HTTP),
# with a bounded verify→fix loop. Only a definitive failure blocks the build.
VERIFY = os.getenv("FOUNDRY_VERIFY", "1").lower() not in ("0", "false", "no")
VERIFY_MAX_ROUNDS = int(os.getenv("FOUNDRY_VERIFY_ROUNDS", "2"))

# REVIEW phase: multi-axis code review after the build, with a bounded fix loop.
REVIEW = os.getenv("FOUNDRY_REVIEW", "1").lower() not in ("0", "false", "no")
REVIEW_MAX_ROUNDS = int(os.getenv("FOUNDRY_REVIEW_ROUNDS", "2"))

# Project constitution: path to a constitution.md overriding the built-in standard.
CONSTITUTION = os.getenv("FOUNDRY_CONSTITUTION", "")

# Concurrency isolation for parallel workers: "worktree" (git worktree per task,
# merged back) or "none" (shared workspace). Falls back to none if git is absent.
ISOLATION = os.getenv("FOUNDRY_ISOLATION", "worktree")

# Skill library — curated expertise injected per role/task. Point at a directory
# of <category>/<skill>/SKILL.md bundles (e.g. a SkillWarden-vetted library).
SKILL_LIBRARY = os.getenv("FOUNDRY_SKILL_LIBRARY", "")
MAX_SKILLS_PER_TASK = int(os.getenv("FOUNDRY_MAX_SKILLS", "3"))

# SkillWarden integration (vetting an imported skill library). Decoupled via CLI.
SKILLWARDEN_CMD = os.getenv("FOUNDRY_SKILLWARDEN", "skillwarden")
SKILLWARDEN_TIMEOUT = float(os.getenv("FOUNDRY_SKILLWARDEN_TIMEOUT", "120"))
