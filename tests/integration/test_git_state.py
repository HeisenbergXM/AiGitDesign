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
from aigit.domain import ActionKind, Classification
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


@pytest.mark.parametrize(
    ("before_content", "after_content"),
    [
        pytest.param(b"x\n", b"x", id="remove-final-newline"),
        pytest.param(b"x\n", b"x\r\n", id="lf-to-crlf"),
    ],
)
def test_newline_only_change_emits_replaced_span(
    repo: Path,
    store: LocalStore,
    before_content: bytes,
    after_content: bytes,
) -> None:
    (repo / "app.py").write_bytes(before_content)
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    (repo / "app.py").write_bytes(after_content)
    after = capture_snapshot(repo, store, DEFAULT_POLICY)

    spans = diff_snapshots(before, after, store)

    assert len(spans) == 1
    span = spans[0]
    assert span.path == "app.py"
    assert span.action is ActionKind.REPLACED
    assert (span.old_start, span.old_end) == (0, 1)
    assert (span.new_start, span.new_end) == (0, 1)
    assert span.old_lines == ("x",)
    assert span.new_lines == ("x",)


def test_snapshot_delta_excludes_preexisting_staged_diff(
    repo: Path, store: LocalStore
) -> None:
    (repo / "app.py").write_text("manual = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    (repo / "app.py").write_text(
        "manual = 1\nai_fix = 2\n", encoding="utf-8"
    )
    after = capture_snapshot(repo, store, DEFAULT_POLICY)

    spans = diff_snapshots(before, after, store)

    assert len(spans) == 1
    assert spans[0].action is ActionKind.ADDED
    assert spans[0].new_lines == ("ai_fix = 2",)


def test_binary_addition_is_unknown_without_source_blob(
    repo: Path, store: LocalStore
) -> None:
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    source = b"binary\x00content"
    (repo / "asset.bin").write_bytes(source)
    after = capture_snapshot(repo, store, DEFAULT_POLICY)

    spans = diff_snapshots(before, after, store)

    assert after.files["asset.bin"].startswith("unknown:binary:")
    assert len(spans) == 1
    assert spans[0].path == "asset.bin"
    assert spans[0].classification is Classification.UNKNOWN
    assert spans[0].action is ActionKind.ADDED
    with pytest.raises(FileNotFoundError):
        store.get_blob(hash_bytes(source))


def test_file_deletion_emits_deleted_span_with_correct_ranges(
    repo: Path, store: LocalStore
) -> None:
    before = capture_snapshot(repo, store, DEFAULT_POLICY)
    (repo / "app.py").unlink()
    after = capture_snapshot(repo, store, DEFAULT_POLICY)

    spans = diff_snapshots(before, after, store)

    assert len(spans) == 1
    span = spans[0]
    assert span.path == "app.py"
    assert span.action is ActionKind.DELETED
    assert (span.old_start, span.old_end) == (0, 1)
    assert (span.new_start, span.new_end) == (0, 0)
    assert span.old_lines == ("committed = 0",)
    assert span.new_lines == ()


def test_central_exclusions_skip_secrets_and_common_lockfiles(
    repo: Path, store: LocalStore
) -> None:
    excluded_paths = {
        ".env",
        ".env.production",
        "credentials.json",
        "package-lock.json",
        "Podfile.lock",
        "mix.lock",
        "packages.lock.json",
        "pubspec.lock",
    }
    for relative_path in excluded_paths:
        (repo / relative_path).write_text(
            f"excluded content for {relative_path}\n", encoding="utf-8"
        )

    snapshot = capture_snapshot(repo, store, DEFAULT_POLICY)

    assert excluded_paths.isdisjoint(snapshot.files)


def test_space_and_unicode_filename_is_captured_via_nul_parsing(
    repo: Path, store: LocalStore
) -> None:
    relative_path = "na\u00efve file.txt"
    source = "captured exactly\n".encode()
    (repo / relative_path).write_bytes(source)

    snapshot = capture_snapshot(repo, store, DEFAULT_POLICY)

    reference = snapshot.files[relative_path]
    assert store.get_blob(reference) == source
