"""Stable JSON command-line interface for the local recorder."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from aigit.recorder import InvalidRecorderInput, Recorder, RecorderStateError


class _ArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="aigit")
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status")
    status.add_argument("--repo", required=True)
    status.add_argument("--json", action="store_true")

    begin = subcommands.add_parser("begin")
    begin.add_argument("--repo", required=True)
    begin.add_argument("--session", required=True)
    begin.add_argument("--prompt-evidence")
    begin.add_argument("--json", action="store_true")

    end = subcommands.add_parser("end")
    end.add_argument("--repo", required=True)
    end.add_argument("--transaction", required=True)
    end.add_argument(
        "--validation",
        required=True,
        choices=("passed", "failed", "not-run"),
    )
    end.add_argument("--json", action="store_true")

    abort = subcommands.add_parser("abort")
    abort.add_argument("--repo", required=True)
    abort.add_argument("--transaction", required=True)
    abort.add_argument("--reason", required=True)
    abort.add_argument("--json", action="store_true")

    link_commit = subcommands.add_parser("link-commit")
    link_commit.add_argument("--repo", required=True)
    link_commit.add_argument("--commit", required=True)
    link_commit.add_argument("--json", action="store_true")

    upload = subcommands.add_parser("upload")
    upload.add_argument("--repo", required=True)
    upload.add_argument("--once", action="store_true", required=True)
    upload.add_argument("--json", action="store_true")

    report = subcommands.add_parser("report")
    report.add_argument("--repo", required=True)
    report.add_argument("--rev", required=True)
    report.add_argument("--json", action="store_true")
    return parser


def _dispatch(arguments: argparse.Namespace) -> dict[str, object]:
    recorder = Recorder(arguments.repo)
    if arguments.command == "status":
        return recorder.status()
    if arguments.command == "begin":
        return recorder.begin(arguments.session, arguments.prompt_evidence)
    if arguments.command == "end":
        return recorder.end(arguments.transaction, arguments.validation)
    if arguments.command == "abort":
        return recorder.abort(arguments.transaction, arguments.reason)
    if arguments.command == "link-commit":
        return recorder.link_commit(arguments.commit)
    if arguments.command == "upload":
        return recorder.upload_once()
    if arguments.command == "report":
        return recorder.report(arguments.rev)
    raise _ArgumentError(f"unknown command: {arguments.command}")


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    evidence_path: Path | None = None
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "begin" and arguments.prompt_evidence is not None:
            evidence_path = Path(arguments.prompt_evidence)
        payload = _dispatch(arguments)
    except (_ArgumentError, InvalidRecorderInput) as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": "INVALID_ARGUMENT",
            "message": str(exc),
        }
        exit_code = 2
    except RecorderStateError as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": "STATE_CORRUPTION",
            "message": str(exc),
        }
        exit_code = 3
    except sqlite3.OperationalError as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": "RECORDER_UNAVAILABLE",
            "message": str(exc),
        }
        exit_code = 0
    except sqlite3.DatabaseError as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": "STATE_CORRUPTION",
            "message": str(exc),
        }
        exit_code = 3
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        payload = {
            "ok": False,
            "status": "unavailable",
            "error": "RECORDER_UNAVAILABLE",
            "message": str(exc),
        }
        exit_code = 0
    else:
        exit_code = 3 if payload.get("error") == "STATE_CORRUPTION" else 0
    finally:
        if evidence_path is not None:
            try:
                evidence_path.unlink(missing_ok=True)
            except OSError:
                pass
    _emit(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
