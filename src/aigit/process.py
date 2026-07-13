"""Detached, repo-scoped observer process lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess as _subprocess
import sys
import tempfile
from types import SimpleNamespace

from aigit.git_state import find_repo, repo_id


# Keep the detached-launch seam local to this module. Tests and embedders can
# replace ``aigit.process.subprocess.Popen`` without mutating the shared stdlib
# module object used by git_state for canonical repository discovery.
subprocess = SimpleNamespace(
    Popen=_subprocess.Popen,
    DEVNULL=_subprocess.DEVNULL,
    CREATE_NO_WINDOW=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
    DETACHED_PROCESS=getattr(_subprocess, "DETACHED_PROCESS", 0),
)


def ensure_observer(root: str | Path) -> None:
    """Ensure one live detached observer child exists for *root*."""

    repository = find_repo(root)
    configured_root = os.environ.get("AIGIT_STATE_DIR")
    state_root = Path(configured_root) if configured_root else Path.home() / ".aigit"
    state_path = state_root / repo_id(repository).removeprefix("sha256:")
    state_path.mkdir(parents=True, exist_ok=True)
    pid_path = state_path / "observer.pid"
    lock_path = state_path / "observer.pid.lock"

    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return

    try:
        os.close(lock_descriptor)
        pid = _read_pid(pid_path)
        if pid is not None and _pid_is_live(pid):
            return

        command = [
            sys.executable,
            "-m",
            "aigit.observer",
            "--repo",
            os.fspath(repository),
        ]
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if platform.system() == "Windows":
            options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        else:
            options["start_new_session"] = True

        child = subprocess.Popen(command, **options)
        _write_pid(pid_path, int(child.pid))
    finally:
        lock_path.unlink(missing_ok=True)


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    stripped = raw.strip()
    if not stripped.isascii() or not stripped.isdecimal():
        return None
    pid = int(stripped)
    return pid if pid > 0 else None


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _write_pid(path: Path, pid: int) -> None:
    if pid <= 0:
        raise ValueError("observer PID must be positive")
    temporary_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as output:
            output.write(f"{pid}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
