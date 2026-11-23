"""Seat integrity: the never-downgrade floor, fix-seat routing, and the security
corroboration that must not fail open.

Each of these was a rule the
code stated in a comment and did not enforce.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from foundry import constants, review, security
from foundry.models import ReviewFinding

PROTECTED = sorted(s for s, t in constants.SEAT_TIERS.items() if t in constants.PROTECTED_TIERS)
EXECUTORS = sorted(s for s, t in constants.SEAT_TIERS.items() if t not in constants.PROTECTED_TIERS)


# ── The floor ────────────────────────────────────────────────────────


def test_protected_seats_are_the_judgment_seats():
    """If someone re-tiers review or security to 'mid', that is a calibration
    decision and this test makes them state it."""
    assert set(PROTECTED) == {"discovery", "planner", "architect", "security", "review", "verify"}
    assert "generalist" in EXECUTORS and "frontend" in EXECUTORS


def test_default_config_resolves_and_top_is_not_a_stale_pin():
    for seat in constants.SEAT_TIERS:
        assert constants.seat_model(seat)
        assert constants.seat_model(seat, api=True)
    # Alias hygiene: no superseded ids pinned in code.
    assert constants.API_MODEL_TIERS["top"] == "claude-opus-5-5"
    assert constants.API_MODEL_TIERS["mid"] == "claude-sonnet-5-5"
    assert constants.API_MODEL_TIERS["cheap"] == "claude-haiku-5-5"
    assert "claude-opus-4-8" not in constants.API_MODEL_TIERS.values()


@pytest.mark.parametrize("seat", EXECUTORS)
def test_executor_seats_stay_degradable(seat, monkeypatch):
    """The 'multiple trips' lever is legitimate here and operators need it."""
    monkeypatch.setitem(constants.MODEL_TIERS, constants.SEAT_TIERS[seat], "haiku")
    assert constants.seat_model(seat) == "haiku"
    assert constants.seat_model(seat, override="haiku") == "haiku"


@pytest.mark.parametrize("seat", PROTECTED)
def test_global_worker_override_cannot_demote_a_protected_seat(seat):
    """FOUNDRY_WORKER_MODEL used to bypass the ENTIRE seat table — one env var put
    review, verify and security on one model."""
    with pytest.raises(constants.JudgeDowngradeRefused) as e:
        constants.seat_model(seat, override="haiku")
    assert seat in str(e.value)
    assert "wait rather than degrade" in str(e.value)


@pytest.mark.parametrize("seat", PROTECTED)
def test_tier_redefinition_cannot_demote_a_protected_seat(seat, monkeypatch):
    """FOUNDRY_MODEL_TOP=sonnet redefined what 'top' even meant."""
    monkeypatch.setitem(constants.MODEL_TIERS, "top", "sonnet")
    with pytest.raises(constants.JudgeDowngradeRefused) as e:
        constants.seat_model(seat)
    assert "FOUNDRY_MODEL_TOP" in str(e.value)


@pytest.mark.parametrize("seat", PROTECTED)
def test_api_tier_redefinition_cannot_demote_a_protected_seat(seat, monkeypatch):
    monkeypatch.setitem(constants.API_MODEL_TIERS, "top", "claude-haiku-4-5")
    with pytest.raises(constants.JudgeDowngradeRefused) as e:
        constants.seat_model(seat, api=True)
    assert "FOUNDRY_API_MODEL_TOP" in str(e.value)


def test_unrankable_model_is_refused_for_a_protected_seat():
    """We cannot prove an unknown id is top tier, and that is not a basis on which
    to seat a judge."""
    with pytest.raises(constants.JudgeDowngradeRefused):
        constants.seat_model("review", override="some-local-thing")


@pytest.mark.parametrize("model", sorted(constants.KNOWN_TOP))
def test_known_top_models_are_accepted(model):
    """The floor blocks downgrades, not changes — otherwise moving the top tier
    forward would need a patch to every call site."""
    assert constants.seat_model("review", override=model) == model


def test_check_protected_seats_passes_by_default():
    constants.check_protected_seats()


def test_check_protected_seats_fails_fast(monkeypatch):
    """An operator should learn from a config error, not from REVIEW exploding after
    a whole build has been paid for."""
    monkeypatch.setattr(constants, "WORKER_MODEL", "haiku")
    with pytest.raises(constants.JudgeDowngradeRefused):
        constants.check_protected_seats()


# ── Repair on the seat that matches the finding ──────────────────────


def _finding(axis, severity="high"):
    return ReviewFinding(axis=axis, severity=severity, issue=f"{axis} problem")


def test_security_findings_route_to_the_security_seat():
    """The defect: a top-tier reviewer's blocking security finding was handed to a
    mid-tier worker wearing the generalist prompt."""
    assert review.fix_role([_finding("security")]) == "security"


def test_security_wins_when_mixed_with_other_axes():
    """Security severity is the one the reviewer may not negotiate down, so it takes
    the seat whenever it is present."""
    role = review.fix_role([_finding("readability"), _finding("security"), _finding("performance")])
    assert role == "security"


@pytest.mark.parametrize("axis", ["correctness", "performance", "architecture", "readability"])
def test_non_security_axes_go_to_the_generalist(axis):
    """Deliberate: `tester` writes tests, so routing a correctness bug there invites a
    test for the bug rather than a fix. Only security has a seat worth diverting to."""
    assert review.fix_role([_finding(axis)]) == "generalist"


def test_unknown_axis_falls_back_to_generalist():
    assert review.fix_role([_finding("vibes")]) == "generalist"
    assert review.fix_role([]) == "generalist"


def test_axis_matching_is_case_and_space_insensitive():
    assert review.fix_role([ReviewFinding(axis=" Security ", severity="high", issue="x")]) == (
        "security"
    )


def test_the_security_seat_is_top_tier():
    """Routing to `security` only helps if that seat is actually protected."""
    assert constants.SEAT_TIERS["security"] in constants.PROTECTED_TIERS


# ── A crashed scanner is not a clean scan ────────────────────────────


def test_critical_findings_returns_none_when_skillwarden_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(security.shutil, "which", lambda _c: None)
    assert security.critical_findings(tmp_path) is None


def test_critical_findings_returns_none_when_the_scan_crashes(monkeypatch, tmp_path):
    """This is the defect: a crashed scanner used to return [] — indistinguishable
    from a clean scan, so the security axis silently lost its corroboration."""
    monkeypatch.setattr(security.shutil, "which", lambda _c: "/usr/bin/skillwarden")

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="skillwarden", timeout=1.0)

    monkeypatch.setattr(security.subprocess, "run", boom)
    assert security.critical_findings(tmp_path) is None


def test_critical_findings_returns_none_on_unparseable_output(monkeypatch, tmp_path):
    monkeypatch.setattr(security.shutil, "which", lambda _c: "/usr/bin/skillwarden")
    monkeypatch.setattr(
        security.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": "not json"})()
    )
    assert security.critical_findings(tmp_path) is None


def test_critical_findings_returns_empty_list_on_a_genuinely_clean_scan(monkeypatch, tmp_path):
    """`[]` must keep meaning "ran, found nothing" — that is the whole distinction."""
    monkeypatch.setattr(security.shutil, "which", lambda _c: "/usr/bin/skillwarden")
    payload = json.dumps({"findings": [], "status": "complete"})
    monkeypatch.setattr(
        security.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": payload})()
    )
    assert security.critical_findings(tmp_path) == []


# ── An incomplete scan is not a clean scan either ───────────────────
# SkillWarden marks a scan `incomplete` when an input was skipped (an unreadable file),
# an analyzer failed, or the target held nothing to scan; its README: never treat an
# incomplete scan as a pass. Reproduced 2026-10-24: a bundle whose helper.py could not be
# read (and held `curl ... | sh`) scanned `incomplete`, label `info`, score 0, and
# `foundry vet` reported it "clean (safe to adopt)".


def _skillwarden_says(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(security.shutil, "which", lambda _c: "/usr/bin/skillwarden")
    out = json.dumps(payload)
    monkeypatch.setattr(security.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": out})())


# A build workspace is a git checkout, and it is the operator's own: SkillWarden is
# asked to leave out the `.git` folder at its top.


def _scanner(monkeypatch, answer):
    """A fake `skillwarden`: `answer(argv)` gives the text it prints."""
    calls: list[list[str]] = []
    monkeypatch.setattr(security.shutil, "which", lambda _c: "/usr/bin/skillwarden")

    def run(argv, **_k):
        calls.append(list(argv))
        return type("P", (), {"stdout": answer(list(argv))})()

    monkeypatch.setattr(security.subprocess, "run", run)
    return calls


def test_the_workspace_scan_leaves_out_the_top_git_folder(monkeypatch, tmp_path):
    payload = json.dumps({"findings": [], "status": "complete"})
    calls = _scanner(monkeypatch, lambda argv: payload)
    assert security.critical_findings(tmp_path) == []
    assert len(calls) == 1 and "--skip-git-dir" in calls[0]


def test_a_scanner_that_does_not_know_the_option_is_asked_again_without_it(monkeypatch, tmp_path):
    payload = json.dumps({"findings": [], "status": "complete"})
    calls = _scanner(
        monkeypatch, lambda argv: "Usage: no such option" if "--skip-git-dir" in argv else payload
    )
    assert security.critical_findings(tmp_path) == []
    assert ["--skip-git-dir" in c for c in calls] == [True, False]


def test_a_scanner_that_answers_neither_way_is_not_a_clean_scan(monkeypatch, tmp_path):
    calls = _scanner(monkeypatch, lambda argv: "not json")
    assert security.critical_findings(tmp_path) is None
    assert len(calls) == 2


def test_vetting_a_library_does_not_leave_anything_out(monkeypatch, tmp_path):
    # A library comes from somewhere else: a `.git` folder inside a bundle is not ours.
    payload = json.dumps({"severity_label": "info", "risk_score": 0, "status": "complete"})
    calls = _scanner(monkeypatch, lambda argv: payload)
    security.vet_library(_one_bundle(tmp_path))
    assert calls and not any("--skip-git-dir" in c for c in calls)


def test_critical_findings_returns_none_on_an_incomplete_scan(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"findings": [], "status": "incomplete"})
    assert security.critical_findings(tmp_path) is None


def test_critical_findings_treats_a_missing_status_as_incomplete(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"findings": []})
    assert security.critical_findings(tmp_path) is None


def test_critical_findings_still_reports_criticals_from_an_incomplete_scan(monkeypatch, tmp_path):
    """What it did find still counts: a CRITICAL blocks whether or not coverage was full."""
    _skillwarden_says(monkeypatch, {
        "status": "incomplete",
        "findings": [{"severity": "critical", "rule_id": "EX-001", "title": "remote exec", "file": "x.py"}],
    })
    out = security.critical_findings(tmp_path)
    assert out is not None and len(out) == 1 and out[0].severity == "high"


def _one_bundle(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "SKILL.md").write_text("# demo\n")
    return tmp_path


def test_vet_never_marks_an_incomplete_scan_safe_to_adopt(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"severity_label": "info", "risk_score": 0, "status": "incomplete"})
    [r] = security.vet_library(_one_bundle(tmp_path))
    assert (r.complete, r.ok) == (False, False)


def test_vet_treats_a_missing_status_as_incomplete(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"severity_label": "info", "risk_score": 0})
    [r] = security.vet_library(_one_bundle(tmp_path))
    assert (r.complete, r.ok) == (False, False)


def test_vet_marks_a_complete_clean_scan_safe_to_adopt(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"severity_label": "info", "risk_score": 0, "status": "complete"})
    [r] = security.vet_library(_one_bundle(tmp_path))
    assert (r.complete, r.ok) == (True, True)


def test_vet_still_rejects_a_complete_high_scan(monkeypatch, tmp_path):
    _skillwarden_says(monkeypatch, {"severity_label": "high", "risk_score": 70, "status": "complete"})
    [r] = security.vet_library(_one_bundle(tmp_path))
    assert (r.complete, r.ok) == (True, False)


def test_review_records_when_corroboration_was_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "committed_critical_findings", lambda _p: None)

    class W:
        def execute(self, _s, _i, _w, seat="generalist"):
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'

    r = review.run_review(W(), "goal", tmp_path)
    assert r.security_corroborated is False


def test_review_records_when_corroboration_ran_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "committed_critical_findings", lambda _p: [])

    class W:
        def execute(self, _s, _i, _w, seat="generalist"):
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'

    r = review.run_review(W(), "goal", tmp_path)
    assert r.security_corroborated is True
    assert r.approved


def test_a_real_critical_still_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        security,
        "committed_critical_findings",
        lambda _p: [ReviewFinding(axis="security", severity="high", issue="rce")],
    )

    class W:
        def execute(self, _s, _i, _w, seat="generalist"):
            return '{"verdict": "approve", "score": 95, "axes": {}, "findings": []}'

    r = review.run_review(W(), "goal", tmp_path)
    assert r.verdict == "changes_requested"
    assert not r.approved
    assert r.security_corroborated is True


def test_missing_corroboration_is_surfaced_in_the_verdict_event(monkeypatch, tmp_path):
    """Recording it in BuildState is not enough — a field nobody reads is not a
    control. The operator has to see that the axis rests on one opinion."""
    monkeypatch.setattr(security, "committed_critical_findings", lambda _p: None)
    monkeypatch.setattr(constants, "DISCOVERY", False)
    monkeypatch.setattr(constants, "VERIFY", False)
    monkeypatch.setattr(constants, "ARCHETYPES", False)

    events: list[tuple[str, str]] = []

    class Provider:
        def complete(self, _s, _u, seat="planner"):
            return json.dumps(
                {
                    "app_name": "a",
                    "stack": "s",
                    "summary": "x",
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "t",
                            "role": "generalist",
                            "instruction": "do it",
                            "depends_on": [],
                            "verify": "",
                        }
                    ],
                }
            )

    class W:
        def execute(self, _s, _i, _w, seat="generalist"):
            return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'

    from foundry import engine

    engine.build(
        "brief",
        workspace=str(tmp_path),
        provider=Provider(),
        worker=W(),
        discovery=False,
        verify=False,
        review=True,
        isolation="none",
        on_event=lambda stage, detail: events.append((stage, detail)),
    )
    verdicts = [d for s, d in events if s == "review-verdict"]
    assert verdicts and "SECURITY UNCORROBORATED" in verdicts[0]


# ── The planner must not carry a default stack ───────────────────────


def test_planner_prompt_names_no_default_stack():
    """The JSON schema the planner fills used to read
    `"stack": "e.g. Next.js 15 + TypeScript + SQLite/Prisma"`. An example inside an
    output schema reads as a default, and the stack decision is inherited by every
    downstream worker — so one incidental example pre-made the most consequential
    generative choice in the tool."""
    from foundry import planner

    prompt = planner._SYSTEM
    for named in ("Next.js", "SQLite", "Prisma", "FastAPI", "Django", "Express"):
        assert named not in prompt, f"planner prompt names a specific stack: {named}"


def test_planner_prompt_requires_the_stack_to_be_justified():
    """Removing the anchor is only half of it — the model still needs telling where
    the choice should come from instead."""
    from foundry import planner

    prompt = planner._SYSTEM.lower()
    assert "from this brief" in prompt
    assert "cannot justify" in prompt or "name that reason" in prompt
