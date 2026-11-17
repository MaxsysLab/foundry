"""Workspace filesystem probe: Foundry refuses case-insensitive filesystems.

The integration checks (`isolation._require_result_fits_base`), git's index and
the collision rules all treat `README.md` and `readme.md` as different paths. On a
case-folding filesystem (macOS APFS/HFS+ as formatted by default, Windows NTFS, some
SMB shares) they are the same file, so a task could overwrite a user file the checks
never saw, and git worktrees can disagree with the disk. Foundry has not been tested
there and does not pretend to handle it: at build start it creates a temporary
directory `.foundry-case-probe-<random>/` in the workspace, writes
`.foundry-case-probe-A` inside it, checks whether `.foundry-case-probe-a` resolves to
the same file, removes what it created, and refuses when it does. Documented in the
README under "Before you run it".
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PROBE_PREFIX = ".foundry-case-probe-"


class UnsupportedWorkspaceError(RuntimeError):
    """The workspace lives on a filesystem Foundry refuses to build on."""


def _resolves_to(upper: Path, lower: Path) -> bool:
    """Whether `lower` names the same existing file as `upper` (case folding)."""
    try:
        return os.path.samefile(lower, upper)
    except OSError:  # `lower` does not exist: the filesystem is case-sensitive
        return False


def workspace_is_case_insensitive(workspace: Path) -> bool:
    """Probe `workspace` (which must exist) and leave it exactly as found: the probe
    removes only the two entries it created, never recursively (an `rmdir` that
    fails because something else appeared in the directory propagates)."""
    probe_dir = Path(tempfile.mkdtemp(prefix=PROBE_PREFIX, dir=workspace))
    upper = probe_dir / f"{PROBE_PREFIX}A"
    try:
        upper.write_bytes(b"foundry case probe\n")
        try:
            return _resolves_to(upper, probe_dir / f"{PROBE_PREFIX}a")
        finally:
            os.unlink(upper)
    finally:
        os.rmdir(probe_dir)


def require_case_sensitive(workspace: Path) -> None:
    """Raise `UnsupportedWorkspaceError` unless `workspace` is on a case-sensitive
    filesystem; probe failures (e.g. an unwritable workspace) propagate as OSError."""
    if workspace_is_case_insensitive(workspace):
        raise UnsupportedWorkspaceError(
            f"workspace {workspace} is on a case-insensitive filesystem, which Foundry "
            "does not support (unsupported by design: its integration checks treat paths "
            "differing only in case as distinct, so a task could silently overwrite a file "
            "they never saw). Use a case-sensitive volume (e.g. a case-sensitive APFS "
            "volume on macOS, or a Linux container). See README.md, 'Before you run it'. "
            "Nothing was built."
        )
