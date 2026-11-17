"""Concurrency isolation via git worktrees.

Parallel workers in a wave would otherwise share one workspace dir and clobber
each other's files. Instead, each task runs in its own **git worktree** (a branch
off the integration branch); on success its changes are committed and merged back.
Merges are serialized (the engine integrates in its single result-collecting
thread), so the integration tree is never written concurrently. Each merge is first
computed with `git merge-tree` under pinned config; a conflict fails that task
loudly without touching the base checkout. A clean result is applied with plumbing
(`read-tree -m -u`, `commit-tree`, `update-ref`), never porcelain `git merge`.
A base repo that is busy (another
merge/rebase in progress, staged changes), unstaged edits or untracked/ignored base
files where the merge result writes (git would overwrite ignored ones), a worktree that is no
longer verifiably Foundry's or is itself mid-merge, and nested repos (gitlinks) are
refused (`IntegrationError`) without touching either. Foundry's `.foundry/` scratch
never enters the task commit.

Foundry never deletes worktrees or branches. Automatic deletion cannot be made
safe against a separate Git actor reclaiming the same names, so each task gets
unpredictable private names and every leftover (completed, rolled-back, failed,
or legacy `.foundry/wt/<task>` / `foundry/<task>`) is *reported* for manual cleanup.

This is *concurrency* isolation; it pairs with the native sandbox (security
isolation) — see docs/ROADMAP.md. If git is unavailable, worktree mode uses a
serial shared workspace; explicit `FOUNDRY_ISOLATION=none` opts out of worktrees.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

from foundry import procenv

# Foundry supplies its own commit identity so it works on any build machine,
# regardless of whether git user.name/email is configured.
_IDENT = ["-c", "user.email=foundry@foundry.invalid", "-c", "user.name=Foundry"]


# Repository-location variables inherited from whatever launched Foundry (e.g. a git
# hook) would point every command at another repo or index; `cwd` decides instead.
# The set lives in `procenv` so workers, gates and git commands cannot drift apart.
_GIT_LOCATION_VARS = procenv._GIT_LOCATION_VARS


def _git_env() -> dict[str, str]:
    return procenv.clean_env()


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *_IDENT, *args], cwd=str(cwd), capture_output=True, timeout=120, env=_git_env()
    )
    # Messages only (never parsed as paths): undecodable bytes must not raise.
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return proc.returncode, output.strip()


def _git_paths(args: list[str], cwd: Path, stdin: str | None = None) -> list[str]:
    """Run a git command with `-z` input/output and return the NUL-separated fields
    verbatim (no stripping or quoting; undecodable bytes kept as surrogates)."""
    # Bytes, not text mode: text mode's universal newlines would turn a '\r' in a
    # path into '\n' (e.g. macOS's `Icon\r`), so checks would look at the wrong path.
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, timeout=120, env=_git_env(),
        input=None if stdin is None else stdin.encode("utf-8", "surrogateescape"),
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {err}")
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _brief(output: str, limit: int = 600) -> str:
    """Git's output on one line, truncated, for events and error messages."""
    text = " | ".join(line.strip() for line in output.splitlines() if line.strip())
    return text if len(text) <= limit else text[:limit] + " ..."


_Snapshot = dict[str, tuple[int, int, int, int, int, int] | None]


def _fs_snapshot(ws: Path, paths: list[str]) -> _Snapshot:
    """Identity of each path on disk (type/mode, inode, device, size, mtime, ctime),
    or None where nothing exists: cheap to take and to compare, and any write,
    replacement, creation or removal of the path changes it."""
    out: _Snapshot = {}
    for rel in paths:
        try:
            st = os.lstat(ws.joinpath(*rel.split("/")))
        except (FileNotFoundError, NotADirectoryError):
            # Nothing there yet (a parent may still be a file the task turns into
            # a directory of the same name).
            out[rel] = None
        else:
            out[rel] = (st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns,
                        st.st_ctime_ns)
    return out


def _shown(paths: list[str], limit: int = 10) -> str:
    return ", ".join(paths[:limit]) + (" ..." if len(paths) > limit else "")


def _is_oid(text: str) -> bool:
    return len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text)


def _blob_oid(data: bytes, oid_len: int) -> str:
    """Git's blob object ID for `data` (SHA-1, or SHA-256 for 64-hex-digit repos)."""
    digest = hashlib.sha256 if oid_len == 64 else hashlib.sha1
    return digest(b"blob %d\0" % len(data) + data).hexdigest()


_NO_MODE = "000000"  # diff-tree's mode for "no entry on this side"
_GITLINK = "160000"

# Config pinned (with `-c`) on `git merge-tree`, so the result Foundry checks and then
# applies is independent of user config:
# - merge.directoryRenames=false: a task's new file lands exactly where the worker
#   wrote it, never relocated into a directory the base renamed (where a user file
#   may live); Git's default ("conflict") would only report the relocation.
# - merge.renames=true / merge.renormalize=false: Git's defaults.
# - merge.autoStash=false: kept as a guard; Foundry never runs porcelain `git merge`.
_MERGE_CONFIG = (
    "merge.directoryRenames=false",
    "merge.renames=true",
    "merge.renormalize=false",
    "merge.autoStash=false",
)


class IntegrationError(RuntimeError):
    """integrate() did not (verifiably) integrate the task.

    `merge_ran` is False when Foundry refused before writing to the base: its refs,
    index and working tree were not written (only unreferenced objects, e.g. from
    `git merge-tree`) and the task is not merged. When True, Foundry started applying
    the checked result and the message says exactly what was written (at most the
    task's own paths, none of which had local edits). Foundry never resets, stashes
    or aborts anything in the base."""

    def __init__(self, message: str, *, merge_ran: bool = False) -> None:
        super().__init__(message)
        self.merge_ran = merge_ran


class HistoryUnreadableError(RuntimeError):
    """The base history cannot be read, so a resume cannot tell which tasks this
    build already merged: it is refused rather than risk merging a task twice."""


class MergeUnverifiedError(IntegrationError):
    """The merge commit landed on the base, but HEAD's parents or tree differ from
    the checked merge (e.g. a concurrent commit). Left in place for manual
    inspection: the task must not be re-run or merged again automatically."""

    def __init__(self, message: str) -> None:
        super().__init__(message, merge_ran=True)


def _checked_git(args: list[str], cwd: Path) -> str:
    rc, output = _git(args, cwd)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {output}")
    return output


@dataclass(frozen=True)
class WorktreeRecord:
    """The actual private path and branch Foundry created (or tried to) for a task."""

    task_id: str
    path: Path
    branch: str
    start: str = ""  # commit the branch was created at (the task commit's only parent)
    gitdir: str = ""  # the worktree's private admin dir under <base>/.git/worktrees/
    nonce: str = ""  # secret written into `gitdir`; a reclaimed worktree lacks it

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    def describe(self) -> str:
        return f"task {self.task_id}: path {self.path} ref {self.ref}"


class WorktreeManager:
    """Per-task git worktrees with serialized merge-back into the integration branch.

    Never removes worktrees/branches: `discard()` only moves the record to
    `retained` (for manual review); failed adds are listed in `unowned`."""

    BRANCH_PREFIX = "foundry-wt/"
    OWNER_FILE = "foundry-owner"
    SCRATCH = ".foundry"  # Foundry's own scratch dir at the workspace root

    def __init__(self, workspace: Path) -> None:
        self.ws = workspace
        self.root = workspace / ".foundry" / "worktrees"
        self.legacy_root = workspace / ".foundry" / "wt"
        self.base = "main"
        self.enabled = False
        self._active: dict[str, WorktreeRecord] = {}
        self.retained: list[WorktreeRecord] = []  # created by us; left for manual cleanup
        self.unowned: list[WorktreeRecord] = []  # failed adds: ownership unknown, untouched
        self.last_conflict = ""  # git's output for the latest integrate() that returned False
        # Tasks whose checked result Foundry has started writing to the base (from
        # `read-tree` on), with the paths it writes: the base may hold their changes,
        # staged or committed, so an interrupted or failed run must never re-run them.
        self.written: dict[str, list[str]] = {}
        # Stamped into merge commit messages (see `merged_tasks`); set by the engine.
        self.build_id = ""
        self.unreadable_refs: list[str] = []  # stale refs `merged_tasks` had to skip

    def setup(self) -> bool:
        """Make a git repo with a base commit. Returns False (serial shared-workspace
        fallback) if git is missing or `git init` fails; later failures raise."""
        if shutil.which("git") is None:
            return False
        if not (self.ws / ".git").exists() and _git(["init"], self.ws)[0] != 0:
            return False
        # Keep Foundry's own scratch (worktrees, staged skills) out of the app repo.
        gitignore = self.ws / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".foundry/" not in existing:
            gitignore.write_text((existing.rstrip() + "\n.foundry/\n").lstrip(), encoding="utf-8")
        # A base commit must exist for `worktree add ... HEAD` to work.
        if _git(["rev-parse", "HEAD"], self.ws)[0] != 0:
            _checked_git(["add", "-A"], self.ws)
            _checked_git(["commit", "--no-verify", "-m", "foundry: initialize workspace"], self.ws)
        branch = _checked_git(["branch", "--show-current"], self.ws)
        if branch:  # empty on a detached HEAD (common in CI): merges land on HEAD
            self.base = branch
        self.enabled = True
        return True

    def worktree(self, task_id: str) -> WorktreeRecord | None:
        """The task's current (not yet discarded) worktree record, if any."""
        return self._active.get(task_id)

    def create(self, task_id: str) -> Path:
        """Create a fresh private worktree on a fresh branch off the base HEAD.

        Names are random, not derived from the task ID, and are never adopted: the
        branch must not exist, and the directory is claimed with an exclusive
        mkdir (mode 0700) before `git worktree add -b` (which refuses existing
        branches and non-empty directories). A prior record for the same task is
        retained, not reused or removed."""
        token = secrets.token_hex(16)
        branch = f"{self.BRANCH_PREFIX}{token}"
        wt = self.root / token
        check = ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        rc, output = _git(check, self.ws)
        if rc == 0:
            raise RuntimeError(f"worktree branch already exists: {branch}")
        if rc != 1:
            raise RuntimeError(f"git {' '.join(check)} failed in {self.ws}: {output}")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wt.mkdir(mode=0o700)  # FileExistsError if the path exists: never adopt it
        rec = WorktreeRecord(task_id, wt, branch)
        try:
            wt.chmod(0o700)  # independent of umask
            # A failed add grants no ownership: git or another actor may have
            # created either name. Record it as unowned and touch nothing.
            start = _checked_git(["rev-parse", "--verify", "HEAD^{commit}"], self.ws)
            rec = replace(rec, start=start)
            _checked_git(["worktree", "add", "-b", branch, str(wt), start], self.ws)
            rec = replace(rec, gitdir=self._worktree_gitdir(wt), nonce=secrets.token_hex(16))
            owner = Path(rec.gitdir) / self.OWNER_FILE
            fd = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(rec.nonce)
        except Exception:
            self.unowned.append(rec)
            raise
        previous = self._active.pop(task_id, None)
        if previous is not None:
            self.retained.append(previous)
        self._active[task_id] = rec
        return wt

    def integrate(self, task_id: str) -> bool:
        """Commit the worktree's changes and merge them into the base branch.

        The merge result is computed first with `git merge-tree` (config pinned, see
        `_MERGE_CONFIG`), so a conflict returns False without touching the base
        (git's messages in `last_conflict`). Raises IntegrationError, without merging,
        if the worktree or base is not in the expected state, the result would
        overwrite unstaged edits or untracked/ignored base files, or the base moved
        while checking. The checked result is then applied (`_apply_result`); a
        failure there raises IntegrationError(merge_ran=True) naming what was written.
        A merge whose result differs from the checked one raises MergeUnverifiedError
        and is left in place."""
        self.last_conflict = ""
        rec = self._active.get(task_id)
        if rec is None:
            raise RuntimeError(f"no active worktree for task {task_id}")
        try:
            self._verify_owned(rec)
            # Commit exactly the worktree's files with the recorded start as the only
            # parent, so whatever the branch/HEAD now points at is never merged.
            self._clear_assume_unchanged(rec.path)
            _checked_git(["add", "-A"], rec.path)
            tree = self._keep_scratch_as_at_start(rec.start, _checked_git(["write-tree"], rec.path))
            if tree == _checked_git(["rev-parse", f"{rec.start}^{{tree}}"], self.ws):
                return True  # no changes: nothing to merge
            nested = self._new_gitlinks(rec.start, tree)
            if nested:
                raise IntegrationError(
                    f"{rec.describe()}: not integrated: the task adds nested git "
                    f"repositories ({', '.join(nested[:10])}); `add -A` records them as "
                    "gitlinks without .gitmodules, so their files would never reach the base"
                )
            sign = _git(["config", "--type=bool", "--get", "commit.gpgsign"], rec.path)[1] == "true"
            commit = ["commit-tree", *(["-S"] if sign else []), tree, "-p", rec.start]
            oid = _checked_git([*commit, "-m", f"foundry: task {task_id}"], rec.path)
            self._require_quiet_base()
            head = _checked_git(["rev-parse", "--verify", "HEAD^{commit}"], self.ws)
            pins = self._merge_pins()
            # Compute the merge result first, touching neither the base working tree
            # nor its index: a conflict is reported without starting (or aborting) a
            # merge, and every path the result writes can be checked beforehand.
            rc, output = _git([*pins, "merge-tree", "--write-tree", "--name-only", head, oid],
                              self.ws)
            lines = output.splitlines()
            result = lines[0].strip() if lines else ""
            if rc not in (0, 1) or not _is_oid(result):
                raise IntegrationError(
                    f"{rec.describe()}: not merged: git merge-tree of {oid} failed: "
                    f"{_brief(output)}"
                )
            if rc == 1:
                self.last_conflict = _brief("\n".join(lines[1:]))
                return False
            changed = self._require_result_fits_base(rec, head, result)
            # Plumbing skips the signature check `git merge` makes under this setting.
            verify = _git(["config", "--type=bool", "--get", "merge.verifySignatures"], self.ws)
            if verify[1] == "true":
                rc, output = _git(["verify-commit", oid], self.ws)
                if rc != 0:
                    raise IntegrationError(
                        f"{rec.describe()}: not merged: merge.verifySignatures is set and the "
                        f"task commit {oid} does not have a valid GPG signature "
                        f"({_brief(output) or 'unsigned'}); enable commit.gpgsign or merge it "
                        "manually"
                    )
            # Last check before writing: the base must still be idle and at `head`
            # (another actor may have committed or started something meanwhile).
            self._require_quiet_base()
            now = _checked_git(["rev-parse", "--verify", "HEAD^{commit}"], self.ws)
            if now != head:
                raise IntegrationError(
                    f"{rec.describe()}: not merged: base repo {self.ws} moved from {head} to "
                    f"{now} while the merge was being checked; run the task's integration again"
                )
            # `read-tree -u` overwrites ignored files: re-check right before writing, so
            # only a change in the last moment before the write can slip through.
            self._require_result_fits_base(rec, head, result)
            # Reference for the last guard: the on-disk identity of every path the
            # result writes, compared again immediately before `read-tree`.
            snapshot = _fs_snapshot(self.ws, changed)
        except IntegrationError:
            raise
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            # Before `_apply_result` nothing in the base has been written.
            raise IntegrationError(f"{rec.describe()}: not integrated: {exc}") from exc
        self._apply_result(rec, task_id, head, oid, result, changed, snapshot)
        # HEAD now holds the merge: any failure from here on is "merged, unverified"
        # (never re-run or merged again), not a refusal or an unexpected crash.
        try:
            self._run_post_merge_hook()
            self._verify_merged(rec, head, oid, result)
        except MergeUnverifiedError:
            raise
        except Exception as exc:
            raise MergeUnverifiedError(
                f"{rec.describe()}: merged {oid}, but checking the result failed: {exc}; "
                "nothing was reset: inspect the base repo manually"
            ) from exc
        return True

    def _run_post_merge_hook(self) -> None:
        """Run the base's post-merge hook as `git merge` would (argument 0: not a
        squash; e.g. Git LFS). Its output, exit status and failures cannot undo the
        merge and are ignored; it runs without Foundry's committer identity."""
        with suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run(
                ["git", "hook", "run", "--ignore-missing", "post-merge", "--", "0"],
                cwd=str(self.ws), capture_output=True, timeout=600, env=_git_env(),
            )

    def _apply_result(
        self, rec: WorktreeRecord, task_id: str, head: str, oid: str, result: str,
        changed: list[str], snapshot: _Snapshot,
    ) -> None:
        """Write the checked merge result `result` to the base and commit it on `head`.

        Plumbing only, never porcelain `git merge`: on any failure, git's merge resets
        the whole working tree to HEAD and re-applies a stash of the user's edits, and
        if that reset fails partway the edits are lost. Here:
        1. `read-tree --no-recurse-submodules -m -u head result` (two-way; never enters
           a submodule, whatever `submodule.recurse` says): updates the index and the files of
           the paths the task changes (all verified clean), leaves every other entry,
           unstaged edit and assume-unchanged/skip-worktree flag as it is, and never
           resets or stashes (under a sparse checkout it re-applies the sparse
           patterns, which only adds or removes clean files). If it fails, the index
           is not written.
        2. `commit-tree` with parents `head` and the task commit, then `update-ref HEAD`
           only if HEAD is still `head` (the old-value check closes the race with a
           concurrent commit).
        No pre-merge-commit/commit-msg hooks run; post-merge hooks (e.g. Git LFS) do.
        A checked-out submodule whose gitlink the task bumps keeps its checkout (git
        then shows it as modified); updating it is left to the user."""
        build = f" [build {self.build_id}]" if self.build_id else ""
        message = f"foundry: merge task {task_id}{build} (commit {oid})"
        written = (
            f"Only the task's paths can have been written (none had local edits): "
            f"{_shown(changed)}; nothing else was touched. To undo, restore those paths from "
            "HEAD (`git checkout HEAD -- <path>`) and delete any the task added"
        )
        read_tree = ["read-tree", "--no-recurse-submodules", "-m", "-u", head, result]
        # Last guard before the first write, as close to it as Python allows: every
        # path the result writes must still be exactly what the checks saw (`lstat`
        # identity). A same-user write landing between this compare and git's own
        # open of the file is the hostile same-UID writer class, which Foundry does
        # not defend against (README, "Before you run it": the trusted operator or
        # the sandbox provides that boundary). Nothing has been written yet, so this
        # is a refusal (merge_ran=False), never a merge failure.
        now = _fs_snapshot(self.ws, changed)
        if now != snapshot:
            moved = sorted(p for p in changed if now.get(p) != snapshot.get(p))
            raise IntegrationError(
                f"{rec.describe()}: not merged: base repo {self.ws} changed on disk in the "
                f"instant before the write, at paths the task writes: {_shown(moved)}; "
                "nothing was written; retry when the base is quiet"
            )
        # Recorded before the first write: a Ctrl-C while git runs may leave it done.
        self.written[task_id] = changed
        try:
            rc, output = _git(read_tree, self.ws)
        except subprocess.TimeoutExpired as exc:
            raise IntegrationError(
                f"{rec.describe()}: not merged: git read-tree of the checked result timed "
                f"out; git may have left .git/index.lock behind. {written}",
                merge_ran=True,
            ) from exc
        if rc != 0:
            raise IntegrationError(
                f"{rec.describe()}: not merged: git read-tree of the checked result failed: "
                f"{_brief(output)}; afterwards {self._base_state(head)}. {written}",
                merge_ran=True,
            )
        applied = (
            f"the task's changes are applied to the base working tree and index but not "
            f"committed (paths: {_shown(changed)}); commit them (`git commit`), or restore "
            "those paths from HEAD"
        )
        sign = _git(["config", "--type=bool", "--get", "commit.gpgsign"], self.ws)[1] == "true"
        commit = ["commit-tree", *(["-S"] if sign else []), result, "-p", head, "-p", oid]
        try:
            rc, new = _git([*commit, "-m", message], self.ws)
        except subprocess.TimeoutExpired as exc:
            raise IntegrationError(
                f"{rec.describe()}: git commit-tree of the merge timed out (e.g. waiting for "
                f"a signing passphrase); {applied}",
                merge_ran=True,
            ) from exc
        if rc != 0 or not _is_oid(new):
            raise IntegrationError(
                f"{rec.describe()}: git commit-tree of the merge failed: {_brief(new)}; "
                f"{applied}",
                merge_ran=True,
            )
        try:
            rc, output = _git(["update-ref", "-m", message, "HEAD", new, head], self.ws)
        except subprocess.TimeoutExpired as exc:
            raise MergeUnverifiedError(
                f"{rec.describe()}: moving HEAD from {head} to the merge {new} timed out, so "
                f"HEAD may or may not hold the merge; if it does not, {applied}"
            ) from exc
        if rc != 0:
            raise IntegrationError(
                f"{rec.describe()}: could not move HEAD from {head} to the merge {new}: "
                f"{_brief(output)}; {applied}",
                merge_ran=True,
            )


    def merged_tasks(self, build_id: str) -> dict[str, str]:
        """Task id -> merge commit for every merge of build `build_id` reachable from
        the base HEAD: the git history, not the session file, is the record of what
        this build already merged."""
        if not build_id:
            return {}
        # Every ref, not just HEAD: a merge stays a merge after the user switches
        # branches, and running the task again on the new branch would repeat it. A
        # ref whose object is missing (a stale ref) cannot hold a readable merge: it is
        # skipped and listed in `unreadable_refs` instead of failing the whole scan.
        rc, refs_out = _git(["for-each-ref", "--format=%(objectname) %(refname)"], self.ws)
        if rc != 0:
            raise RuntimeError(f"git for-each-ref failed in {self.ws}: {refs_out}")
        tips: list[tuple[str, str]] = [
            (oid, name) for oid, _, name in (line.partition(" ") for line in refs_out.splitlines())
        ]
        rc, head = _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], self.ws)
        if rc == 0 and head:
            tips.append((head, "HEAD"))
        proc = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=str(self.ws), capture_output=True, timeout=120, env=_git_env(),
            input="".join(f"{oid}\n" for oid, _ in tips).encode(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git cat-file failed in {self.ws}: {proc.stderr.decode()}")
        checks = proc.stdout.decode().splitlines()
        if len(checks) != len(tips):
            raise RuntimeError(f"git cat-file answered {len(checks)} of {len(tips)} refs")
        readable = [oid for (oid, _), check in zip(tips, checks, strict=True)
                    if not check.endswith(" missing")]
        self.unreadable_refs = [name for (_, name), check in zip(tips, checks, strict=True)
                                if check.endswith(" missing")]
        if not readable:
            return {}
        proc = subprocess.run(
            ["git", "log", "--stdin", "--format=%H%x1f%s"],
            cwd=str(self.ws), capture_output=True, timeout=120, env=_git_env(),
            input="".join(f"{oid}\n" for oid in readable).encode(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git log failed in {self.ws}: {proc.stderr.decode()}")
        out = proc.stdout.decode("utf-8", "replace")
        found: dict[str, str] = {}
        stamp = re.compile(
            rf"^foundry: merge task (.+) \[build {re.escape(build_id)}\] \(commit [0-9a-f]+\)$"
        )
        for line in out.splitlines():
            sha, _, subject = line.partition("\x1f")
            match = stamp.match(subject)
            if match:
                found.setdefault(match.group(1), sha)
        return found

    def _base_state(self, head: str) -> str:
        """What Foundry can observe about the base after a failed merge."""
        try:
            rc, now = _git(["rev-parse", "--verify", "HEAD^{commit}"], self.ws)
            staged = _git(["diff", "--cached", "--quiet"], self.ws)[0]
        except (subprocess.TimeoutExpired, OSError):
            return "base state unknown (git did not answer)"
        moved = "HEAD unchanged" if rc == 0 and now == head else f"HEAD is {now or 'unreadable'}"
        index = "index matches HEAD" if staged == 0 else "index differs from HEAD"
        return f"base {moved}, {index}"

    def _merge_pins(self) -> list[str]:
        """`-c` options for `git merge-tree`, so the merge Foundry checks and applies
        does not depend on the user's config."""
        pins = [arg for setting in _MERGE_CONFIG for arg in ("-c", setting)]
        rc, ref = _git(["symbolic-ref", "-q", "HEAD"], self.ws)
        branch = ref[len("refs/heads/"):] if rc == 0 and ref.startswith("refs/heads/") else ""
        if branch and "=" not in branch:  # `-c` splits at the first '='
            # branch.<name>.mergeOptions (e.g. --squash, -Xours) would change the merge.
            pins += ["-c", f"branch.{branch}.mergeOptions="]
        return pins

    def _verify_merged(self, rec: WorktreeRecord, head: str, oid: str, result: str) -> None:
        """After applying the merge: HEAD must be a merge of exactly `head` and `oid` whose
        tree is the precomputed `result`. A mismatch (e.g. a concurrent commit) is
        reported; the merge is never reset or undone."""
        try:
            new = _checked_git(["rev-list", "--parents", "-n", "1", "HEAD"], self.ws).split()
            tree = _checked_git(["rev-parse", "--verify", "HEAD^{tree}"], self.ws)
        except RuntimeError as exc:
            raise MergeUnverifiedError(
                f"{rec.describe()}: merged {oid} but cannot verify the result: {exc}"
            ) from exc
        if new[1:] != [head, oid] or tree != result:
            raise MergeUnverifiedError(
                f"{rec.describe()}: merged {oid}, but the base result differs from the checked "
                f"merge (expected tree {result} on {head}; got commit {new[0]} with parents "
                f"{' '.join(new[1:]) or 'none'} and tree {tree}); nothing was reset: "
                "inspect the base repo manually"
            )

    @staticmethod
    def _clear_assume_unchanged(wt: Path) -> None:
        """In Foundry's own worktree only: clear assume-unchanged bits (set on every
        checked-out file under core.ignoreStat=true, or by the worker), which would
        make `add -A` skip the worker's edits to those files. Skip-worktree bits are
        kept: clearing them would record files a sparse checkout omits as deleted."""
        fields = _git_paths(["ls-files", "-v", "-z"], wt)
        flagged = [field[2:] for field in fields if field[:1].islower()]
        if flagged:
            _git_paths(["update-index", "--no-assume-unchanged", "-z", "--stdin"], wt,
                       "".join(f"{path}\0" for path in flagged))

    def _keep_scratch_as_at_start(self, start: str, tree: str) -> str:
        """`tree` with its top-level `.foundry` entry (Foundry's scratch: staged skills,
        etc.) replaced by whatever `start` had there, usually nothing. Worktrees check
        out the committed .gitignore, which may not ignore `.foundry/`, and a worker
        may even commit it; neither may put scratch into the base history. The user's
        .gitignore is not touched."""
        def entries(tree_ish: str) -> list[str]:
            return _git_paths(["ls-tree", "-z", tree_ish], self.ws)

        def is_scratch(entry: str) -> bool:
            return entry.split("\t", 1)[1] == self.SCRATCH

        current, at_start = entries(tree), entries(start)
        scratch = [e for e in current if is_scratch(e)]
        if scratch == [e for e in at_start if is_scratch(e)]:
            return tree
        kept = [e for e in current if not is_scratch(e)] + [e for e in at_start if is_scratch(e)]
        return _git_paths(["mktree", "-z"], self.ws, "".join(f"{e}\0" for e in kept))[0].strip()

    def _new_gitlinks(self, start: str, tree: str) -> list[str]:
        """Paths that are gitlinks (mode 160000) in `tree` but were not at `start`."""
        fields = _git_paths(["diff-tree", "-r", "-z", "--no-renames", start, tree], self.ws)
        # -z raw format: ":<old mode> <new mode> <old oid> <new oid> <status>" NUL <path>
        pairs = zip(fields[::2], fields[1::2], strict=True)
        return [path for meta, path in pairs
                if meta.split()[1] == "160000" and meta.split()[0][1:] != "160000"]

    def _worktree_gitdir(self, wt: Path) -> str:
        gitdir = _checked_git(["rev-parse", "--absolute-git-dir"], wt)
        common = Path(_checked_git(["rev-parse", "--git-common-dir"], self.ws))
        admin = (common if common.is_absolute() else self.ws / common).resolve() / "worktrees"
        if Path(gitdir).resolve().parent != admin:
            raise RuntimeError(f"{wt} uses git dir {gitdir}, not a worktree of {self.ws}")
        return gitdir

    def _verify_owned(self, rec: WorktreeRecord) -> None:
        """The recorded path must still be Foundry's own worktree: its own top level and
        admin dir (not a parent repo found by discovery), holding Foundry's nonce (a
        reclaimed worktree at the same names lacks it), with the recorded branch checked
        out. Same-user actors can still race this; it closes the deterministic cases."""
        top = _checked_git(["rev-parse", "--show-toplevel"], rec.path)
        if Path(top).resolve() != rec.path.resolve():
            raise IntegrationError(f"{rec.describe()}: not a worktree top level (git found {top})")
        if self._worktree_gitdir(rec.path) != rec.gitdir:
            raise IntegrationError(f"{rec.describe()}: no longer uses git dir {rec.gitdir}")
        owner = Path(rec.gitdir) / self.OWNER_FILE
        if not rec.nonce or not owner.is_file() or owner.read_text(encoding="utf-8") != rec.nonce:
            raise IntegrationError(f"{rec.describe()}: not Foundry's worktree (reclaimed?)")
        # A worker left mid-merge/rebase/cherry-pick (or with unmerged paths from e.g. a
        # conflicting `stash pop`) would have its conflict markers committed as-is.
        busy = self._busy_reason(rec.path)
        if busy:
            raise IntegrationError(f"{rec.describe()}: worktree has {busy}; not committed")
        head = _checked_git(["symbolic-ref", "-q", "HEAD"], rec.path)
        if head != rec.ref:
            raise IntegrationError(f"{rec.describe()}: has {head} checked out instead")

    @staticmethod
    def _git_path(name: str, cwd: Path) -> Path:
        path = Path(_checked_git(["rev-parse", "--git-path", name], cwd))
        return path if path.is_absolute() else cwd / path

    def _busy_reason(self, cwd: Path) -> str | None:
        """Why the checkout at `cwd` is mid-operation (None if idle): a merge,
        cherry-pick, revert or rebase in progress, or unmerged index entries."""
        busy = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
        for name in busy:
            if self._git_path(name, cwd).exists():
                return f"an operation in progress ({name})"
        if _checked_git(["ls-files", "--unmerged"], cwd):
            return "unmerged paths"
        return None

    def _require_quiet_base(self) -> None:
        """Refuse (touching nothing) unless the base repo is idle: no merge, cherry-pick,
        revert or rebase in progress, no unmerged paths and no staged changes. Unstaged
        tracked edits are allowed unless the merge changes those paths (checked
        against the computed result); the merge never autostashes them."""
        busy = self._busy_reason(self.ws)
        if busy:
            raise IntegrationError(f"base repo {self.ws} has {busy}")
        rc, output = _git(["diff", "--cached", "--quiet"], self.ws)
        if rc != 0:
            raise IntegrationError(f"base repo {self.ws} has staged changes ({output or 'diff'})")

    def _require_result_fits_base(
        self, rec: WorktreeRecord, head: str, result: str
    ) -> list[str]:
        """Refuse (touching nothing) unless the base working tree can take the merge
        result `result` (a tree, computed against the base commit `head`) without
        losing anything of the user's. Returns the paths the result changes.

        1. No path the merge changes may have unstaged edits: git would refuse anyway
           (and with `merge.autoStash` would stash, merge and re-apply them).
        2. No path the result adds that `head` does not track may exist on disk (file,
           dir or symlink; untracked or ignored alike, which `lstat` covers whatever
           the ignore rules): git silently overwrites IGNORED files, and ignore rules
           may differ between the base and the task. A directory `head` tracks is
           fine only if everything in it is tracked (and so checked by git). No
           parent may be an untracked file or symlink.
        Checking the computed result, not the task's own paths, covers every path
        the merge writes, including ones Git's rename handling would choose."""
        fields = _git_paths(["diff-tree", "-r", "-z", "--no-renames", head, result], self.ws)
        # -z raw format: ":<old mode> <new mode> <old oid> <new oid> <status>" NUL <path>
        entries = [(meta[1:].split()[0], meta[1:].split()[1], path)
                   for meta, path in zip(fields[::2], fields[1::2], strict=True)]
        changed = {path for _, _, path in entries}
        dirty = set(_git_paths(["diff", "--no-ext-diff", "--name-only", "-z"], self.ws))
        hidden, sparse = self._hidden_edits()
        clash = sorted((dirty | hidden | sparse) & changed)
        if clash:
            flagged = " (some marked assume-unchanged or skip-worktree)" if (
                (hidden | sparse) & changed) else ""
            raise IntegrationError(
                f"{rec.describe()}: not merged: base repo {self.ws} has unstaged changes{flagged} "
                f"that would be overwritten by the merge: {_shown(clash)}; commit or move them "
                "aside, or merge the task commit manually"
            )
        if hidden:
            raise IntegrationError(
                f"{rec.describe()}: not merged: base repo {self.ws} has local edits marked "
                f"assume-unchanged or skip-worktree: {_shown(sorted(hidden))}; git cannot see "
                "them, and if its merge failed it would reset them to HEAD. Clear the flag "
                "(`git update-index --no-assume-unchanged` / `--no-skip-worktree`) and commit "
                "or move them aside, or merge the task commit manually"
            )
        # Paths `head` tracks that the result removes or changes (all of them, for a
        # tracked directory the result replaces with a file).
        tracked = {path for old, _, path in entries if old != _NO_MODE}
        blocked: set[str] = set()
        for old, new, rel in entries:
            # A tracked path the result turns into a file or symlink (or a gitlink it
            # removes): `read-tree -u` deletes whatever directory is there, e.g. a
            # checked-out submodule with its own repo, which `git diff` does not report
            # when only its untracked/ignored content differs.
            if old != _NO_MODE and new != _GITLINK and (new != _NO_MODE or old == _GITLINK):
                st = self._lstat_in_tree(rel)
                if st is not None and stat.S_ISDIR(st.st_mode) and self._dir_has_untracked(
                    self.ws.joinpath(*rel.split("/")), rel, tracked
                ):
                    blocked.add(rel)
                continue
            if old != _NO_MODE or new == _NO_MODE:
                continue  # only paths new to the base can collide with untracked files
            parts = rel.split("/")
            for depth in range(1, len(parts) + 1):
                sub = "/".join(parts[:depth])
                path = self.ws.joinpath(*parts[:depth])
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    break  # nothing on disk here, so nothing deeper either
                if depth == len(parts):
                    if not (stat.S_ISDIR(st.st_mode) and self._only_tracked_in(path, sub, tracked)):
                        blocked.add(sub)
                elif not stat.S_ISDIR(st.st_mode):
                    # A file/symlink where the result needs a directory: fine only if
                    # `head` tracks it (git then checks it like any tracked file).
                    if sub not in tracked:
                        blocked.add(sub)
                    break
        if blocked:
            raise IntegrationError(
                f"{rec.describe()}: not merged: base repo {self.ws} has untracked or "
                f"ignored files (or a checked-out submodule) where the task writes or removes "
                f"files: {_shown(sorted(blocked))}; move them aside or merge the task commit "
                "manually"
            )
        return sorted(changed)

    def _hidden_edits(self) -> tuple[set[str], set[str]]:
        """Base index entries marked assume-unchanged or skip-worktree, which `git diff`
        (and the stash git's merge takes) skip, compared with the working tree directly.

        Returns (`hidden`, `sparse`): `hidden` = flagged entries whose file differs from
        the index entry in content, mode, type or existence; `sparse` = skip-worktree
        entries absent from disk (the normal sparse-checkout state: nothing to lose,
        and git's failure-path reset does not recreate them, so only a merge that
        changes one is refused). Content is hashed by `git hash-object` with the path's
        filters, as `git add` would. An entry flagged both ways is listed by git as
        assume-unchanged and treated as such."""
        fields = _git_paths(["ls-files", "-s", "-v", "-z"], self.ws)
        filemode = _git(["config", "--type=bool", "--get", "core.fileMode"], self.ws)[1] != "false"
        symlinks = _git(["config", "--type=bool", "--get", "core.symlinks"], self.ws)[1] != "false"
        hidden: set[str] = set()
        sparse: set[str] = set()
        to_hash: dict[str, str] = {}  # path -> index OID, for regular files on disk
        for field in fields:
            meta, path = field.split("\t", 1)
            tag, mode, oid = meta.split()[:3]
            if not (tag.islower() or tag == "S"):
                continue
            st = self._lstat_in_tree(path)
            if st is None:
                (sparse if tag == "S" else hidden).add(path)
            elif mode == "160000":
                if not stat.S_ISDIR(st.st_mode):
                    hidden.add(path)
            elif mode == "120000":
                if stat.S_ISLNK(st.st_mode):
                    target = os.readlink(os.fsencode(self.ws / path))
                elif stat.S_ISREG(st.st_mode) and not symlinks:
                    target = (self.ws / path).read_bytes()
                else:
                    hidden.add(path)
                    continue
                if _blob_oid(target, len(oid)) != oid:
                    hidden.add(path)
            elif not stat.S_ISREG(st.st_mode) or (
                filemode and bool(st.st_mode & 0o100) != (mode == "100755")
            ):
                hidden.add(path)
            else:
                to_hash[path] = oid
        hashes = self._hash_worktree_files(list(to_hash))
        hidden.update(path for path, oid in to_hash.items() if hashes[path] != oid)
        return hidden, sparse

    def _lstat_in_tree(self, rel: str) -> os.stat_result | None:
        """lstat of repo path `rel` under the base, or None if it (or a parent that
        is not a real directory, e.g. a file or symlink) is missing."""
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            try:
                if not stat.S_ISDIR(os.lstat(self.ws.joinpath(*parts[:depth])).st_mode):
                    return None
            except FileNotFoundError:
                return None
        try:
            return os.lstat(self.ws.joinpath(*parts))
        except FileNotFoundError:
            return None

    def _hash_worktree_files(self, paths: list[str]) -> dict[str, str]:
        """Blob OIDs of base working-tree files, filtered (clean/eol) by path."""
        # --stdin-paths reads lines (and unquotes a leading '"'); other names go one by one.
        plain = [
            p for p in paths if "\n" not in p and not p.startswith('"') and not p.endswith("\r")
        ]
        oids: dict[str, str] = {}
        if plain:
            stdin = "".join(f"{p}\n" for p in plain).encode("utf-8", "surrogateescape")
            proc = subprocess.run(
                ["git", "hash-object", "--stdin-paths"], cwd=str(self.ws), capture_output=True,
                timeout=120, input=stdin, env=_git_env(),
            )
            out = proc.stdout.decode().split()
            if proc.returncode != 0 or len(out) != len(plain):
                err = proc.stderr.decode().strip()
                raise RuntimeError(f"git hash-object failed in {self.ws}: {err}")
            oids.update(zip(plain, out, strict=True))
        for path in paths:
            if path not in oids:
                proc = subprocess.run(
                    ["git", "hash-object", f"--path={path}", "--stdin"], cwd=str(self.ws),
                    capture_output=True, timeout=120, input=(self.ws / path).read_bytes(),
                    env=_git_env(),
                )
                if proc.returncode != 0:
                    err = proc.stderr.decode().strip()
                    raise RuntimeError(f"git hash-object failed in {self.ws}: {err}")
                oids[path] = proc.stdout.decode().strip()
        return oids

    @classmethod
    def _dir_has_untracked(cls, path: Path, rel: str, tracked: set[str]) -> bool:
        """Whether the on-disk directory `path` holds anything besides the tracked
        paths `tracked` (an empty directory holds nothing to lose)."""
        with os.scandir(path) as it:
            if next(it, None) is None:
                return False
        return not cls._only_tracked_in(path, rel, tracked)

    @staticmethod
    def _only_tracked_in(path: Path, rel: str, tracked: set[str]) -> bool:
        """Whether the on-disk directory `path` (repo path `rel`) holds nothing but
        files and directories of the tracked paths `tracked` (no symlink is followed)."""
        prefix = f"{rel}/"
        inside = {p for p in tracked if p.startswith(prefix)}
        dirs = {p[: i] for p in inside for i, ch in enumerate(p) if ch == "/"}
        if not inside:
            return False
        pending = [(path, rel)]
        while pending:
            directory, name = pending.pop()
            with os.scandir(directory) as it:
                for entry in it:
                    child = f"{name}/{entry.name}"
                    if entry.is_dir(follow_symlinks=False):
                        if child not in dirs:
                            return False
                        pending.append((Path(entry.path), child))
                    elif child not in inside:
                        return False
        return True

    def discard(self, task_id: str) -> WorktreeRecord | None:
        """Release a task's worktree WITHOUT deleting anything; returns the record
        now listed in `retained` for manual cleanup (None if none was active).

        Runs no git command: a recorded path/ref may since have been reclaimed by
        another Git actor, so no check-then-delete sequence is safe."""
        rec = self._active.pop(task_id, None)
        if rec is not None:
            self.retained.append(rec)
        return rec

    def leftover_resources(self) -> list[str]:
        """Report (never delete) `.foundry/worktrees/*` paths and `foundry-wt/*`
        branches that already exist, i.e. left by earlier builds. Call before create()."""
        items = [str(p) for p in sorted(self.root.iterdir())] if self.root.is_dir() else []
        prefix = f"refs/heads/{self.BRANCH_PREFIX}"
        refs = _checked_git(["for-each-ref", "--format=%(refname)", prefix], self.ws)
        return items + [r for r in refs.splitlines() if r]

    def legacy_resources(self) -> dict[str, list[str]]:
        """Report (never delete) pre-existing task-named `.foundry/wt/<task>` paths
        and `refs/heads/foundry/*` branches from older Foundry versions."""
        paths: dict[Path, str] = {}
        if self.legacy_root.is_dir():
            for child in sorted(self.legacy_root.iterdir()):
                paths.setdefault(child.resolve(), str(child))
        legacy = self.legacy_root.resolve()
        listing = _checked_git(["worktree", "list", "--porcelain"], self.ws)
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
                if path.resolve().parent == legacy:
                    paths.setdefault(path.resolve(), str(path))
        refs = _checked_git(["for-each-ref", "--format=%(refname)", "refs/heads/foundry/"], self.ws)
        return {"paths": list(paths.values()), "refs": [r for r in refs.splitlines() if r]}
