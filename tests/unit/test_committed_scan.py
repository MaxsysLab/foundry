"""Review corroboration reads committed blobs, not build leftovers or edits."""

import subprocess
from pathlib import Path

import pytest

from foundry import procenv, review, security
from foundry.models import ReviewFinding


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=procenv.clean_env(),
        text=True,
    ).stdout


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--template=")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("committed content\n")
    (tmp_path / ".gitattributes").write_text("app.py export-ignore\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


class Worker:
    def execute(self, _system, _instruction, _workspace, seat="generalist"):
        return '{"verdict":"approve","score":95,"findings":[]}'


def test_review_scans_only_committed_blobs_and_cleans_snapshot(monkeypatch, repository):
    root = repository
    (root / "app.py").write_text("staged edit\n")
    git(root, "add", "app.py")
    (root / "app.py").write_text("unstaged edit\n")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "junk").write_text("cache")
    git(root, "worktree", "add", "-b", "worker", ".worktrees/worker", "HEAD")
    visited = []

    def scan(path):
        path = Path(path)
        visited.append(path)
        assert path != root
        assert (path / "app.py").read_text() == "committed content\n"
        assert sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()) == [
            ".gitattributes",
            "app.py",
        ]
        return []

    monkeypatch.setattr(security, "critical_findings", scan)
    result = review.run_review(Worker(), "goal", root)
    assert result.security_corroborated is True
    assert result.approved
    assert visited and not visited[0].exists()
    assert (root / "app.py").read_text() == "unstaged edit\n"
    assert git(root, "show", ":app.py") == "staged edit\n"
    assert (root / ".worktrees/worker/app.py").exists()


def test_committed_critical_still_blocks_and_names_original_file(monkeypatch, repository):
    def scan(path):
        return [
            ReviewFinding(
                axis="security",
                severity="high",
                file=str(Path(path) / "app.py"),
                issue="critical",
            )
        ]

    monkeypatch.setattr(security, "critical_findings", scan)
    result = review.run_review(Worker(), "goal", repository)
    assert not result.approved
    assert result.security_corroborated is True
    assert result.findings[0].file == "app.py"


def test_missing_commit_is_missing_corroboration_not_clean(monkeypatch, tmp_path):
    git(tmp_path, "init", "--template=")

    def must_not_scan(_path):
        pytest.fail("an unavailable committed tree must not be called clean")

    monkeypatch.setattr(security, "critical_findings", must_not_scan)
    assert review.run_review(Worker(), "goal", tmp_path).security_corroborated is False


def test_scan_failure_is_visible_and_snapshot_is_removed(monkeypatch, repository):
    visited = []

    def scan(path):
        visited.append(Path(path))
        return None

    monkeypatch.setattr(security, "critical_findings", scan)
    assert review.run_review(Worker(), "goal", repository).security_corroborated is False
    assert visited and not visited[0].exists()


def test_committed_symlink_is_scanned_as_text_not_followed(monkeypatch, repository):
    outside = repository.parent / "private-input"
    outside.write_text("not part of this commit")
    (repository / "link").symlink_to(outside)
    git(repository, "add", "link")
    git(repository, "commit", "-m", "link")

    def scan(path):
        link = Path(path) / "link"
        assert not link.is_symlink()
        assert link.read_text() == str(outside)
        return []

    monkeypatch.setattr(security, "critical_findings", scan)
    assert review.run_review(Worker(), "goal", repository).security_corroborated is True


def test_submodule_is_not_reported_as_a_complete_scan(monkeypatch, repository):
    head = git(repository, "rev-parse", "HEAD").strip()
    git(repository, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
    git(repository, "commit", "-m", "gitlink")

    def must_not_scan(_path):
        pytest.fail("submodule contents are unavailable to the committed-blob scan")

    monkeypatch.setattr(security, "critical_findings", must_not_scan)
    assert review.run_review(Worker(), "goal", repository).security_corroborated is False
