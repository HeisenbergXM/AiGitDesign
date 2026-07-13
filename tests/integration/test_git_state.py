import os
import subprocess
from pathlib import Path

import pytest

from aigit.canonical import hash_bytes
from aigit.git_state import (
    DEFAULT_POLICY,
    SnapshotPolicy,
    capture_snapshot,
    diff_snapshots,
)
from aigit.local_store import LocalStore


def _git(repo: Path, *args: str) -> bytes:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "AIGit Test")
    _git(root, "config", "user.email", "aigit-test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    (root / "app.py").write_text("committed = 0\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    (root / "test_app.py").write_text("def test_existing():\n    pass\n", encoding="utf-8")
    return root


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "store")


def test_snapshot_delta_excludes_preexisting_dirty_diff(
    repo: Path, store: LocalStore
) -> None:
    (repo / "app.py").write_text("manual = 1\n", encoding="utf-8")
    status_before = _git(repo, "status", "--porcelain=v1")
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    assert _git(repo, "status", "--porcelain=v1") == status_before

    (repo / "app.py").write_text(
        "manual = 1\nai_fix = 2\n", encoding="utf-8"
    )
    status_before = _git(repo, "status", "--porcelain=v1")
    after = capture_snapshot(repo, store, DEFAULT_POLICY)
    assert _git(repo, "status", "--porcelain=v1") == status_before

    spans = diff_snapshots(before, after, store)
    assert [span.new_lines for span in spans] == [("ai_fix = 2",)]


def test_repository_policy_cannot_raise_central_file_size_limit(
    repo: Path, store: LocalStore
) -> None:
    source = b"x" * (2 * 1024 * 1024 + 1)
    (repo / "large.txt").write_bytes(source)
    permissive_policy = SnapshotPolicy(max_file_bytes=3 * 1024 * 1024)

    snapshot = capture_snapshot(repo, store, permissive_policy)

    assert snapshot.files["large.txt"].startswith("unknown:oversized:")
    with pytest.raises(FileNotFoundError):
        store.get_blob(hash_bytes(source))
