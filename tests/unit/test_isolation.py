"""Git-worktree concurrency isolation (uses real git in a temp repo)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from foundry import engine
from foundry import isolation as isolation_mod
from foundry.isolation import WorktreeManager
from foundry.models import BuildState, Plan, Task
from foundry.session import save

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def test_setup_initializes_repo_and_gitignore(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup() is True
    assert (tmp_path / ".git").exists()
    assert ".foundry/" in (tmp_path / ".gitignore").read_text()


@pytest.mark.parametrize("failed_command", ["add", "commit"])
def test_setup_rejects_failed_git_commands(monkeypatch, tmp_path, failed_command):
    git = isolation_mod._git

    def failing_git(args, cwd):
        if args[0] == failed_command:
            return 1, "injected git failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    mgr = WorktreeManager(tmp_path)
    with pytest.raises(RuntimeError, match="injected git failure"):
        mgr.setup()
    assert not mgr.enabled


def test_worktree_changes_merge_back(tmp_path):
    mgr = WorktreeManager(tmp_path)
    mgr.setup()
    wt = mgr.create("t1")
    (wt / "app.py").write_text("print('hello')\n")
    assert mgr.integrate("t1") is True
    mgr.discard("t1")
    # the change is now in the integration tree
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"


DESTRUCTIVE_GIT = (
    ["worktree", "remove"], ["worktree", "prune"],
    ["branch", "-D"], ["branch", "-d"], ["branch", "--delete"],
    ["reset"], ["checkout"], ["clean"], ["update-ref", "-d"], ["restore"], ["stash"],
)


def _user_git(cwd, *args):
    """Another Git actor (the user), bypassing Foundry's `_git` and any spy."""
    proc = subprocess.run(
        ["git", "-c", "user.email=u@example.invalid", "-c", "user.name=User", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _two_task_session(tmp_path):
    plan = Plan(app_name="a", stack="s", summary="", tasks=[
        Task(id="t1", title="one", role="backend", instruction="one"),
        Task(id="t2", title="two", role="backend", instruction="two"),
    ])
    session = tmp_path / "session.json"
    save(session, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    return session


def _build(tmp_path, session, fake_provider, worker, events=None):
    return engine.build(
        "x", workspace=str(tmp_path), provider=fake_provider, worker=worker,
        session_path=str(session), isolation="worktree", discovery=False,
        use_archetypes=False, verify=False, review=False,
        on_event=(lambda kind, msg: events.append((kind, msg))) if events is not None
        else engine._noop,
    )


def _branch_exists(branch, cwd):
    return isolation_mod._git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd)[0] == 0


def _is_add(args):
    return args[:3] == ["worktree", "add", "-b"]


def _cmd(args):
    """A git command without its leading `-c name=value` config pins."""
    args = list(args)
    while args[:1] == ["-c"]:
        args = args[2:]
    return args


def _forbid_destruction(monkeypatch):
    """Spy on git and rmtree; any destructive cleanup command fails the test.
    Recorded commands have their leading `-c` pins stripped (see `_cmd`)."""
    git = isolation_mod._git
    calls: list[list[str]] = []

    own_failed_merge = {"open": False}

    def spy_git(raw, cwd):
        args = _cmd(raw)
        calls.append(args)
        assert not any(args[: len(d)] == d for d in DESTRUCTIVE_GIT), args
        if args[:2] == ["merge", "--abort"]:
            # Only allowed directly for a merge Foundry itself just started and lost.
            assert own_failed_merge["open"], f"merge --abort of a merge Foundry did not start: {args}"
            own_failed_merge["open"] = False
            return git(raw, cwd)
        result = git(raw, cwd)
        if args[:1] == ["merge"]:
            own_failed_merge["open"] = result[0] != 0
        return result

    def no_rmtree(*a, **k):
        raise AssertionError(f"rmtree called: {a}")

    monkeypatch.setattr(isolation_mod, "_git", spy_git)
    monkeypatch.setattr(isolation_mod.shutil, "rmtree", no_rmtree)
    return calls


def test_create_uses_private_unpredictable_path_and_ref(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    assert rec is not None and rec.path == wt and rec.task_id == "t1"
    # Neither the path nor the branch is derived from the task ID.
    assert "t1" not in wt.name and "t1" not in rec.branch
    assert wt != tmp_path / ".foundry" / "wt" / "t1"
    assert not rec.branch.startswith("foundry/")
    assert (wt.stat().st_mode & 0o077) == 0  # private to this user
    assert _branch_exists(rec.branch, tmp_path)
    assert rec.ref == f"refs/heads/{rec.branch}"


def test_colliding_task_ids_across_managers_get_disjoint_resources(tmp_path):
    first, second = WorktreeManager(tmp_path), WorktreeManager(tmp_path)
    assert first.setup() and second.setup()
    a, b = first.create("t1"), second.create("t1")
    ra, rb = first.worktree("t1"), second.worktree("t1")
    assert ra is not None and rb is not None
    assert a != b and ra.branch != rb.branch
    (a / "a.txt").write_text("a")
    (b / "b.txt").write_text("b")
    first.discard("t1")
    assert (a / "a.txt").read_text() == "a" and (b / "b.txt").read_text() == "b"
    assert _branch_exists(ra.branch, tmp_path) and _branch_exists(rb.branch, tmp_path)


def test_discard_never_deletes_and_reports_retained(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    (wt / "user.txt").write_text("keep")
    calls = _forbid_destruction(monkeypatch)
    assert mgr.discard("t1") == rec
    # Checked before _branch_exists below, which itself goes through the spy.
    assert calls == []  # discard runs no git command at all
    assert (wt / "user.txt").read_text() == "keep"
    assert _branch_exists(rec.branch, tmp_path)
    assert rec in mgr.retained
    assert mgr.worktree("t1") is None


def test_create_rejects_failed_worktree_add(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    git = isolation_mod._git

    def failing_git(args, cwd):
        if args[:2] == ["worktree", "add"]:
            return 1, "injected worktree failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    with pytest.raises(RuntimeError, match="injected worktree failure"):
        mgr.create("t1")
    # A failed add grants no ownership: nothing recorded as owned/retained.
    assert mgr.worktree("t1") is None
    assert mgr.retained == []
    assert len(mgr.unowned) == 1 and mgr.unowned[0].task_id == "t1"


def test_failed_second_worktree_prevents_entire_wave(monkeypatch, fake_provider, fake_worker, tmp_path):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    adds: list[list[str]] = []

    def failing_git(args, cwd):
        if _is_add(args):
            adds.append(list(args))
            if len(adds) == 2:
                return 1, "injected second worktree failure"
        assert not any(args[: len(d)] == d for d in DESTRUCTIVE_GIT), args
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    events: list[tuple[str, str]] = []
    with pytest.raises(RuntimeError, match="injected second worktree failure"):
        _build(tmp_path, session, fake_provider, fake_worker, events)
    assert fake_worker.calls == []
    first_branch, first_path = adds[0][3], adds[0][4]
    # The completed first worktree is retained (not deleted) and reported.
    assert (tmp_path / first_path).exists() or Path(first_path).exists()
    assert _branch_exists(first_branch, tmp_path)
    retained = " ".join(m for k, m in events if k == "worktree-retained")
    assert first_path in retained and first_branch in retained


def test_partial_second_worktree_failure_leaves_ambiguous_branch_and_directory(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    adds: list[list[str]] = []

    def failing_git(args, cwd):
        if _is_add(args):
            adds.append(list(args))
            if len(adds) == 2:
                assert git(["branch", args[3], "HEAD"], cwd)[0] == 0
                partial = Path(args[4])
                partial.mkdir(exist_ok=True)
                (partial / "incomplete").write_text("partial git output")
                return 1, "injected partial worktree failure"
        assert not any(args[: len(d)] == d for d in DESTRUCTIVE_GIT), args
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    with pytest.raises(RuntimeError, match="injected partial worktree failure"):
        _build(tmp_path, session, fake_provider, fake_worker)
    assert fake_worker.calls == []
    assert Path(adds[0][4]).exists()  # first retained, not rolled back destructively
    assert (Path(adds[1][4]) / "incomplete").read_text() == "partial git output"
    assert _branch_exists(adds[1][3], tmp_path)


def test_concurrent_claim_after_preflight_is_not_deleted(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    git = isolation_mod._git
    claimed: dict[str, str] = {}

    def competing_git(args, cwd):
        if _is_add(args):
            # Another process claims both names before add runs.
            claimed.update(branch=args[3], path=args[4])
            assert git(["branch", args[3], "HEAD"], cwd)[0] == 0
            (Path(args[4]) / "other.txt").write_text("other process")
            return 1, "branch already exists"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", competing_git)
    with pytest.raises(RuntimeError, match="branch already exists"):
        mgr.create("t1")
    _forbid_destruction(monkeypatch)
    mgr.discard("t1")
    assert (Path(claimed["path"]) / "other.txt").read_text() == "other process"
    assert _branch_exists(claimed["branch"], tmp_path)


def test_show_ref_error_is_not_treated_as_absence(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    git = isolation_mod._git

    def broken_git(args, cwd):
        if args[:2] == ["show-ref", "--verify"]:
            return 128, "repository inaccessible"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", broken_git)
    with pytest.raises(RuntimeError, match="repository inaccessible"):
        mgr.create("t1")
    assert mgr.worktree("t1") is None


def test_cleanup_exception_does_not_mask_creation_failure(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    adds: list[list[str]] = []

    def failing_git(args, cwd):
        if _is_add(args):
            adds.append(list(args))
            if len(adds) == 2:
                return 1, "original creation error"
        return git(args, cwd)

    def broken_discard(self, task_id):
        raise OSError("cleanup error")

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    monkeypatch.setattr(WorktreeManager, "discard", broken_discard)
    with pytest.raises(RuntimeError, match="original creation error"):
        _build(tmp_path, session, fake_provider, fake_worker)
    assert fake_worker.calls == []
    assert Path(adds[0][4]).exists()
    assert _branch_exists(adds[0][3], tmp_path)


def test_discard_does_not_remove_reclaimed_worktree(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    git = isolation_mod._git
    # A separate actor releases Foundry's worktree and reclaims the SAME recorded
    # path and branch before discard. The old record is not current ownership.
    assert git(["worktree", "remove", "--force", str(wt)], tmp_path)[0] == 0
    assert git(["branch", "-D", rec.branch], tmp_path)[0] == 0
    assert git(["worktree", "add", "-b", rec.branch, str(wt), "HEAD"], tmp_path)[0] == 0
    (wt / "external.txt").write_text("belongs to another actor")

    mgr.discard("t1")

    assert (wt / "external.txt").read_text() == "belongs to another actor"
    assert _branch_exists(rec.branch, tmp_path)


def test_discard_does_not_delete_branch_reclaimed_after_remove(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    mgr.create("t1")
    rec = mgr.worktree("t1")
    git = isolation_mod._git

    def reclaim_between_commands(args, cwd):
        result = git(args, cwd)
        if args[:2] == ["worktree", "remove"] and result[0] == 0:
            # The name becomes free as soon as remove finishes. Another actor
            # creates it at HEAD before any separate branch -D command.
            assert git(["branch", "-D", rec.branch], cwd)[0] == 0
            assert git(["branch", rec.branch, "HEAD"], cwd)[0] == 0
        return result

    monkeypatch.setattr(isolation_mod, "_git", reclaim_between_commands)
    mgr.discard("t1")

    assert _branch_exists(rec.branch, tmp_path)


def test_wave_rollback_preserves_reclaimed_first_worktree(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    adds: list[list[str]] = []

    def fail_after_reclaim(args, cwd):
        if _is_add(args):
            adds.append(list(args))
            if len(adds) == 2:
                branch, wt = adds[0][3], adds[0][4]
                assert git(["worktree", "remove", "--force", wt], cwd)[0] == 0
                assert git(["branch", "-D", branch], cwd)[0] == 0
                assert git(["worktree", "add", "-b", branch, wt, "HEAD"], cwd)[0] == 0
                (Path(wt) / "external.txt").write_text("belongs to another actor")
                return 1, "second worktree creation failed"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", fail_after_reclaim)
    with pytest.raises(RuntimeError, match="second worktree creation failed"):
        _build(tmp_path, session, fake_provider, fake_worker)

    assert fake_worker.calls == []
    assert (Path(adds[0][4]) / "external.txt").read_text() == "belongs to another actor"
    assert _branch_exists(adds[0][3], tmp_path)


def test_create_never_reuses_existing_resources(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    existing = mgr.create("t1")
    rec = mgr.worktree("t1")
    (existing / "user.txt").write_text("do not delete")

    again = mgr.create("t1")  # same task ID → fresh private resources
    assert again != existing and mgr.worktree("t1").branch != rec.branch
    assert rec in mgr.retained
    assert (existing / "user.txt").read_text() == "do not delete"
    assert _branch_exists(rec.branch, tmp_path)


def test_legacy_task_named_resources_are_reported_not_deleted(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    assert WorktreeManager(tmp_path).setup()
    legacy = tmp_path / ".foundry" / "wt" / "t1"
    legacy.parent.mkdir(parents=True)
    assert git(["worktree", "add", "-b", "foundry/t1", str(legacy), "HEAD"], tmp_path)[0] == 0
    (legacy / "old.txt").write_text("legacy work")
    assert git(["branch", "foundry/t2", "HEAD"], tmp_path)[0] == 0

    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    report = mgr.legacy_resources()
    assert str(legacy) in report["paths"]
    assert {"refs/heads/foundry/t1", "refs/heads/foundry/t2"} <= set(report["refs"])

    _forbid_destruction(monkeypatch)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, fake_worker, events)
    assert state.done
    legacy_msgs = " ".join(m for k, m in events if k == "worktree-legacy")
    assert str(legacy) in legacy_msgs and "refs/heads/foundry/t2" in legacy_msgs
    assert (legacy / "old.txt").read_text() == "legacy work"
    assert _branch_exists("foundry/t1", tmp_path) and _branch_exists("foundry/t2", tmp_path)


def test_successful_build_merges_and_reports_retained_without_deletion(
    monkeypatch, fake_provider, tmp_path,
):
    class WritingWorker:
        name = "writing"

        def is_available(self) -> bool:
            return True

        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            (Path(workspace) / f"{instruction.split()[-1]}.txt").write_text("x")
            return "ok"

    session = _two_task_session(tmp_path)
    _forbid_destruction(monkeypatch)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, WritingWorker(), events)
    assert state.done
    assert (tmp_path / "one.txt").exists() and (tmp_path / "two.txt").exists()
    retained = [m for k, m in events if k == "worktree-retained"]
    assert len(retained) == 2
    assert all(".foundry/worktrees/" in m and "refs/heads/" in m for m in retained)


def test_disjoint_parallel_worktrees_both_merge(tmp_path):
    mgr = WorktreeManager(tmp_path)
    mgr.setup()
    a, b = mgr.create("a"), mgr.create("b")  # both branch off the same base
    (a / "a.py").write_text("a\n")
    (b / "b.py").write_text("b\n")
    assert mgr.integrate("a") is True
    assert mgr.integrate("b") is True  # disjoint files → clean
    mgr.discard("a")
    mgr.discard("b")
    assert (tmp_path / "a.py").exists() and (tmp_path / "b.py").exists()


def test_conflicting_worktree_fails_without_corruption(tmp_path):
    (tmp_path / "shared.txt").write_text("base\n")  # before setup → committed into the base
    mgr = WorktreeManager(tmp_path)
    mgr.setup()
    a, b = mgr.create("a"), mgr.create("b")  # both branch off the base that has shared.txt
    (a / "shared.txt").write_text("from-a\n")
    (b / "shared.txt").write_text("from-b\n")
    assert mgr.integrate("a") is True
    assert mgr.integrate("b") is False  # same file, divergent → conflict, aborted
    mgr.discard("a")
    mgr.discard("b")
    # base is intact (a's merge), not corrupted by the failed merge
    assert (tmp_path / "shared.txt").read_text() == "from-a\n"


def test_engine_build_with_worktree_isolation(fake_provider, tmp_path):
    # A worker that actually writes a file in its worktree; verify it merges to the workspace.
    class WritingWorker:
        name = "writing"

        def is_available(self) -> bool:
            return True

        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            if "qa engineer verifying" in system.lower():
                return ('{"verdict": "pass", "summary": "ran", '
                        '"checks": [{"name": "starts", "ok": true, "detail": "200"}]}')
            if "final code review" in system.lower():
                return '{"verdict": "approve", "score": 90, "axes": {}, "findings": []}'
            # name the file by the task instruction marker so files are disjoint
            marker = "x" if "do x" in instruction else ("y" if "do y" in instruction else "z")
            (Path(workspace) / f"{marker}.txt").write_text(marker)
            return "ok"

    plan = Plan(app_name="a", stack="s", summary="", tasks=[
        Task(id="t1", title="scaffold", role="architect", instruction="do x"),
        Task(id="t2", title="feature", role="backend", instruction="do y", depends_on=["t1"]),
    ])
    sp = tmp_path / "s.json"
    save(sp, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    state = engine.build("x", workspace=str(tmp_path), provider=fake_provider,
                         worker=WritingWorker(), session_path=str(sp), isolation="worktree")
    assert state.done
    # both workers' files were merged back into the integration workspace
    assert (tmp_path / "x.txt").exists() and (tmp_path / "y.txt").exists()


# --- Blocker 1: integrate() must never abort/undo a base-repo operation it did not start.


def _head(cwd):
    return _user_git(cwd, "rev-parse", "HEAD")[1]


def _stat(path):
    """Identity of a file on disk: rewritten files (even byte-identical) change it."""
    st = path.lstat()
    return st.st_ino, st.st_mtime_ns, st.st_ctime_ns, path.read_bytes()


def test_integrate_preserves_foreign_in_progress_merge_in_base(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("base\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    assert _user_git(tmp_path, "checkout", "-q", "-b", "other")[0] == 0
    (tmp_path / "f.txt").write_text("other\n")
    assert _user_git(tmp_path, "commit", "-qam", "other")[0] == 0
    assert _user_git(tmp_path, "checkout", "-q", mgr.base)[0] == 0
    (tmp_path / "f.txt").write_text("mine\n")
    assert _user_git(tmp_path, "commit", "-qam", "mine")[0] == 0
    wt = mgr.create("t1")
    (wt / "new.txt").write_text("task\n")
    # Another actor is mid-merge in the base: conflict hand-resolved and staged,
    # plus a brand-new staged file.
    assert _user_git(tmp_path, "merge", "other")[0] != 0
    (tmp_path / "f.txt").write_text("hand-resolved\n")
    (tmp_path / "extra.txt").write_text("user staged during merge\n")
    assert _user_git(tmp_path, "add", "f.txt", "extra.txt")[0] == 0
    merge_head = (tmp_path / ".git" / "MERGE_HEAD").read_text()
    head = _head(tmp_path)

    outcome: object
    try:
        outcome = mgr.integrate("t1")
    except RuntimeError as exc:
        outcome = exc

    assert (tmp_path / ".git" / "MERGE_HEAD").read_text() == merge_head
    assert (tmp_path / "f.txt").read_text() == "hand-resolved\n"
    assert (tmp_path / "extra.txt").read_text() == "user staged during merge\n"
    assert _user_git(tmp_path, "diff", "--cached", "--name-only")[1].split() == ["extra.txt", "f.txt"]
    assert _head(tmp_path) == head
    assert outcome is not True


def test_integrate_never_aborts_when_base_has_staged_changes(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "a.txt").write_text("task\n")
    (tmp_path / "a.txt").write_text("user staged\n")
    assert _user_git(tmp_path, "add", "a.txt")[0] == 0
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)

    with pytest.raises(isolation_mod.IntegrationError, match="staged"):
        mgr.integrate("t1")

    assert ["merge", "--abort"] not in calls
    assert not any(c[:1] == ["merge"] for c in calls)  # refused before touching base
    assert _user_git(tmp_path, "diff", "--cached", "--name-only")[1] == "a.txt"
    assert (tmp_path / "a.txt").read_text() == "user staged\n"
    assert _head(tmp_path) == head


def test_conflict_is_detected_without_starting_a_merge_and_keeps_unstaged_wip(
    monkeypatch, tmp_path,
):
    (tmp_path / "shared.txt").write_text("base\n")
    (tmp_path / "notes.txt").write_text("notes\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    a, b = mgr.create("a"), mgr.create("b")
    (a / "shared.txt").write_text("from-a\n")
    (b / "shared.txt").write_text("from-b\n")
    assert mgr.integrate("a") is True
    (tmp_path / "notes.txt").write_text("user unstaged WIP\n")  # unrelated tracked WIP
    before = _stat(tmp_path / "notes.txt"), _stat(tmp_path / "shared.txt")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)

    assert mgr.integrate("b") is False

    # The conflict is found by `git merge-tree` alone: the base working tree and
    # index are never touched, so there is nothing to abort.
    assert not any(c[:1] == ["merge"] for c in calls)
    assert "CONFLICT" in mgr.last_conflict and "shared.txt" in mgr.last_conflict
    assert not (tmp_path / ".git" / "MERGE_HEAD").exists()
    assert _head(tmp_path) == head
    assert (tmp_path / "shared.txt").read_text() == "from-a\n"
    assert (tmp_path / "notes.txt").read_text() == "user unstaged WIP\n"
    assert (_stat(tmp_path / "notes.txt"), _stat(tmp_path / "shared.txt")) == before


# --- Blocker 2: integrate() must verify it is committing/merging Foundry's own worktree.


def _is_ancestor(commit, cwd):
    return _user_git(cwd, "merge-base", "--is-ancestor", commit, "HEAD")[0] == 0


def test_integrate_refuses_worktree_reclaimed_at_same_path_and_branch(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    (wt / "task.txt").write_text("task work\n")
    # Another actor releases and reclaims the recorded names before integrate.
    assert _user_git(tmp_path, "worktree", "remove", "--force", str(wt))[0] == 0
    assert _user_git(tmp_path, "branch", "-D", rec.branch)[0] == 0
    assert _user_git(tmp_path, "worktree", "add", "-b", rec.branch, str(wt), "HEAD")[0] == 0
    (wt / "foreign.txt").write_text("other actor's private WIP\n")
    head, tip = _head(tmp_path), _user_git(tmp_path, "rev-parse", rec.ref)[1]

    with pytest.raises(isolation_mod.IntegrationError):
        mgr.integrate("t1")

    assert _head(tmp_path) == head
    assert not (tmp_path / "foreign.txt").exists()
    assert _user_git(tmp_path, "rev-parse", rec.ref)[1] == tip  # nothing committed there
    assert "?? foreign.txt" in _user_git(wt, "status", "--porcelain")[1]  # untouched WIP


def test_integrate_never_merges_a_foreign_commit_swapped_into_the_branch(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    (wt / "task.txt").write_text("task\n")
    assert _user_git(tmp_path, "checkout", "-q", "-b", "evil")[0] == 0
    (tmp_path / "evil.txt").write_text("evil\n")
    assert _user_git(tmp_path, "add", "evil.txt")[0] == 0
    assert _user_git(tmp_path, "commit", "-qm", "evil")[0] == 0
    evil = _head(tmp_path)
    assert _user_git(tmp_path, "checkout", "-q", mgr.base)[0] == 0
    assert _user_git(tmp_path, "update-ref", rec.ref, evil)[0] == 0

    assert mgr.integrate("t1") is True

    assert (tmp_path / "task.txt").read_text() == "task\n"
    assert not (tmp_path / "evil.txt").exists()
    assert not _is_ancestor(evil, tmp_path)


def test_integrate_never_commits_base_work_when_worktree_gitfile_is_missing(tmp_path):
    (tmp_path / "f.txt").write_text("base\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (tmp_path / "f.txt").write_text("user uncommitted WIP\n")
    (wt / ".git").unlink()  # git would now discover the PARENT (base) repo from wt
    head = _head(tmp_path)

    with pytest.raises(isolation_mod.IntegrationError):
        mgr.integrate("t1")

    assert _head(tmp_path) == head
    assert _user_git(tmp_path, "status", "--porcelain", "--untracked-files=no")[1] == "M f.txt"
    assert (tmp_path / "f.txt").read_text() == "user uncommitted WIP\n"


def test_integrate_refuses_worktree_switched_to_another_branch(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "task.txt").write_text("task\n")
    assert _user_git(wt, "checkout", "-q", "-b", "elsewhere")[0] == 0
    head = _head(tmp_path)

    with pytest.raises(isolation_mod.IntegrationError, match="refs/heads/elsewhere"):
        mgr.integrate("t1")

    assert _head(tmp_path) == head
    assert _user_git(tmp_path, "rev-parse", "elsewhere")[1] == head  # nothing committed


FAKE_GPG = """#!/bin/sh
# Test double for gpg: "signs" anything (no keys, no network) and reports success.
cat >/dev/null
printf '[GNUPG:] BEGIN_SIGNING\\n[GNUPG:] SIG_CREATED D 1 8 00 0 FAKE\\n' >&2
printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nfake\\n-----END PGP SIGNATURE-----\\n'
"""


@pytest.mark.parametrize("signer", ["broken", "working"])
def test_integrate_reports_true_only_when_the_task_reached_the_base(
    tmp_path, tmp_path_factory, signer,
):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "task.txt").write_text("task\n")
    # commit.gpgsign is set: Foundry's task commit must be signed (commit-tree ignores
    # the setting, so -S is required) or, if signing cannot work, fail loudly.
    program = "false"
    if signer == "working":
        fake = tmp_path_factory.mktemp("gpg") / "fake-gpg"
        fake.write_text(FAKE_GPG)
        fake.chmod(0o700)
        program = str(fake)
    assert _user_git(tmp_path, "config", "commit.gpgsign", "true")[0] == 0
    assert _user_git(tmp_path, "config", "gpg.program", program)[0] == 0
    head = _head(tmp_path)

    outcome: object
    try:
        outcome = mgr.integrate("t1")
    except RuntimeError as exc:
        outcome = exc

    assert (outcome is True) == (tmp_path / "task.txt").exists()
    if signer == "working":
        assert outcome is True
        task_commit = _user_git(tmp_path, "cat-file", "commit", "HEAD^2")[1]
        assert "gpgsig -----BEGIN PGP SIGNATURE-----" in task_commit
    else:
        assert outcome is not True
        assert _head(tmp_path) == head
        assert not (tmp_path / ".git" / "MERGE_HEAD").exists()


# --- Blocker 3: a mid-wave exception must report every worktree and run no worker after it.


class _WritingWorker:
    name = "writing"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system, instruction, workspace, seat="generalist") -> str:
        self.calls.append(instruction)
        (Path(workspace) / f"{instruction.split()[-1]}.txt").write_text("x")
        return "ok"


def test_integrate_exception_reports_every_worktree_and_propagates(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)

    def timeout(self, task_id):
        raise TimeoutError("git merge timed out")

    monkeypatch.setattr(WorktreeManager, "integrate", timeout)
    _forbid_destruction(monkeypatch)
    events: list[tuple[str, str]] = []
    with pytest.raises(TimeoutError, match="git merge timed out"):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)

    on_disk = sorted(p.name for p in (tmp_path / ".foundry" / "worktrees").iterdir())
    retained = " ".join(m for k, m in events if k == "worktree-retained")
    assert len(on_disk) == 2
    assert all(name in retained for name in on_disk)


def test_context_failure_after_first_task_runs_no_worker_and_reports(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)

    class Library:
        staged = 0

        def select_for(self, *args):
            return []

        def stage(self, chosen, workspace):
            Library.staged += 1
            if Library.staged == 2:
                raise OSError("stage failed")
            return ""

    monkeypatch.setattr(engine.SkillLibrary, "load", staticmethod(lambda root: Library()))
    worker = _WritingWorker()
    events: list[tuple[str, str]] = []
    with pytest.raises(OSError, match="stage failed"):
        _build(tmp_path, session, fake_provider, worker, events)

    assert worker.calls == []
    on_disk = sorted(p.name for p in (tmp_path / ".foundry" / "worktrees").iterdir())
    retained = " ".join(m for k, m in events if k == "worktree-retained")
    assert len(on_disk) == 2 and all(name in retained for name in on_disk)


def test_failing_reports_do_not_mask_the_original_error_or_skip_records(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)

    def timeout(self, task_id):
        raise TimeoutError("original")

    monkeypatch.setattr(WorktreeManager, "integrate", timeout)
    reports: list[str] = []

    def on_event(kind, msg):
        if kind == "worktree-retained":
            reports.append(msg)
            raise OSError("event sink broken")

    with pytest.raises(TimeoutError, match="original"):
        engine.build(
            "x", workspace=str(tmp_path), provider=fake_provider, worker=_WritingWorker(),
            session_path=str(session), isolation="worktree", discovery=False,
            use_archetypes=False, verify=False, review=False, on_event=on_event,
        )
    assert len(reports) == 2  # each record attempted despite the first report failing


def test_refused_integration_fails_the_task_and_build_continues(fake_provider, tmp_path):
    session = _two_task_session(tmp_path)
    (tmp_path / "base.txt").write_text("base\n")

    class GitfileDeletingWorker(_WritingWorker):
        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            out = super().execute(system, instruction, workspace, seat)
            if instruction.endswith("one"):
                (Path(workspace) / ".git").unlink()
            return out

    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, GitfileDeletingWorker(), events)

    kinds = [k for k, _ in events]
    assert "integrate-refused" in kinds
    assert not state.done
    assert (tmp_path / "two.txt").exists() and not (tmp_path / "one.txt").exists()
    assert sum(k == "worktree-retained" for k in kinds) == 2


# --- Regressions: detached HEAD, init fallback, scan failures, leftovers, chmod failure.


def test_detached_head_workspace_still_isolates_and_merges(tmp_path):
    (tmp_path / "f.txt").write_text("x\n")
    assert _user_git(tmp_path, "init", "-q")[0] == 0
    assert _user_git(tmp_path, "add", "-A")[0] == 0
    assert _user_git(tmp_path, "commit", "-qm", "init")[0] == 0
    assert _user_git(tmp_path, "checkout", "-q", "--detach")[0] == 0  # common in CI
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup() is True
    wt = mgr.create("t1")
    (wt / "task.txt").write_text("task\n")
    assert mgr.integrate("t1") is True
    assert (tmp_path / "task.txt").read_text() == "task\n"


def test_failed_git_init_falls_back_to_shared_workspace(monkeypatch, tmp_path):
    git = isolation_mod._git

    def failing_init(args, cwd):
        return (1, "injected init failure") if args[:1] == ["init"] else git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_init)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup() is False
    assert not mgr.enabled


def test_legacy_scan_failure_is_a_warning_not_a_build_abort(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git

    def failing_list(args, cwd):
        if args[:2] == ["worktree", "list"]:
            return 128, "fatal: injected list failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_list)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, fake_worker, events)
    assert state.done and len(fake_worker.calls) == 2
    assert any("injected list failure" in m for k, m in events if k == "worktree-warning")


def test_leftovers_from_earlier_builds_are_reported_not_deleted(
    monkeypatch, fake_provider, fake_worker, tmp_path,
):
    session = _two_task_session(tmp_path)
    earlier = WorktreeManager(tmp_path)
    assert earlier.setup()
    old_wt = earlier.create("t1")
    old_ref = earlier.worktree("t1").ref
    (old_wt / "old.txt").write_text("earlier build's work")
    assert _user_git(tmp_path, "branch", "foundry-wt/stray", "HEAD")[0] == 0

    _forbid_destruction(monkeypatch)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, fake_worker, events)

    assert state.done
    leftovers = [m for k, m in events if k == "worktree-leftover"]
    joined = " ".join(leftovers)
    assert str(old_wt) in joined and old_ref in joined and "refs/heads/foundry-wt/stray" in joined
    # This build's own worktrees are reported as retained, not as leftovers.
    assert len(leftovers) == 3
    assert (old_wt / "old.txt").read_text() == "earlier build's work"
    assert _branch_exists("foundry-wt/stray", tmp_path)


def test_chmod_failure_after_mkdir_records_the_directory(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    real_chmod = Path.chmod

    def failing_chmod(self, mode, **kwargs):
        if self.parent == mgr.root:
            raise PermissionError("injected chmod failure")
        return real_chmod(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", failing_chmod)
    with pytest.raises(PermissionError, match="injected chmod failure"):
        mgr.create("t1")
    assert len(mgr.unowned) == 1
    assert mgr.unowned[0].path.parent == mgr.root and mgr.unowned[0].path.is_dir()


def test_worker_commits_inside_its_worktree_still_merge(monkeypatch, tmp_path):
    # Guard: Foundry commits the worktree's files on the recorded start, so work a
    # worker committed itself is still integrated (flattened into one task commit).
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "committed.txt").write_text("worker commit\n")
    assert _user_git(wt, "add", "committed.txt")[0] == 0
    assert _user_git(wt, "commit", "-qm", "worker")[0] == 0
    (wt / "loose.txt").write_text("uncommitted\n")
    _forbid_destruction(monkeypatch)
    assert mgr.integrate("t1") is True
    assert (tmp_path / "committed.txt").exists() and (tmp_path / "loose.txt").exists()


# --- Review 2 blocker: a merge must never overwrite or (via its own abort) delete
# untracked/ignored files in the base working tree.

PLACEHOLDER = b"API_KEY=placeholder-user-value\n"  # placeholder, never a real secret


def _conflicting_pair(tmp_path, gitignore=None):
    """A manager whose task "b" will conflict on shared.txt (task "a" already merged)."""
    (tmp_path / "shared.txt").write_text("base\n")
    if gitignore is not None:
        (tmp_path / ".gitignore").write_text(gitignore)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    a, b = mgr.create("a"), mgr.create("b")
    (a / "shared.txt").write_text("from-a\n")
    assert mgr.integrate("a") is True
    (b / "shared.txt").write_text("from-b\n")
    return mgr, b


def _plain_pair(tmp_path, gitignore=None):
    (tmp_path / "shared.txt").write_text("base\n")
    if gitignore is not None:
        (tmp_path / ".gitignore").write_text(gitignore)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    return mgr, mgr.create("b")


def _assert_refused_untouched(monkeypatch, mgr, tmp_path, user_files, conflict=False):
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    if conflict:  # found by `git merge-tree` alone: reported as a conflict, nothing touched
        assert mgr.integrate("b") is False
        assert "CONFLICT" in mgr.last_conflict
    else:
        with pytest.raises(isolation_mod.IntegrationError, match="untracked or ignored"):
            mgr.integrate("b")
    assert not any(c[:1] == ["merge"] for c in calls)  # refused before touching base
    assert _head(tmp_path) == head
    for path, content in user_files.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("conflict", [False, True])
def test_merge_never_replaces_or_deletes_a_locally_ignored_base_file(
    monkeypatch, tmp_path, conflict,
):
    # (i) The user locally ignores .env (unstaged .gitignore edit) and keeps one;
    # the worker scaffolds its own .env, which its worktree does not ignore.
    mgr, b = (_conflicting_pair if conflict else _plain_pair)(tmp_path)
    (tmp_path / ".gitignore").write_text(".foundry/\n.env\n")
    (tmp_path / ".env").write_bytes(PLACEHOLDER)
    (b / ".env").write_text("API_KEY=changeme\n")
    _assert_refused_untouched(monkeypatch, mgr, tmp_path, {tmp_path / ".env": PLACEHOLDER}, conflict)


@pytest.mark.parametrize("conflict", [False, True])
def test_merge_never_replaces_or_deletes_a_base_file_the_worker_unignored(
    monkeypatch, tmp_path, conflict,
):
    # (ii) The committed .gitignore ignores secret.env; the worker un-ignores it and
    # adds a file at the same path; the base keeps an ignored secret.env.
    gitignore = ".foundry/\nsecret.env\n"
    mgr, b = (_conflicting_pair if conflict else _plain_pair)(tmp_path, gitignore)
    (tmp_path / "secret.env").write_bytes(PLACEHOLDER)
    (b / ".gitignore").write_text(".foundry/\n")
    (b / "secret.env").write_text("task value\n")
    _assert_refused_untouched(monkeypatch, mgr, tmp_path, {tmp_path / "secret.env": PLACEHOLDER}, conflict)


@pytest.mark.parametrize("conflict", [False, True])
def test_merge_never_replaces_an_ignored_base_file_with_a_task_directory(
    monkeypatch, tmp_path, conflict,
):
    # Directory/file collision: base has an ignored FILE `cache`; the task adds `cache/x`.
    gitignore = ".foundry/\ncache\n"
    mgr, b = (_conflicting_pair if conflict else _plain_pair)(tmp_path, gitignore)
    (tmp_path / "cache").write_bytes(PLACEHOLDER)
    (b / ".gitignore").write_text(".foundry/\n")
    (b / "cache").mkdir()
    (b / "cache" / "x").write_text("task\n")
    _assert_refused_untouched(monkeypatch, mgr, tmp_path, {tmp_path / "cache": PLACEHOLDER}, conflict)


@pytest.mark.parametrize("conflict", [False, True])
def test_merge_never_replaces_an_ignored_base_directory_with_a_task_file(
    monkeypatch, tmp_path, conflict,
):
    # File/directory collision: base has an ignored DIRECTORY `build/`; task adds file `build`.
    gitignore = ".foundry/\nbuild/\n"
    mgr, b = (_conflicting_pair if conflict else _plain_pair)(tmp_path, gitignore)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.bin").write_bytes(PLACEHOLDER)
    (b / ".gitignore").write_text(".foundry/\n")
    (b / "build").write_text("task file\n")
    _assert_refused_untouched(
        monkeypatch, mgr, tmp_path, {tmp_path / "build" / "out.bin": PLACEHOLDER}, conflict,
    )


def test_task_files_merge_when_no_base_file_is_in_the_way(tmp_path):
    # Guard against over-refusal: new paths absent from the base still merge, even
    # inside an existing ignored directory and alongside an unrelated ignored file.
    mgr, b = _plain_pair(tmp_path, ".foundry/\n.env\nlogs/\n")
    (tmp_path / ".env").write_bytes(PLACEHOLDER)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "user.log").write_bytes(PLACEHOLDER)
    (b / "app.py").write_text("print('hi')\n")
    (b / "pkg").mkdir()
    (b / "pkg" / "mod.py").write_text("x = 1\n")
    assert mgr.integrate("b") is True
    assert (tmp_path / "app.py").exists() and (tmp_path / "pkg" / "mod.py").exists()
    assert (tmp_path / ".env").read_bytes() == PLACEHOLDER
    assert (tmp_path / "logs" / "user.log").read_bytes() == PLACEHOLDER


# --- Review 2 item 1: a worktree left mid-operation must not be committed as-is.


def _worktree_with_conflict(tmp_path, how):
    (tmp_path / "f.txt").write_text("base\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    if how == "merge":
        assert _user_git(wt, "checkout", "-q", "-b", "side")[0] == 0
        (wt / "f.txt").write_text("side\n")
        assert _user_git(wt, "commit", "-qam", "side")[0] == 0
        assert _user_git(wt, "checkout", "-q", mgr.worktree("t1").branch)[0] == 0
        (wt / "f.txt").write_text("mine\n")
        assert _user_git(wt, "commit", "-qam", "mine")[0] == 0
        assert _user_git(wt, "merge", "side")[0] != 0  # conflict: MERGE_HEAD + markers
    else:  # a conflicting `stash pop` leaves unmerged paths but no MERGE_HEAD
        (wt / "f.txt").write_text("stashed\n")
        assert _user_git(wt, "stash", "-q")[0] == 0
        (wt / "f.txt").write_text("committed\n")
        assert _user_git(wt, "commit", "-qam", "c")[0] == 0
        assert _user_git(wt, "stash", "pop")[0] != 0
    assert "<<<<<<<" in (wt / "f.txt").read_text()
    return mgr


@pytest.mark.parametrize("how", ["merge", "stash-pop"])
def test_integrate_refuses_a_worktree_left_mid_conflict(monkeypatch, tmp_path, how):
    mgr = _worktree_with_conflict(tmp_path, how)
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match=r"unmerged|in progress"):
        mgr.integrate("t1")
    assert not any(c[:1] in (["add"], ["merge"]) for c in calls)  # nothing staged/merged
    assert _head(tmp_path) == head
    assert (tmp_path / "f.txt").read_text() == "base\n"


# --- Review 2 item 2: git's reason for a failed merge must reach the caller.


def test_merge_refused_by_git_raises_with_gits_reason(monkeypatch, tmp_path):
    (tmp_path / "y.txt").write_text("y\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "y.txt").write_text("task\n")
    (tmp_path / "y.txt").write_text("user unstaged WIP\n")  # git refuses to overwrite it
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match=r"(?s)overwritten.*y\.txt"):
        mgr.integrate("t1")
    assert not any(c[:1] == ["merge"] for c in calls)  # refused up front: no merge at all
    assert (tmp_path / "y.txt").read_text() == "user unstaged WIP\n"
    assert _head(tmp_path) == head


def test_merge_conflict_event_carries_gits_reason(fake_provider, tmp_path):
    class SameFileWorker(_WritingWorker):
        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            (Path(workspace) / "shared.txt").write_text(f"{instruction}\n")
            return "ok"

    session = _two_task_session(tmp_path)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, SameFileWorker(), events)
    conflicts = [m for k, m in events if k == "merge-conflict"]
    assert not state.done and len(conflicts) == 1
    assert "CONFLICT" in conflicts[0] and "shared.txt" in conflicts[0]


# --- Review 2 item 3: a nested repo would be merged as an empty gitlink.


def test_integrate_refuses_a_task_that_adds_a_nested_repository(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    assert _user_git(nested, "init", "-q")[0] == 0
    (nested / "lib.py").write_text("x = 1\n")
    assert _user_git(nested, "add", "lib.py")[0] == 0
    assert _user_git(nested, "commit", "-qm", "nested")[0] == 0
    (wt / "app.py").write_text("import vendor\n")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match=r"vendor/lib.*nested|nested.*vendor/lib"):
        mgr.integrate("t1")
    assert not any(c[:1] == ["merge"] for c in calls)
    assert _head(tmp_path) == head
    assert not (tmp_path / "vendor").exists()
    assert (nested / "lib.py").read_text() == "x = 1\n"  # the worker's nested repo is untouched


# --- Review 2 item 4: Foundry scratch must never enter the task commit.


@pytest.mark.parametrize("worker_commits", [False, True])
def test_foundry_scratch_never_enters_base_history(tmp_path, worker_commits):
    # The user's committed .gitignore lacks `.foundry/`, so worktrees don't ignore it.
    (tmp_path / ".gitignore").write_text("*.log\n")
    assert _user_git(tmp_path, "init", "-q")[0] == 0
    assert _user_git(tmp_path, "add", ".gitignore")[0] == 0
    assert _user_git(tmp_path, "commit", "-qm", "user")[0] == 0
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    committed_gitignore = _user_git(tmp_path, "show", "HEAD:.gitignore")[1]
    wt = mgr.create("t1")
    (wt / ".foundry" / "skills").mkdir(parents=True)
    (wt / ".foundry" / "skills" / "s.md").write_text("staged skill\n")
    (wt / "app.py").write_text("print('hi')\n")
    if worker_commits:  # e.g. a coding agent that runs `git add -A && git commit`
        assert _user_git(wt, "add", "-A")[0] == 0
        assert _user_git(wt, "commit", "-qm", "worker")[0] == 0
    assert mgr.integrate("t1") is True
    tracked = _user_git(tmp_path, "ls-tree", "-r", "--name-only", "HEAD")[1].split()
    assert "app.py" in tracked
    assert not any(p.startswith(".foundry") for p in tracked)
    assert _user_git(tmp_path, "show", "HEAD:.gitignore")[1] == committed_gitignore


# --- Review 2 item 5: tests that fail when a safety step is removed.


def test_queued_workers_never_start_after_the_wave_aborts(monkeypatch, fake_provider, tmp_path):
    import threading

    plan = Plan(app_name="a", stack="s", summary="", tasks=[
        Task(id=f"t{i}", title=name, role="backend", instruction=name)
        for i, name in enumerate(["one", "two", "three"], 1)
    ])
    session = tmp_path / "session.json"
    save(session, BuildState(brief="x", workspace=str(tmp_path), plan=plan))
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)  # t3 waits in the queue
    two_started, wave_aborted = threading.Event(), threading.Event()

    class BlockingSecondWorker(_WritingWorker):
        def execute(self, system, instruction, workspace, seat="generalist") -> str:
            out = super().execute(system, instruction, workspace, seat)
            if instruction.split()[-1] == "two":
                two_started.set()
                # Hold the single pool thread until the engine's abort cleanup has
                # run (it cancels queued futures before discarding any worktree).
                assert wave_aborted.wait(timeout=30)
            return out

    def timeout(self, task_id):
        assert two_started.wait(timeout=30)  # t2 is running when t1's integrate fails
        raise TimeoutError("git merge timed out")

    discard = WorktreeManager.discard

    def discard_after_cancel(self, task_id):
        wave_aborted.set()
        return discard(self, task_id)

    monkeypatch.setattr(WorktreeManager, "integrate", timeout)
    monkeypatch.setattr(WorktreeManager, "discard", discard_after_cancel)
    worker = BlockingSecondWorker()
    with pytest.raises(TimeoutError):
        _build(tmp_path, session, fake_provider, worker)
    started = [c.split()[-1] for c in worker.calls]
    assert started == ["one", "two"]  # t3 was cancelled while still queued


def test_failed_worktree_add_is_reported_as_unowned(monkeypatch, fake_provider, fake_worker, tmp_path):
    session = _two_task_session(tmp_path)
    git = isolation_mod._git
    adds: list[list[str]] = []

    def failing_git(args, cwd):
        if _is_add(args):
            adds.append(list(args))
            if len(adds) == 2:
                return 1, "injected second worktree failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_git)
    events: list[tuple[str, str]] = []
    with pytest.raises(RuntimeError, match="injected second worktree failure"):
        _build(tmp_path, session, fake_provider, fake_worker, events)
    unowned = [m for k, m in events if k == "worktree-unowned"]
    assert len(unowned) == 1
    assert adds[1][4] in unowned[0] and adds[1][3] in unowned[0]


def test_create_never_adopts_an_owner_token_planted_in_the_new_gitdir(monkeypatch, tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    git = isolation_mod._git
    planted: list[Path] = []

    def plant_token(args, cwd):
        result = git(args, cwd)
        if _is_add(args) and result[0] == 0:
            # Another actor writes its own token into the fresh admin dir first.
            gitdir = Path(git(["rev-parse", "--absolute-git-dir"], Path(args[4]))[1])
            owner = gitdir / WorktreeManager.OWNER_FILE
            owner.write_text("someone else's token, long enough to show overwrites")
            planted.append(owner)
        return result

    monkeypatch.setattr(isolation_mod, "_git", plant_token)
    with pytest.raises(FileExistsError):
        mgr.create("t1")
    assert mgr.worktree("t1") is None
    assert len(mgr.unowned) == 1
    assert planted[0].read_text() == "someone else's token, long enough to show overwrites"


# --- Review 2 item 6: the merge message names what was actually merged.


def test_merge_message_names_the_task_and_the_merged_commit(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    branch = mgr.worktree("t1").branch
    (wt / "task.txt").write_text("task\n")
    assert mgr.integrate("t1") is True
    task_commit = _user_git(tmp_path, "rev-parse", "HEAD^2")[1]
    subject = _user_git(tmp_path, "log", "-1", "--format=%s", "HEAD")[1]
    assert "t1" in subject and task_commit in subject
    assert branch not in subject  # that branch never points at the merged commit


def test_integrate_refuses_a_worktree_whose_core_worktree_points_elsewhere(tmp_path):
    # Same admin dir and nonce, but git's work tree is redirected to the base: without
    # the top-level check, `add -A` would commit the user's base WIP as the task.
    (tmp_path / "f.txt").write_text("base\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    assert _user_git(tmp_path, "config", "extensions.worktreeConfig", "true")[0] == 0
    assert _user_git(wt, "config", "--worktree", "core.worktree", str(tmp_path))[0] == 0
    (tmp_path / "f.txt").write_text("user uncommitted WIP\n")
    head = _head(tmp_path)
    with pytest.raises(isolation_mod.IntegrationError, match="top level"):
        mgr.integrate("t1")
    assert _head(tmp_path) == head
    assert (tmp_path / "f.txt").read_text() == "user uncommitted WIP\n"


def test_integrate_refuses_a_worktree_redirected_to_another_admin_dir(tmp_path):
    # The recorded admin dir (and its nonce) is intact, but the worktree's .git file
    # now points at another worktree's admin dir whose HEAD names Foundry's branch.
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    rec = mgr.worktree("t1")
    other = tmp_path.parent / f"{tmp_path.name}-other"
    assert _user_git(tmp_path, "worktree", "add", "-q", "-b", "other", str(other))[0] == 0
    decoy = Path(_user_git(other, "rev-parse", "--absolute-git-dir")[1])
    (decoy / "HEAD").write_text(f"ref: {rec.ref}\n")
    (wt / ".git").write_text(f"gitdir: {decoy}\n")
    (wt / "task.txt").write_text("task\n")
    index_before = (decoy / "index").read_bytes()
    head = _head(tmp_path)
    with pytest.raises(isolation_mod.IntegrationError, match="git dir"):
        mgr.integrate("t1")
    assert _head(tmp_path) == head
    assert (decoy / "index").read_bytes() == index_before  # other actor's index untouched


# --- Review 4: the merge result is computed with `git merge-tree` before the base is touched.


def _base_is_quiet(cwd):
    """No merge in progress, no unmerged paths, no autostash left behind."""
    git_dir = cwd / ".git"
    assert not (git_dir / "MERGE_HEAD").exists()
    assert not (git_dir / "MERGE_AUTOSTASH").exists()
    assert _user_git(cwd, "ls-files", "--unmerged")[1] == ""
    assert _user_git(cwd, "stash", "list")[1] == ""


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("user_setting", [None, "true", "conflict"])
def test_directory_rename_never_relocates_a_task_file_onto_a_user_file(
    monkeypatch, tmp_path, user_setting, conflict,
):
    # Task a moves app/ -> svc/; task b (same start) adds app/.env. Git's directory
    # rename detection would place b's file at svc/.env, where the user keeps an
    # ignored file. Foundry pins merge.directoryRenames=false: the task's file lands
    # where the worker wrote it, and the user's file is never written or deleted.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)\n")
    (tmp_path / "app" / "util.py").write_text("x = 1\n")
    (tmp_path / "shared.txt").write_text("base\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    if user_setting is not None:
        assert _user_git(tmp_path, "config", "merge.directoryRenames", user_setting)[0] == 0
    (tmp_path / ".gitignore").write_text(".foundry/\n.env\n")  # unstaged: user ignores .env
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / ".env").write_bytes(PLACEHOLDER)
    a, b = mgr.create("a"), mgr.create("b")
    shutil.move(str(a / "app"), str(a / "svc"))
    if conflict:
        (a / "shared.txt").write_text("from-a\n")
        (b / "shared.txt").write_text("from-b\n")
    assert mgr.integrate("a") is True
    (b / "app" / ".env").write_text("API_KEY=changeme\n")
    user_file = _stat(tmp_path / "svc" / ".env")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)

    outcome = mgr.integrate("b")

    assert _stat(tmp_path / "svc" / ".env") == user_file
    _base_is_quiet(tmp_path)
    assert ["merge", "--abort"] not in calls
    assert _user_git(tmp_path, "cat-file", "-e", "HEAD:svc/.env")[0] != 0
    if conflict:
        assert outcome is False
        assert not any(c[:1] == ["merge"] for c in calls)
        assert _head(tmp_path) == head
    else:
        assert outcome is True
        assert _user_git(tmp_path, "show", "HEAD:app/.env")[1] == "API_KEY=changeme"
        assert (tmp_path / "app" / ".env").read_text() == "API_KEY=changeme\n"


@pytest.mark.parametrize("overlap", [False, True])
def test_autostash_config_never_stashes_or_rewrites_user_edits(monkeypatch, tmp_path, overlap):
    # With merge.autoStash=true git would stash the user's unstaged edit, merge, and
    # re-apply it (conflict markers, `UU`, a leftover stash). Foundry pins it off and
    # refuses up front a task that changes a file the user is editing.
    (tmp_path / "f.txt").write_text("line1\nline2\n")
    (tmp_path / "g.txt").write_text("g\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    assert _user_git(tmp_path, "config", "merge.autoStash", "true")[0] == 0
    b = mgr.create("b")
    if overlap:
        (b / "f.txt").write_text("TASK line1\nline2\n")
    else:
        (b / "g.txt").write_text("task g\n")
    (tmp_path / "f.txt").write_text("USER line1\nline2\n")  # the user's unstaged edit
    user_file = _stat(tmp_path / "f.txt")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)

    if overlap:
        with pytest.raises(isolation_mod.IntegrationError, match=r"(?s)overwritten.*f\.txt"):
            mgr.integrate("b")
        assert not any(c[:1] == ["merge"] for c in calls)
        assert _head(tmp_path) == head
    else:
        assert mgr.integrate("b") is True
        assert (tmp_path / "g.txt").read_text() == "task g\n"
    assert _stat(tmp_path / "f.txt") == user_file
    _base_is_quiet(tmp_path)
    assert _user_git(tmp_path, "status", "--porcelain", "--", "f.txt")[1] == "M f.txt"


@pytest.mark.parametrize("in_the_way", [None, "ignored", "untracked"])
def test_task_replacing_a_tracked_directory_with_a_file(monkeypatch, tmp_path, in_the_way):
    # A tracked directory on disk is not an untracked collision; only untracked or
    # ignored content inside it (which git would delete or refuse over) is.
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.txt").write_text("a\n")
    (tmp_path / "d" / "sub" / "c.txt").write_text("c\n")
    (tmp_path / ".gitignore").write_text(".foundry/\n*.bin\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    b = mgr.create("b")
    shutil.rmtree(b / "d")
    (b / "d").write_text("now a file\n")
    if in_the_way is None:
        assert mgr.integrate("b") is True
        assert (tmp_path / "d").read_text() == "now a file\n"
        return
    extra = tmp_path / "d" / "sub" / ("cache.bin" if in_the_way == "ignored" else "notes.txt")
    extra.write_bytes(PLACEHOLDER)
    _assert_refused_untouched(monkeypatch, mgr, tmp_path, {extra: PLACEHOLDER})
    assert (tmp_path / "d" / "a.txt").read_text() == "a\n"


def _race_after_merge_tree(monkeypatch, tmp_path, user_action, after="merge-tree"):
    """Run `user_action` (another same-user Git actor) right after Foundry's git
    command `after` (default `merge-tree`: between the checked result and applying it)."""
    git = isolation_mod._git

    def racing_git(args, cwd):
        result = git(args, cwd)
        if _cmd(args)[:1] == [after]:
            user_action()
        return result

    monkeypatch.setattr(isolation_mod, "_git", racing_git)


def _commit_as_user(cwd, name, text):
    (cwd / name).write_text(text)
    assert _user_git(cwd, "add", name)[0] == 0
    assert _user_git(cwd, "commit", "-qm", f"user {name}")[0] == 0


def test_base_changing_after_the_check_is_refused_without_writing(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "shared.txt").write_text("from-b\n")
    _race_after_merge_tree(monkeypatch, tmp_path,
                           lambda: _commit_as_user(tmp_path, "shared.txt", "user\n"))
    calls = _forbid_destruction(monkeypatch)

    with pytest.raises(isolation_mod.IntegrationError, match="moved") as info:
        mgr.integrate("b")

    assert info.value.merge_ran is False
    assert not any(c[:1] in (["merge"], ["read-tree"]) for c in calls)
    assert (tmp_path / "shared.txt").read_text() == "user\n"
    _base_is_quiet(tmp_path)


def test_merge_result_differing_from_the_checked_tree_is_reported(monkeypatch, tmp_path):
    # Another actor commits right after Foundry moved HEAD: the base no longer shows
    # the checked merge, which is reported (never reset) for manual inspection.
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    _race_after_merge_tree(monkeypatch, tmp_path,
                           lambda: _commit_as_user(tmp_path, "other.txt", "user\n"),
                           after="update-ref")
    calls = _forbid_destruction(monkeypatch)

    with pytest.raises(isolation_mod.MergeUnverifiedError, match="differs from the checked"):
        mgr.integrate("b")

    assert (tmp_path / "other.txt").read_text() == "user\n"  # never reset away
    assert (tmp_path / "task.txt").read_text() == "task\n"
    _no_porcelain_merge(calls)
    _base_is_quiet(tmp_path)


def test_branch_merge_options_cannot_change_how_the_task_is_merged(tmp_path):
    mgr, b = _plain_pair(tmp_path)
    assert _user_git(tmp_path, "config", f"branch.{mgr.base}.mergeOptions", "--squash")[0] == 0
    (b / "task.txt").write_text("task\n")
    assert mgr.integrate("b") is True
    parents = _user_git(tmp_path, "rev-list", "--parents", "-n1", "HEAD")[1].split()
    assert len(parents) == 3  # a real two-parent merge commit, not a squash
    _base_is_quiet(tmp_path)


# --- Review 5: edits git is told not to look at (assume-unchanged / skip-worktree).
# Git's merge, on a strategy failure, resets tracked files to HEAD and re-applies a
# stash that never captured such edits, so they would be lost; Foundry refuses first.


def _index_entries(cwd):
    """Index entries with their assume-unchanged/skip-worktree tags (`ls-files -s -v`)."""
    return _user_git(cwd, "ls-files", "-s", "-v")[1]


def _flagged_base(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    return mgr, mgr.create("b")


def _flag(cwd, flag, *paths):
    assert _user_git(cwd, "update-index", f"--{flag}", *paths)[0] == 0


def _assert_refused_flagged(monkeypatch, mgr, tmp_path, user_files):
    head, index = _head(tmp_path), _index_entries(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match="assume-unchanged or skip-worktree"):
        mgr.integrate("b")
    assert not any(c[:1] == ["merge"] for c in calls)  # refused before git merge ran
    assert _head(tmp_path) == head
    assert _index_entries(tmp_path) == index  # flags and entries untouched
    for path, content in user_files.items():
        assert path.read_bytes() == content
    _base_is_quiet(tmp_path)


@pytest.mark.parametrize("task_action", ["modify", "delete"])
@pytest.mark.parametrize("flag", ["assume-unchanged", "skip-worktree"])
def test_hidden_edit_on_a_path_the_task_changes_is_refused_and_kept(
    monkeypatch, tmp_path, flag, task_action,
):
    mgr, b = _flagged_base(tmp_path, {"cfg.txt": "orig\n", "other.txt": "o\n"})
    if task_action == "modify":
        (b / "cfg.txt").write_text("TASK\n")
    else:
        (b / "cfg.txt").unlink()
    _flag(tmp_path, flag, "cfg.txt")
    (tmp_path / "cfg.txt").write_bytes(PLACEHOLDER)  # the user's hidden local edit
    (tmp_path / "other.txt").write_text("user unstaged other\n")
    _assert_refused_flagged(monkeypatch, mgr, tmp_path, {
        tmp_path / "cfg.txt": PLACEHOLDER, tmp_path / "other.txt": b"user unstaged other\n",
    })


def test_skip_worktree_trigger_never_wipes_an_unrelated_assume_unchanged_edit(
    monkeypatch, tmp_path,
):
    # A skip-worktree edit on a path the task changes would make git's merge fail;
    # its failure path would then reset an assume-unchanged edit on an UNRELATED path.
    mgr, b = _flagged_base(tmp_path, {"A.txt": "a\n", "B.txt": "b\n"})
    (b / "A.txt").write_text("task a\n")
    _flag(tmp_path, "skip-worktree", "A.txt")
    (tmp_path / "A.txt").write_text("user a\n")
    _flag(tmp_path, "assume-unchanged", "B.txt")
    (tmp_path / "B.txt").write_bytes(PLACEHOLDER)
    _assert_refused_flagged(monkeypatch, mgr, tmp_path, {
        tmp_path / "A.txt": b"user a\n", tmp_path / "B.txt": PLACEHOLDER,
    })


@pytest.mark.parametrize("change", ["edit", "delete", "chmod", "symlink"])
def test_any_hidden_edit_anywhere_in_the_base_is_refused(monkeypatch, tmp_path, change):
    # Even on a path the task does not touch: any failure of the real merge would
    # reset it, so Foundry refuses while such an edit exists.
    mgr, b = _flagged_base(tmp_path, {"cfg.txt": "orig\n"})
    (b / "task.txt").write_text("task\n")
    _flag(tmp_path, "assume-unchanged", "cfg.txt")
    cfg = tmp_path / "cfg.txt"
    expected: dict[Path, bytes] = {}
    if change == "edit":
        cfg.write_bytes(PLACEHOLDER)
        expected[cfg] = PLACEHOLDER
    elif change == "delete":
        cfg.unlink()
    elif change == "chmod":
        cfg.chmod(0o755)
        expected[cfg] = b"orig\n"
    else:
        cfg.unlink()
        cfg.symlink_to("orig\n")  # same bytes as the blob, but a symlink now
    _assert_refused_flagged(monkeypatch, mgr, tmp_path, expected)
    assert not (tmp_path / "task.txt").exists()
    if change == "delete":
        assert not cfg.exists()  # the user's deletion was not undone
    if change == "symlink":
        assert cfg.is_symlink()


def test_core_ignore_stat_base_edit_is_refused_and_kept(monkeypatch, tmp_path):
    # With core.ignoreStat=true every checked-out entry is assume-unchanged, so the
    # user's ordinary edit is invisible to `git diff`.
    assert _user_git(tmp_path, "init", "-q")[0] == 0
    assert _user_git(tmp_path, "config", "core.ignoreStat", "true")[0] == 0
    mgr, b = _flagged_base(tmp_path, {"settings.ini": "debug=false\n"})
    (b / "settings.ini").write_text("debug=false\nfeature=1\n")
    (tmp_path / "settings.ini").write_bytes(PLACEHOLDER)
    _assert_refused_flagged(monkeypatch, mgr, tmp_path, {tmp_path / "settings.ini": PLACEHOLDER})


def test_clean_flagged_entries_do_not_block_a_merge(tmp_path):
    # Guard against over-refusal: an unmodified assume-unchanged file the task
    # changes merges normally (git updates it), and an absent skip-worktree file
    # (the normal sparse-checkout state) elsewhere stays absent.
    mgr, b = _flagged_base(tmp_path, {"app.py": "v1\n", "sparse.txt": "s\n"})
    _flag(tmp_path, "assume-unchanged", "app.py")
    _flag(tmp_path, "skip-worktree", "sparse.txt")
    (tmp_path / "sparse.txt").unlink()
    (b / "app.py").write_text("v2\n")
    assert mgr.integrate("b") is True
    assert (tmp_path / "app.py").read_text() == "v2\n"
    assert _user_git(tmp_path, "show", "HEAD:app.py")[1] == "v2"
    assert not (tmp_path / "sparse.txt").exists()
    _base_is_quiet(tmp_path)


def test_core_ignore_stat_never_drops_the_workers_edits(tmp_path):
    # core.ignoreStat=true marks every file checked out into Foundry's worktree
    # assume-unchanged; `add -A` would then miss the worker's edits to existing files
    # and integrate() would report success while dropping them.
    assert _user_git(tmp_path, "init", "-q")[0] == 0
    assert _user_git(tmp_path, "config", "core.ignoreStat", "true")[0] == 0
    mgr, b = _flagged_base(tmp_path, {"app.py": "v1\n", "gone.py": "old\n"})
    (b / "app.py").write_text("v2 from worker\n")
    (b / "gone.py").unlink()
    (b / "new.py").write_text("new\n")
    assert mgr.integrate("b") is True
    assert _user_git(tmp_path, "show", "HEAD:app.py")[1] == "v2 from worker"
    assert _user_git(tmp_path, "cat-file", "-e", "HEAD:gone.py")[0] != 0
    assert _user_git(tmp_path, "show", "HEAD:new.py")[1] == "new"
    assert (tmp_path / "app.py").read_text() == "v2 from worker\n"
    assert not (tmp_path / "gone.py").exists()
    _base_is_quiet(tmp_path)


def _hook(repo, name, body):
    hook = repo / ".git" / "hooks" / name
    hook.write_text(f"#!/bin/sh\n{body}\n")
    hook.chmod(0o755)


@pytest.mark.parametrize("hook", ["pre-merge-commit", "commit-msg", "prepare-commit-msg"])
def test_failing_commit_hooks_are_bypassed_but_post_merge_runs(monkeypatch, tmp_path, hook):
    # Foundry commits the checked merge with plumbing, so no commit hook runs (its
    # setup commit uses --no-verify too): a failing hook cannot stop the merge or be
    # misreported as a conflict. post-merge hooks still run; core.hooksPath is kept.
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    _hook(tmp_path, hook, "exit 1")
    _hook(tmp_path, "post-merge", "echo ran > .git/post-merge-ran")
    calls = _forbid_destruction(monkeypatch)
    assert mgr.integrate("b") is True
    assert (tmp_path / "task.txt").read_text() == "task\n"
    assert (tmp_path / ".git" / "post-merge-ran").exists()
    assert ["merge", "--abort"] not in calls
    assert not any("core.hooksPath" in arg for c in calls for arg in c)
    _base_is_quiet(tmp_path)


def test_verify_signatures_setting_refuses_an_unsigned_task_commit(monkeypatch, tmp_path):
    # merge.verifySignatures: git's merge would refuse an unsigned commit. The
    # plumbing Foundry uses does not check it, so Foundry checks it up front.
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    (tmp_path / "shared.txt").write_text("user unstaged\n")
    assert _user_git(tmp_path, "config", "merge.verifySignatures", "true")[0] == 0
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match="GPG signature") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert not any(c[:1] in (["merge"], ["read-tree"]) for c in calls)
    assert _head(tmp_path) == head
    assert not (tmp_path / "task.txt").exists()
    assert (tmp_path / "shared.txt").read_text() == "user unstaged\n"
    _base_is_quiet(tmp_path)


def test_refusals_before_the_merge_say_the_merge_never_ran(monkeypatch, tmp_path):
    mgr, b = _flagged_base(tmp_path, {"cfg.txt": "orig\n"})
    (b / "cfg.txt").write_text("TASK\n")
    (tmp_path / "cfg.txt").write_text("user\n")
    with pytest.raises(isolation_mod.IntegrationError) as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False


def test_unverified_merge_is_recorded_and_never_rerun_or_remerged(
    monkeypatch, fake_provider, tmp_path,
):
    # The task's merge commit landed but the base result differs from the checked one
    # (a concurrent user commit). It is neither "refused" nor retryable: a resumed
    # build must not run the task again or merge it a second time.
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)  # t1 integrates first
    raced = {"done": False}

    def user_commits_once():
        if not raced["done"]:
            raced["done"] = True
            _commit_as_user(tmp_path, "user.txt", "user\n")

    _race_after_merge_tree(monkeypatch, tmp_path, user_commits_once, after="update-ref")
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, _WritingWorker(), events)

    kinds = [k for k, _ in events]
    assert "integrate-unverified" in kinds and "integrate-refused" not in kinds
    assert "task-unverified" in kinds
    assert not state.done and state.done_ids == ["t2"]
    saved = BuildState.model_validate_json(session.read_text())
    assert {t.id: t.status for t in saved.plan.tasks}["t1"] == "unverified"

    resumed_worker = _WritingWorker()
    events.clear()
    state = _build(tmp_path, session, fake_provider, resumed_worker, events)
    assert resumed_worker.calls == []  # t1 not re-run (t2 already done)
    assert not state.done
    assert any(k == "task-unverified" and "one" in m for k, m in events)
    log = _user_git(tmp_path, "log", "--format=%s")[1]
    assert log.count("foundry: merge task t1 ") == 1  # merged exactly once


def test_failure_after_writing_the_base_is_not_reported_as_refused(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    git = isolation_mod._git

    def failing_update_ref(args, cwd):
        if _cmd(args)[:1] == ["update-ref"]:
            return 1, "fatal: injected ref failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_update_ref)
    events: list[tuple[str, str]] = []
    state = _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    kinds = [k for k, _ in events]
    assert kinds.count("integrate-failed") == 1  # the first task's merge was applied
    assert "merge-conflict" not in kinds
    # ... and left uncommitted, so the second task is refused rather than stacked on it.
    refused = [m for k, m in events if k == "integrate-refused"]
    assert len(refused) == 1 and "staged changes" in refused[0]
    assert not state.done


def test_task_changing_an_absent_skip_worktree_file_is_refused(monkeypatch, tmp_path):
    # The file is absent on purpose (sparse checkout); git's merge would write the
    # task's version there, widening the user's checkout behind their back.
    mgr, b = _flagged_base(tmp_path, {"sparse.txt": "s\n"})
    (b / "sparse.txt").write_text("task\n")
    _flag(tmp_path, "skip-worktree", "sparse.txt")
    (tmp_path / "sparse.txt").unlink()
    _assert_refused_flagged(monkeypatch, mgr, tmp_path, {})
    assert not (tmp_path / "sparse.txt").exists()


# --- Review 6: apply the checked result with plumbing, never porcelain `git merge`.
# On any strategy failure git 2.39's `merge` runs `read-tree --reset -u HEAD` and
# `stash apply`; if the reset itself fails, unstaged edits to files the task never
# touches are lost. Foundry applies the precomputed result tree instead.


def _no_porcelain_merge(calls):
    assert not any(c[:1] == ["merge"] for c in calls), calls


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_failed_apply_never_loses_unrelated_unstaged_edits(monkeypatch, tmp_path):
    for name, text in {"a.txt": "a\n", "b.txt": "b\n", "ro/keep": "k\n"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "ro" / "new.txt").write_text("task\n")
    (tmp_path / "b.txt").write_text("USER PRECIOUS EDIT\n")
    (tmp_path / "ro" / "keep").write_text("user edit keep\n")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    os.chmod(tmp_path / "ro", 0o555)
    try:
        with pytest.raises(isolation_mod.IntegrationError) as info:
            mgr.integrate("t1")
    finally:
        os.chmod(tmp_path / "ro", 0o755)
    assert info.value.merge_ran is True  # git may have written the task's own paths
    _no_porcelain_merge(calls)
    assert (tmp_path / "b.txt").read_text() == "USER PRECIOUS EDIT\n"
    assert (tmp_path / "ro" / "keep").read_text() == "user edit keep\n"
    assert not (tmp_path / "ro" / "new.txt").exists()
    assert _head(tmp_path) == head
    assert _user_git(tmp_path, "diff", "--cached", "--quiet")[0] == 0  # index untouched
    assert "ro/new.txt" in str(info.value)  # names what may have been written
    _base_is_quiet(tmp_path)


def test_ignored_file_with_carriage_return_in_its_name_is_never_overwritten(
    monkeypatch, tmp_path
):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / ".gitignore").write_text("Icon?\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t")
    (wt / "Icon\r").write_text("task icon\n")
    assert _user_git(wt, "add", "-f", "Icon\r")[0] == 0
    (tmp_path / "Icon\r").write_text("USER IGNORED DATA\n")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match="Icon"):
        mgr.integrate("t")
    _no_porcelain_merge(calls)
    assert (tmp_path / "Icon\r").read_text() == "USER IGNORED DATA\n"
    assert _head(tmp_path) == head


def test_unedited_flagged_file_with_carriage_return_in_its_name_is_not_refused(tmp_path):
    mgr, b = _flagged_base(tmp_path, {"cr\r": "same\n"})
    (b / "task.txt").write_text("task\n")
    _flag(tmp_path, "assume-unchanged", "cr\r")
    assert mgr.integrate("b") is True
    assert (tmp_path / "task.txt").read_text() == "task\n"
    assert (tmp_path / "cr\r").read_text() == "same\n"


def _commit_concurrently_at(monkeypatch, tmp_path, command):
    """Another actor commits `theirs.txt` just before Foundry runs git `command`."""
    spy = isolation_mod._git

    def racing_git(raw, cwd):
        if _cmd(raw)[:1] == [command] and not (tmp_path / "theirs.txt").exists():
            (tmp_path / "theirs.txt").write_text("theirs\n")
            assert _user_git(tmp_path, "add", "theirs.txt")[0] == 0
            assert _user_git(tmp_path, "commit", "-qm", "theirs")[0] == 0
        return spy(raw, cwd)

    monkeypatch.setattr(isolation_mod, "_git", racing_git)


def test_base_moving_during_the_checks_is_refused_untouched(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    calls = _forbid_destruction(monkeypatch)
    _commit_concurrently_at(monkeypatch, tmp_path, "merge-tree")
    with pytest.raises(isolation_mod.IntegrationError, match="moved") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    _no_porcelain_merge(calls)
    assert _user_git(tmp_path, "log", "-1", "--format=%s")[1] == "theirs"
    assert not (tmp_path / "task.txt").exists()
    assert _user_git(tmp_path, "diff", "--cached", "--quiet")[0] == 0
    _base_is_quiet(tmp_path)


def test_concurrent_commit_after_the_checks_is_never_overwritten(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    calls = _forbid_destruction(monkeypatch)
    _commit_concurrently_at(monkeypatch, tmp_path, "read-tree")
    with pytest.raises(isolation_mod.IntegrationError, match="not committed") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is True
    _no_porcelain_merge(calls)
    # update-ref's old-value check kept the other actor's commit as HEAD.
    assert _user_git(tmp_path, "log", "-1", "--format=%s")[1] == "theirs"
    assert (tmp_path / "theirs.txt").read_text() == "theirs\n"
    assert (tmp_path / "task.txt").read_text() == "task\n"  # applied, reported as such


def test_failed_ref_update_reports_applied_but_uncommitted_changes(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    (tmp_path / "user.txt").write_text("user unstaged\n")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    spy = isolation_mod._git

    def failing_update_ref(raw, cwd):
        if _cmd(raw)[:1] == ["update-ref"]:
            return 1, "fatal: cannot lock ref 'HEAD': injected"
        return spy(raw, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_update_ref)
    with pytest.raises(isolation_mod.IntegrationError, match="not committed") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is True
    _no_porcelain_merge(calls)
    assert _head(tmp_path) == head
    assert (tmp_path / "task.txt").read_text() == "task\n"  # applied, reported as such
    assert (tmp_path / "user.txt").read_text() == "user unstaged\n"


def test_successful_integration_uses_plumbing_and_runs_post_merge(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    (tmp_path / "user.txt").write_text("user unstaged\n")
    _hook(tmp_path, "post-merge", "echo ran > .git/post-merge-ran")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    assert mgr.integrate("b") is True
    _no_porcelain_merge(calls)
    parents = _user_git(tmp_path, "rev-list", "--parents", "-n", "1", "HEAD")[1].split()
    assert parents[1] == head and len(parents) == 3
    assert (tmp_path / "user.txt").read_text() == "user unstaged\n"
    assert (tmp_path / ".git" / "post-merge-ran").exists()
    assert _user_git(tmp_path, "diff", "--cached", "--quiet")[0] == 0
    _base_is_quiet(tmp_path)


# --- Review 7: `read-tree -u` removes a directory where the result puts a file, and
# nothing after `update-ref` may escape as a non-IntegrationError.


def _base_with_checked_out_gitlink(tmp_path):
    """A base tracking `sub` as a gitlink whose nested repo is checked out on disk,
    with an unpushed commit and an untracked file of the user's inside it."""
    (tmp_path / "a.txt").write_text("a\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    assert _user_git(sub, "init", "-q")[0] == 0
    (sub / "s.txt").write_text("s\n")
    assert _user_git(sub, "add", "s.txt")[0] == 0
    assert _user_git(sub, "commit", "-qm", "s")[0] == 0
    oid = _user_git(sub, "rev-parse", "HEAD")[1]
    assert _user_git(tmp_path, "init", "-q")[0] == 0
    assert _user_git(tmp_path, "add", "a.txt")[0] == 0
    assert _user_git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub")[0] == 0
    assert _user_git(tmp_path, "commit", "-qm", "base with gitlink")[0] == 0
    # Unpushed work on another branch and an untracked file: the gitlink itself still
    # matches, so `git diff` reports nothing for `sub`.
    assert _user_git(sub, "checkout", "-q", "-b", "wip")[0] == 0
    (sub / "wip.txt").write_text("unpushed\n")
    assert _user_git(sub, "add", "wip.txt")[0] == 0
    assert _user_git(sub, "commit", "-qm", "unpushed")[0] == 0
    assert _user_git(sub, "checkout", "-q", "-")[0] == 0
    (sub / "notes.txt").write_text("USER NOTES\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    return mgr, mgr.create("b"), sub


@pytest.mark.parametrize("change", ["file", "delete"])
def test_task_replacing_a_checked_out_gitlink_is_refused_and_keeps_the_repo(
    monkeypatch, tmp_path, change
):
    mgr, b, sub = _base_with_checked_out_gitlink(tmp_path)
    (b / "sub").rmdir()  # uninitialised in the task's worktree: an empty directory
    if change == "file":
        (b / "sub").write_text("task file\n")
    head = _head(tmp_path)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match="sub") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert not any(c[:1] in (["merge"], ["read-tree"]) for c in calls)
    assert (sub / ".git").is_dir()
    assert (sub / "notes.txt").read_text() == "USER NOTES\n"
    assert _user_git(sub, "log", "-1", "--format=%s", "wip")[1] == "unpushed"
    assert _head(tmp_path) == head


def test_post_merge_hook_output_cannot_break_a_landed_merge(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    _hook(tmp_path, "post-merge", r"printf '\377done\n'; printf '\376' >&2")
    assert mgr.integrate("b") is True
    assert (tmp_path / "task.txt").read_text() == "task\n"


def test_any_error_after_head_moved_is_reported_as_unverified(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")

    def broken_verify(*args, **kwargs):
        raise ValueError("injected failure after the merge landed")

    monkeypatch.setattr(WorktreeManager, "_verify_merged", broken_verify)
    with pytest.raises(isolation_mod.MergeUnverifiedError, match=r"injected failure"):
        mgr.integrate("b")
    assert _user_git(tmp_path, "log", "-1", "--format=%s")[1].startswith("foundry: merge task b")


def test_engine_never_reruns_a_task_whose_integration_raised_unexpectedly(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)

    def unexpected(self, task_id):
        raise ValueError("injected unexpected integration error")

    monkeypatch.setattr(WorktreeManager, "integrate", unexpected)
    events: list[tuple[str, str]] = []
    with pytest.raises(ValueError, match="injected unexpected"):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    saved = BuildState.model_validate_json(session.read_text())
    statuses = {t.id: t.status for t in saved.plan.tasks}
    assert "unverified" in statuses.values()  # the task that raised: never re-run
    assert "done" not in statuses.values()
    assert any(k == "integrate-unverified" and "injected" in m for k, m in events)

    resumed_worker = _WritingWorker()
    monkeypatch.setattr(WorktreeManager, "integrate", lambda self, task_id: True)
    _build(tmp_path, session, fake_provider, resumed_worker, events)
    unverified = [t.title for t in saved.plan.tasks if statuses[t.id] == "unverified"]
    assert not any(title in call for title in unverified for call in resumed_worker.calls)


def test_base_staging_changes_during_the_checks_is_refused_untouched(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")

    def stage_unrelated():
        (tmp_path / "staged.txt").write_text("user staged\n")
        assert _user_git(tmp_path, "add", "staged.txt")[0] == 0

    _race_after_merge_tree(monkeypatch, tmp_path, stage_unrelated)
    calls = _forbid_destruction(monkeypatch)
    with pytest.raises(isolation_mod.IntegrationError, match="staged changes") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert not any(c[:1] == ["read-tree"] for c in calls)
    assert not (tmp_path / "task.txt").exists()


def test_ignored_file_appearing_during_the_checks_is_never_overwritten(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path, gitignore="*.log\n")
    (b / "build.log").write_text("task log\n")
    assert _user_git(b, "add", "-f", "build.log")[0] == 0
    git = isolation_mod._git

    def ignored_file_appears_after_the_first_check(args, cwd):
        if _cmd(args)[-1:] == ["merge.verifySignatures"]:  # read right after that check
            (tmp_path / "build.log").write_text("USER LOG\n")
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", ignored_file_appears_after_the_first_check)
    with pytest.raises(isolation_mod.IntegrationError, match=r"build\.log") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert (tmp_path / "build.log").read_text() == "USER LOG\n"


# --- Review 8: never recurse into submodules; a merge that landed is recorded before
# anything (hook, verification, Ctrl-C) can interrupt.


def test_gitlink_bump_never_touches_a_checked_out_submodule(tmp_path):
    up = tmp_path / "upstream"
    up.mkdir()
    assert _user_git(up, "init", "-q")[0] == 0
    (up / ".gitignore").write_text("*.log\n")
    (up / "s.txt").write_text("v1\n")
    assert _user_git(up, "add", "-A")[0] == 0
    assert _user_git(up, "commit", "-qm", "v1")[0] == 0
    v1 = _head(up)
    (up / "s.txt").write_text("v2\n")
    (up / "debug.log").write_text("upstream\n")
    assert _user_git(up, "add", "-f", "s.txt", "debug.log")[0] == 0
    assert _user_git(up, "commit", "-qm", "v2")[0] == 0
    v2 = _head(up)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a\n")
    mgr = WorktreeManager(ws)
    assert mgr.setup()
    assert _user_git(ws, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                     str(up), "sub")[0] == 0
    assert _user_git(ws / "sub", "checkout", "-q", v1)[0] == 0
    assert _user_git(ws, "add", "-A")[0] == 0
    assert _user_git(ws, "commit", "-qm", "sub")[0] == 0
    assert _user_git(ws, "config", "submodule.recurse", "true")[0] == 0
    (ws / "sub" / "debug.log").write_text("USER ignored data\n")
    b = mgr.create("t")
    assert _user_git(b, "update-index", "--cacheinfo", f"160000,{v2},sub")[0] == 0
    (b / "x.txt").write_text("x\n")
    assert mgr.integrate("t") is True
    assert (ws / "sub" / "debug.log").read_text() == "USER ignored data\n"
    assert _head(ws / "sub") == v1  # the submodule checkout is never moved
    assert (ws / "sub" / "s.txt").read_text() == "v1\n"
    assert (ws / "x.txt").read_text() == "x\n"


def test_interrupt_after_the_merge_landed_is_recorded_and_never_rerun(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)

    def ctrl_c(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(WorktreeManager, "_run_post_merge_hook", ctrl_c)
    events: list[tuple[str, str]] = []
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    saved = BuildState.model_validate_json(session.read_text())
    statuses = {t.id: t.status for t in saved.plan.tasks}
    merged = [t for t in ("t1", "t2")
              if f"foundry: merge task {t} " in _user_git(tmp_path, "log", "--format=%s")[1]]
    assert len(merged) == 1 and statuses[merged[0]] == "unverified"

    monkeypatch.undo()
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    resumed = _WritingWorker()
    _build(tmp_path, session, fake_provider, resumed, events)
    title = {"t1": "one", "t2": "two"}[merged[0]]
    assert not any(call.endswith(title) for call in resumed.calls)
    log = _user_git(tmp_path, "log", "--format=%s")[1]
    assert log.count(f"foundry: merge task {merged[0]} ") == 1


def test_interrupt_before_writing_leaves_the_task_retryable(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")

    def ctrl_c(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(WorktreeManager, "_require_result_fits_base", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        mgr.integrate("b")
    assert "b" not in mgr.written


@pytest.mark.parametrize("command", ["read-tree", "commit-tree", "update-ref"])
def test_git_timeout_while_applying_is_reported_honestly(monkeypatch, tmp_path, command):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    git = isolation_mod._git

    def slow(args, cwd):
        cmd = _cmd(args)
        # commit-tree: the merge commit (two parents), not the task's own commit
        if cmd[:1] == [command] and (command != "commit-tree" or cmd.count("-p") == 2):
            raise subprocess.TimeoutExpired(["git", command], 120)
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", slow)
    expected = isolation_mod.MergeUnverifiedError if command == "update-ref" else (
        isolation_mod.IntegrationError)
    with pytest.raises(expected, match=r"timed out") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is True
    assert "task.txt" in str(info.value)


def test_git_timeout_before_writing_is_a_refusal(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    git = isolation_mod._git

    def slow(args, cwd):
        if _cmd(args)[:1] == ["merge-tree"]:
            raise subprocess.TimeoutExpired(["git", "merge-tree"], 120)
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", slow)
    with pytest.raises(isolation_mod.IntegrationError, match=r"timed out") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert not (tmp_path / "task.txt").exists()


# --- Review 9: the engine saves a task's outcome before reporting anything, so an
# interrupt or a failing event sink can never leave a merged task PENDING.


def _interrupting_build(monkeypatch, fake_provider, tmp_path, on_kind):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    events: list[tuple[str, str]] = []

    def on_event(kind, msg):
        events.append((kind, msg))
        if kind == on_kind:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        engine.build(
            "x", workspace=str(tmp_path), provider=fake_provider, worker=_WritingWorker(),
            session_path=str(session), isolation="worktree", discovery=False,
            use_archetypes=False, verify=False, review=False, on_event=on_event,
        )
    saved = BuildState.model_validate_json(session.read_text())
    return session, {t.id: t.status for t in saved.plan.tasks}


def _merged_tasks(tmp_path):
    log = _user_git(tmp_path, "log", "--format=%s")[1]
    return [t for t in ("t1", "t2") if f"foundry: merge task {t} " in log]


@pytest.mark.parametrize("on_kind", ["worktree-retained", "task-done"])
def test_interrupt_while_reporting_a_merged_task_keeps_it_done(
    monkeypatch, fake_provider, tmp_path, on_kind,
):
    session, statuses = _interrupting_build(monkeypatch, fake_provider, tmp_path, on_kind)
    merged = _merged_tasks(tmp_path)
    assert len(merged) == 1 and statuses[merged[0]] == "done"
    resumed = _WritingWorker()
    monkeypatch.undo()
    _build(tmp_path, session, fake_provider, resumed, [])
    title = {"t1": "one", "t2": "two"}[merged[0]]
    assert not any(call.endswith(title) for call in resumed.calls)
    assert _user_git(tmp_path, "log", "--format=%s")[1].count(
        f"foundry: merge task {merged[0]} ") == 1


def test_interrupt_while_reporting_an_unverified_merge_keeps_it_unverified(
    monkeypatch, fake_provider, tmp_path,
):
    def unverified(self, rec, head, oid, result):
        raise isolation_mod.MergeUnverifiedError("injected: differs from the checked merge")

    monkeypatch.setattr(WorktreeManager, "_verify_merged", unverified)
    _, statuses = _interrupting_build(
        monkeypatch, fake_provider, tmp_path, "integrate-unverified")
    merged = _merged_tasks(tmp_path)
    assert len(merged) == 1 and statuses[merged[0]] == "unverified"


def test_failure_after_writing_the_base_is_never_retried(monkeypatch, fake_provider, tmp_path):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    git = isolation_mod._git

    def failing_update_ref(args, cwd):
        if _cmd(args)[:1] == ["update-ref"]:
            return 1, "fatal: injected ref failure"
        return git(args, cwd)

    monkeypatch.setattr(isolation_mod, "_git", failing_update_ref)
    _build(tmp_path, session, fake_provider, _WritingWorker(), [])
    saved = BuildState.model_validate_json(session.read_text())
    # The first task's changes were applied (uncommitted): a manual `git commit` must
    # not be followed by an automatic re-run that merges the task again.
    assert "unverified" in {t.status for t in saved.plan.tasks}


def test_inherited_git_location_variables_are_ignored(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    other = tmp_path.parent / f"{tmp_path.name}-other.index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(other))
    monkeypatch.setenv("GIT_DIR", str(tmp_path.parent / "no-such-repo.git"))
    assert mgr.integrate("b") is True
    assert (tmp_path / "task.txt").read_text() == "task\n"
    assert not other.exists()


# --- Review 10: a task is recorded as written before Foundry's first write to the base,
# so an interrupt while git itself runs can never leave it retryable.


def _interrupt_right_after(monkeypatch, command):
    git = isolation_mod._git

    def interrupted(args, cwd):
        result = git(args, cwd)
        if _cmd(args)[:1] == [command]:  # git finished; Ctrl-C reaches Python now
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(isolation_mod, "_git", interrupted)


@pytest.mark.parametrize("command", ["read-tree", "commit-tree", "update-ref"])
def test_interrupt_while_git_writes_the_base_is_saved_unverified(
    monkeypatch, fake_provider, tmp_path, command,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    if command == "commit-tree":  # the merge commit, not the task's own commit
        git = isolation_mod._git

        def interrupted(args, cwd):
            result = git(args, cwd)
            if _cmd(args)[:1] == ["commit-tree"] and _cmd(args).count("-p") == 2:
                raise KeyboardInterrupt
            return result

        monkeypatch.setattr(isolation_mod, "_git", interrupted)
    else:
        _interrupt_right_after(monkeypatch, command)
    events: list[tuple[str, str]] = []
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    saved = BuildState.model_validate_json(session.read_text())
    statuses = {t.id: t.status for t in saved.plan.tasks}
    assert sorted(statuses.values()) == ["pending", "unverified"]
    first = next(t for t, s in statuses.items() if s == "unverified")
    note = [m for k, m in events if k == "integrate-unverified"]
    written = {"t1": "one.txt", "t2": "two.txt"}[first]
    assert len(note) == 1 and written in note[0]  # names what may be in the base

    monkeypatch.undo()
    resumed = _WritingWorker()
    _build(tmp_path, session, fake_provider, resumed, [])
    title = {"t1": "one", "t2": "two"}[first]
    assert not any(call.endswith(title) for call in resumed.calls)
    log = _user_git(tmp_path, "log", "--format=%s")[1]
    assert log.count(f"foundry: merge task {first} ") <= 1


def test_failing_save_after_a_merge_says_what_to_fix(monkeypatch, fake_provider, tmp_path):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    real_save = engine.session_mod.save

    def disk_full(path, state):
        if any(t.status in ("done", "unverified") for t in state.plan.tasks):
            raise OSError(28, "No space left on device")
        return real_save(path, state)

    monkeypatch.setattr(engine.session_mod, "save", disk_full)
    events: list[tuple[str, str]] = []
    with pytest.raises(OSError, match="No space left"):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    notes = [m for k, m in events if k == "session-not-saved"]
    assert len(notes) == 1 and "done_ids" in notes[0] and str(session) in notes[0]


def test_second_interrupt_during_the_abort_save_still_reports(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    _interrupt_right_after(monkeypatch, "update-ref")
    real_save = engine.session_mod.save

    def interrupted_save(path, state):
        if any(t.status == "unverified" for t in state.plan.tasks):
            raise KeyboardInterrupt  # the user presses Ctrl-C again while saving
        return real_save(path, state)

    monkeypatch.setattr(engine.session_mod, "save", interrupted_save)
    events: list[tuple[str, str]] = []
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    kinds = [k for k, _ in events]
    assert "integrate-unverified" in kinds and "session-not-saved" in kinds


def test_unexpected_error_before_writing_says_nothing_was_written(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)

    def broken(*args, **kwargs):
        raise ValueError("injected before any write")

    monkeypatch.setattr(WorktreeManager, "_require_result_fits_base", broken)
    events: list[tuple[str, str]] = []
    with pytest.raises(ValueError):
        _build(tmp_path, session, fake_provider, _WritingWorker(), events)
    notes = [m for k, m in events if k == "integrate-unverified"]
    assert len(notes) == 1 and "nothing was merged" in notes[0]
    assert _user_git(tmp_path, "log", "--format=%s")[1] == "foundry: initialize workspace"


# --- The same-UID writer in the instant before `read-tree`. The window is
# narrowed to the lstat snapshot taken after the last check and compared right before
# the write; what is left (a write between that compare and git's own open) is the
# hostile same-user class the README documents as unsupported by design.


def _write_after_the_reference_snapshot(monkeypatch, tmp_path, rel, text):
    real = isolation_mod._fs_snapshot
    writes: list[str] = []

    def spy(ws, paths):
        snapshot = real(ws, paths)
        if not writes:  # the first snapshot is the reference; the write lands after it
            (tmp_path / rel).write_text(text)
            writes.append(rel)
        return snapshot

    monkeypatch.setattr(isolation_mod, "_fs_snapshot", spy)
    return writes


def test_ignored_file_appearing_in_the_instant_before_read_tree_is_never_overwritten(
    monkeypatch, tmp_path,
):
    mgr, b = _plain_pair(tmp_path, gitignore="*.log\n")
    (b / "build.log").write_text("task log\n")
    assert _user_git(b, "add", "-f", "build.log")[0] == 0
    head = _head(tmp_path)
    writes = _write_after_the_reference_snapshot(monkeypatch, tmp_path, "build.log", "USER LOG\n")
    with pytest.raises(isolation_mod.IntegrationError, match=r"(?s)changed.*build\.log") as info:
        mgr.integrate("b")
    assert writes == ["build.log"] and info.value.merge_ran is False
    assert (tmp_path / "build.log").read_text() == "USER LOG\n"
    assert _head(tmp_path) == head
    _base_is_quiet(tmp_path)
    assert _user_git(tmp_path, "diff", "--cached", "--name-only")[1] == ""  # index untouched
    assert "b" not in mgr.written  # nothing was written: the task may be run again


def test_tracked_file_edited_in_the_instant_before_read_tree_is_never_overwritten(
    monkeypatch, tmp_path,
):
    mgr, b = _plain_pair(tmp_path)
    (b / "shared.txt").write_text("task edit\n")
    head = _head(tmp_path)
    _write_after_the_reference_snapshot(monkeypatch, tmp_path, "shared.txt", "user edit\n")
    with pytest.raises(isolation_mod.IntegrationError, match=r"(?s)changed.*shared\.txt") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert (tmp_path / "shared.txt").read_text() == "user edit\n"
    assert _head(tmp_path) == head


def test_snapshot_compare_lets_an_unchanged_base_merge(monkeypatch, tmp_path):
    mgr, b = _plain_pair(tmp_path)
    (b / "task.txt").write_text("task\n")
    calls: list[list[str]] = []
    real = isolation_mod._fs_snapshot
    monkeypatch.setattr(
        isolation_mod, "_fs_snapshot", lambda ws, paths: calls.append(list(paths)) or real(ws, paths)
    )
    assert mgr.integrate("b") is True
    assert calls == [["task.txt"], ["task.txt"]]  # reference after the last check, compare before the write
    assert (tmp_path / "task.txt").read_text() == "task\n"


# --- A disk failure while saving the session after a merge landed. The merge
# commit itself is the durable record: a resume finds it and never re-runs or
# re-merges the task, even though the session on disk still says "pending".


class _ChangingWriter:
    """Writes different content on every run, so a re-run would produce a second merge."""

    name = "changing"
    runs = 0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def execute(self, system, instruction, workspace, seat="generalist") -> str:
        _ChangingWriter.runs += 1
        self.calls.append(instruction)
        (Path(workspace) / f"{instruction.split()[-1]}.txt").write_text(f"run {_ChangingWriter.runs}")
        return "ok"


def _session_save_fails_once_a_task_is_merged(monkeypatch):
    real_save = engine.session_mod.save

    def disk_full(path, state):
        if any(t.status in ("done", "unverified") for t in state.plan.tasks):
            raise OSError(28, "No space left on device")
        return real_save(path, state)

    monkeypatch.setattr(engine.session_mod, "save", disk_full)


def test_disk_failure_during_the_session_save_is_recoverable_on_resume(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    _session_save_fails_once_a_task_is_merged(monkeypatch)
    events: list[tuple[str, str]] = []
    with pytest.raises(OSError, match="No space left"):  # the CLI turns this into exit 1
        _build(tmp_path, session, fake_provider, _ChangingWriter(), events)
    notes = [m for k, m in events if k == "session-not-saved"]
    assert len(notes) == 1 and str(session) in notes[0]
    # The build stopped at the failure: exactly one merge reached the base, and the
    # session on disk (last successful save) still has that task pending.
    merged = _merged_tasks(tmp_path)
    assert len(merged) == 1
    first = merged[0]
    saved = BuildState.model_validate_json(session.read_text())
    assert {t.id: t.status for t in saved.plan.tasks}[first] == "pending"
    head_after_failure = _head(tmp_path)

    monkeypatch.undo()
    resumed = _ChangingWriter()
    resumed_events: list[tuple[str, str]] = []
    _build(tmp_path, session, fake_provider, resumed, resumed_events)
    title = {"t1": "one", "t2": "two"}[first]
    assert not any(call.endswith(title) for call in resumed.calls)  # never run again
    log = _user_git(tmp_path, "log", "--format=%s")[1]
    assert log.count(f"foundry: merge task {first} ") == 1  # never merged again
    held = [m for k, m in resumed_events if k == "task-unverified" and title in m]
    assert held and any(head_after_failure in m for m in held)  # names the merge commit
    # The finding is persisted: the next resume sees the task as unverified outright.
    saved = BuildState.model_validate_json(session.read_text())
    assert {t.id: t.status for t in saved.plan.tasks}[first] == "unverified"


def test_merge_record_of_another_build_in_the_same_workspace_is_not_claimed(
    monkeypatch, fake_provider, tmp_path,
):
    # An older build in this workspace merged its own "t1"; this build's "t1" is new.
    old = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    _build(tmp_path, old, fake_provider, _ChangingWriter(), [])
    assert _merged_tasks(tmp_path) == ["t1", "t2"]
    fresh = tmp_path / "fresh.json"
    plan = Plan(app_name="b", stack="s", summary="", tasks=[
        Task(id="t1", title="one again", role="backend", instruction="three"),
    ])
    save(fresh, BuildState(brief="y", workspace=str(tmp_path), plan=plan))
    worker = _ChangingWriter()
    state = _build(tmp_path, fresh, fake_provider, worker, [])
    assert len(worker.calls) == 1 and worker.calls[0].endswith("Your task: three")
    assert state.done_ids == ["t1"]


# --- The pre-write snapshot must not refuse legitimate tasks, and
# the resume check finds this build's merges on any branch.


def test_task_turning_a_file_into_a_same_name_directory_merges(tmp_path):
    (tmp_path / "config").write_text("old\n")
    mgr = WorktreeManager(tmp_path)
    assert mgr.setup()
    wt = mgr.create("t1")
    (wt / "config").unlink()
    (wt / "config").mkdir()
    (wt / "config" / "x.json").write_text("{}\n")
    assert mgr.integrate("t1") is True
    assert (tmp_path / "config" / "x.json").read_text() == "{}\n"


def test_in_place_same_size_rewrite_before_the_write_is_refused(monkeypatch, tmp_path):
    # The user rewrites a file the task changes in place (same inode, same size) after
    # the checks: only the timestamps tell the snapshots apart, and the guard must
    # refuse before git writes anything (merge_ran False).
    mgr, b = _plain_pair(tmp_path)
    (b / "shared.txt").write_text("task\n")
    target = tmp_path / "shared.txt"
    real_snapshot = isolation_mod._fs_snapshot
    calls = {"n": 0}

    def snapshot_then_user_rewrites(ws, paths):
        out = real_snapshot(ws, paths)
        calls["n"] += 1
        if calls["n"] == 1:  # after the reference snapshot, before the final compare
            time.sleep(0.02)
            with open(target, "r+b") as fh:
                fh.write(b"USER")  # "base\n" -> "USER\n": same size, same inode
        return out

    monkeypatch.setattr(isolation_mod, "_fs_snapshot", snapshot_then_user_rewrites)
    with pytest.raises(isolation_mod.IntegrationError, match="changed on disk") as info:
        mgr.integrate("b")
    assert info.value.merge_ran is False
    assert target.read_text() == "USER\n"


def test_resume_finds_this_builds_merge_on_a_branch_switched_away_from(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    _session_save_fails_once_a_task_is_merged(monkeypatch)
    with pytest.raises(OSError):
        _build(tmp_path, session, fake_provider, _ChangingWriter(), [])
    first = _merged_tasks(tmp_path)[0]
    # The user moves the checkout to a new branch from before the merge.
    assert _user_git(tmp_path, "checkout", "-q", "-b", "elsewhere", "HEAD~1")[0] == 0
    monkeypatch.undo()
    resumed = _ChangingWriter()
    _build(tmp_path, session, fake_provider, resumed, [])
    title = {"t1": "one", "t2": "two"}[first]
    assert not any(call.endswith(title) for call in resumed.calls)


def test_unreadable_history_stops_a_resume_with_a_clear_error(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)

    def broken(self, build_id):
        raise RuntimeError("git log failed: injected")

    monkeypatch.setattr(WorktreeManager, "merged_tasks", broken)
    events: list[tuple[str, str]] = []
    with pytest.raises(isolation_mod.HistoryUnreadableError, match="cannot read the base history"):
        _build(tmp_path, session, fake_provider, _ChangingWriter(), events)


def test_a_broken_ref_elsewhere_does_not_hide_or_block_this_builds_merges(
    monkeypatch, fake_provider, tmp_path,
):
    session = _two_task_session(tmp_path)
    monkeypatch.setattr(engine.constants, "MAX_PARALLEL", 1)
    _session_save_fails_once_a_task_is_merged(monkeypatch)
    with pytest.raises(OSError):
        _build(tmp_path, session, fake_provider, _ChangingWriter(), [])
    first = _merged_tasks(tmp_path)[0]
    # A stale ref pointing at a missing object (e.g. after a manual cleanup).
    (tmp_path / ".git" / "refs" / "heads" / "broken").write_text("1" * 40 + "\n")
    monkeypatch.undo()
    resumed = _ChangingWriter()
    events: list[tuple[str, str]] = []
    _build(tmp_path, session, fake_provider, resumed, events)
    title = {"t1": "one", "t2": "two"}[first]
    assert not any(call.endswith(title) for call in resumed.calls)
    assert any(k == "worktree-warning" and "refs/heads/broken" in m for k, m in events)


def test_unreadable_history_is_a_clean_cli_refusal(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from foundry import cli

    def unreadable(*args, **kwargs):
        raise isolation_mod.HistoryUnreadableError("cannot read the base history: injected")

    monkeypatch.setattr(cli, "build", unreadable)
    result = CliRunner().invoke(
        cli.app, ["build", "a brief", "-w", str(tmp_path / "ws"), "--provider", "cli"]
    )
    assert result.exit_code == 2, result.output
    assert "cannot read the base history" in result.output
    assert "Traceback" not in result.output
