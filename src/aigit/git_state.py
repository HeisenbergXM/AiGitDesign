"""Read-only Git worktree snapshots and text deltas."""

from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

from aigit.canonical import canonical_json, hash_bytes
from aigit.domain import ActionKind, Classification, GitSnapshot, PatchSpan
from aigit.local_store import BlobIntegrityError, LocalStore


MAX_FILE_BYTES = 2 * 1024 * 1024

_VENDORED_DIRECTORIES = frozenset(
    {
        ".bundle",
        ".venv",
        "bower_components",
        "deps",
        "node_modules",
        "third-party",
        "third_party",
        "vendor",
        "venv",
    }
)
_GENERATED_DIRECTORIES = frozenset(
    {
        ".coverage",
        ".gradle",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "generated",
        "out",
        "target",
    }
)
_LOCKFILES = frozenset(
    {
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "mix.lock",
        "package-lock.json",
        "packages.lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "podfile.lock",
        "poetry.lock",
        "pubspec.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_SECRET_FILENAMES = frozenset(
    {
        ".netrc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SECRET_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
_UNKNOWN_PREFIX = "unknown:"


@dataclass(frozen=True)
class SnapshotPolicy:
    """Repository-specific additions to the central snapshot exclusions."""

    exclude_globs: tuple[str, ...] = ()
    max_file_bytes: int = MAX_FILE_BYTES

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        object.__setattr__(self, "exclude_globs", tuple(self.exclude_globs))


DEFAULT_POLICY = SnapshotPolicy()


def _git(root: Path, *arguments: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout


def find_repo(path: str | Path) -> Path:
    """Return the physical root of the Git repository containing *path*."""
    candidate = Path(path)
    working_directory = candidate if candidate.is_dir() else candidate.parent
    output = _git(working_directory, "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve()


def repo_id(root: str | Path) -> str:
    """Return a deterministic local identity for a repository root."""
    physical_root = find_repo(root)
    normalized = os.path.normcase(os.fspath(physical_root))
    return hash_bytes(os.fsencode(normalized))


def capture_snapshot(
    root: str | Path,
    store: LocalStore,
    policy: SnapshotPolicy = DEFAULT_POLICY,
) -> GitSnapshot:
    """Capture tracked and untracked worktree content without changing Git state."""
    repository = find_repo(root)
    head = os.fsdecode(_git(repository, "rev-parse", "HEAD").strip())
    cached_diff = _git(repository, "diff", "--cached", "--binary")
    listed_files = _git(
        repository,
        "ls-files",
        "-co",
        "--exclude-standard",
        "-z",
    )

    paths = {
        os.fsdecode(raw_path).replace("\\", "/")
        for raw_path in listed_files.split(b"\0")
        if raw_path
    }
    files: dict[str, str] = {}
    for relative_path in sorted(paths):
        if _is_excluded(relative_path, policy):
            continue
        worktree_path = _worktree_path(repository, relative_path)
        if worktree_path is None:
            files[relative_path] = _unknown_reference("unsafe-path", relative_path)
            continue
        reference = _capture_file(worktree_path, store, policy)
        if reference is not None:
            files[relative_path] = reference

    return GitSnapshot(
        head=head,
        index_hash=hash_bytes(cached_diff),
        worktree_hash=hash_bytes(canonical_json(files)),
        files=files,
    )


def diff_snapshots(
    before: GitSnapshot,
    after: GitSnapshot,
    store: LocalStore,
) -> list[PatchSpan]:
    """Compute ordered text spans introduced between two worktree snapshots."""
    spans: list[PatchSpan] = []
    for path in sorted(before.files.keys() | after.files.keys()):
        old_reference = before.files.get(path)
        new_reference = after.files.get(path)
        if old_reference == new_reference:
            continue

        if _is_unknown(old_reference) or _is_unknown(new_reference):
            spans.append(_unknown_span(path, old_reference, new_reference))
            continue

        try:
            old_lines = _blob_lines(store, old_reference)
            new_lines = _blob_lines(store, new_reference)
        except (BlobIntegrityError, OSError, UnicodeError, ValueError):
            spans.append(_unknown_span(path, old_reference, new_reference))
            continue

        matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation == "equal":
                continue
            spans.append(
                PatchSpan(
                    path=path,
                    old_start=old_start,
                    old_end=old_end,
                    new_start=new_start,
                    new_end=new_end,
                    old_lines=_normalize_lines(old_lines[old_start:old_end]),
                    new_lines=_normalize_lines(new_lines[new_start:new_end]),
                    classification=Classification.UNKNOWN,
                    action=_action_for(operation),
                    confidence=0.0,
                )
            )
    return spans


def _is_excluded(relative_path: str, policy: SnapshotPolicy) -> bool:
    path = PurePosixPath(relative_path)
    folded_parts = tuple(part.casefold() for part in path.parts)
    if ".git" in folded_parts or ".aigit" in folded_parts:
        return True
    if any(part in _VENDORED_DIRECTORIES for part in folded_parts):
        return True
    if any(part in _GENERATED_DIRECTORIES for part in folded_parts[:-1]):
        return True

    filename = folded_parts[-1] if folded_parts else ""
    if filename in _LOCKFILES:
        return True
    if filename.endswith((".min.css", ".min.js", ".min.mjs", ".min.cjs")):
        return True
    if _is_secret(filename):
        return True

    folded_path = relative_path.casefold()
    return any(
        fnmatch.fnmatchcase(folded_path, pattern.replace("\\", "/").casefold())
        for pattern in policy.exclude_globs
    )


def _is_secret(filename: str) -> bool:
    return (
        filename == ".env"
        or filename.startswith(".env.")
        or filename in _SECRET_FILENAMES
        or filename.endswith(_SECRET_SUFFIXES)
    )


def _worktree_path(repository: Path, relative_path: str) -> Path | None:
    posix_path = PurePosixPath(relative_path)
    if posix_path.is_absolute() or any(
        part in {"", ".", ".."} for part in posix_path.parts
    ):
        return None
    return repository.joinpath(*posix_path.parts)


def _capture_file(
    path: Path,
    store: LocalStore,
    policy: SnapshotPolicy,
) -> str | None:
    max_file_bytes = min(policy.max_file_bytes, MAX_FILE_BYTES)
    try:
        before_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _unknown_reference("unreadable", type(exc).__name__)

    if stat.S_ISLNK(before_stat.st_mode):
        return _unknown_reference("symlink", _stat_identity(before_stat))
    if not stat.S_ISREG(before_stat.st_mode):
        return _unknown_reference("special", _stat_identity(before_stat))
    if before_stat.st_size > max_file_bytes:
        return _unknown_reference("oversized", _stat_identity(before_stat))

    try:
        with path.open("rb") as source:
            data = source.read(max_file_bytes + 1)
        after_stat = path.lstat()
    except OSError as exc:
        return _unknown_reference(
            "unreadable",
            _stat_identity(before_stat) + ":" + type(exc).__name__,
        )

    if _stat_identity(before_stat) != _stat_identity(after_stat):
        fingerprint = canonical_json(
            {
                "before": _stat_identity(before_stat),
                "after": _stat_identity(after_stat),
                "bytes": hash_bytes(data),
            }
        )
        return _unknown_reference("concurrent", os.fsdecode(fingerprint))
    if len(data) > max_file_bytes:
        return _unknown_reference("oversized", _stat_identity(after_stat))
    if _is_binary(data):
        return _unknown_reference("binary", hash_bytes(data))
    return store.put_blob(data)


def _stat_identity(file_stat: os.stat_result) -> str:
    return ":".join(
        str(value)
        for value in (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mode,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )
    )


def _unknown_reference(reason: str, evidence: str) -> str:
    return f"{_UNKNOWN_PREFIX}{reason}:{hash_bytes(os.fsencode(evidence))}"


def _is_unknown(reference: str | None) -> bool:
    return reference is not None and reference.startswith(_UNKNOWN_PREFIX)


def _is_binary(data: bytes) -> bool:
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _blob_lines(store: LocalStore, reference: str | None) -> tuple[str, ...]:
    if reference is None:
        return ()
    return tuple(
        store.get_blob(reference).decode("utf-8").splitlines(keepends=True)
    )


def _normalize_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line.rstrip("\r\n") for line in lines)


def _unknown_span(
    path: str,
    old_reference: str | None,
    new_reference: str | None,
) -> PatchSpan:
    if old_reference is None:
        action = ActionKind.ADDED
    elif new_reference is None:
        action = ActionKind.DELETED
    else:
        action = ActionKind.REPLACED
    return PatchSpan(
        path=path,
        old_start=0,
        old_end=0,
        new_start=0,
        new_end=0,
        old_lines=(),
        new_lines=(),
        classification=Classification.UNKNOWN,
        action=action,
        confidence=0.0,
    )


def _action_for(operation: str) -> ActionKind:
    if operation == "insert":
        return ActionKind.ADDED
    if operation == "delete":
        return ActionKind.DELETED
    return ActionKind.REPLACED
