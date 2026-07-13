from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", repository], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repository, "config", "user.name", "Recorder Tests"],
        check=True,
    )
    (repository / "dirty.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "dirty.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "initial"],
        check=True,
    )
    return repository


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AIGIT_STATE_DIR"] = str(tmp_path / "aigit-state")
    source_root = str(PROJECT_ROOT / "src")
    prior_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source_root
        if not prior_pythonpath
        else os.pathsep.join((source_root, prior_pythonpath))
    )
    return env


def invoke(
    cli_env: dict[str, str],
    *args: object,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = subprocess.run(
        [sys.executable, "-m", "aigit.cli", *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
        env=cli_env,
        timeout=2,
    )
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(result.stdout)
    assert not result.stdout[end:].strip(), result.stdout
    assert isinstance(payload, dict)
    return result, payload


def git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", repo, "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commit_all(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_cli_records_only_net_applied_patch(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    (repo / "dirty.py").write_text("manual = 1\n", encoding="utf-8")

    begin_result, begun = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-1", "--json"
    )
    assert begin_result.returncode == 0
    (repo / "dirty.py").write_text("manual = 1\nai = 2\n", encoding="utf-8")

    end_result, ended = invoke(
        cli_env,
        "end",
        "--repo",
        repo,
        "--transaction",
        begun["transaction_id"],
        "--validation",
        "passed",
        "--json",
    )

    assert end_result.returncode == 0
    assert ended["status"] in {"recorded", "local-only"}
    assert ended["counts"] == {"AI_SKILL": 1}


def test_second_begin_reports_active_transaction_without_touching_worktree(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    (repo / "dirty.py").write_text("manual = 1\n", encoding="utf-8")
    first_result, first = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-1", "--json"
    )
    assert first_result.returncode == 0
    content_before = (repo / "dirty.py").read_bytes()
    status_before = git_status(repo)

    second_result, second = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-2", "--json"
    )

    assert second_result.returncode == 0
    assert second["ok"] is False
    assert second["error"] == "ACTIVE_TRANSACTION"
    assert (repo / "dirty.py").read_bytes() == content_before
    assert git_status(repo) == status_before
    assert first["transaction_id"]


def test_abort_clears_transaction_without_recording_its_patch(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    first_result, first = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-1", "--json"
    )
    assert first_result.returncode == 0
    (repo / "dirty.py").write_text("aborted = 1\n", encoding="utf-8")

    abort_result, aborted = invoke(
        cli_env,
        "abort",
        "--repo",
        repo,
        "--transaction",
        first["transaction_id"],
        "--reason",
        "patch not applied",
        "--json",
    )
    assert abort_result.returncode == 0
    assert aborted["ok"] is True

    second_result, second = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-2", "--json"
    )
    assert second_result.returncode == 0
    (repo / "dirty.py").write_text(
        "aborted = 1\nrecorded = 2\n", encoding="utf-8"
    )
    end_result, ended = invoke(
        cli_env,
        "end",
        "--repo",
        repo,
        "--transaction",
        second["transaction_id"],
        "--validation",
        "not-run",
        "--json",
    )

    assert end_result.returncode == 0
    assert ended["counts"] == {"AI_SKILL": 1}


def test_begin_deletes_valid_prompt_evidence(
    repo: Path,
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "prompt-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "fingerprints": [],
                "counts": [],
                "line_fingerprints": [],
                "normalized_line_count": 0,
                "normalized_token_count": 0,
            }
        ),
        encoding="utf-8",
    )

    result, payload = invoke(
        cli_env,
        "begin",
        "--repo",
        repo,
        "--session",
        "s-1",
        "--prompt-evidence",
        evidence,
        "--json",
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert not evidence.exists()


def test_begin_deletes_invalid_prompt_evidence(
    repo: Path,
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "prompt-evidence.json"
    evidence.write_text("{not valid json", encoding="utf-8")

    result, payload = invoke(
        cli_env,
        "begin",
        "--repo",
        repo,
        "--session",
        "s-1",
        "--prompt-evidence",
        evidence,
        "--json",
    )

    assert result.returncode != 0
    assert payload["ok"] is False
    assert not evidence.exists()


def test_begin_deletes_prompt_evidence_when_session_is_empty(
    repo: Path,
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "prompt-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "fingerprints": [],
                "counts": [],
                "line_fingerprints": [],
                "normalized_line_count": 0,
                "normalized_token_count": 0,
            }
        ),
        encoding="utf-8",
    )

    result, payload = invoke(
        cli_env,
        "begin",
        "--repo",
        repo,
        "--session",
        "",
        "--prompt-evidence",
        evidence,
        "--json",
    )

    assert result.returncode != 0
    assert payload["error"] == "INVALID_ARGUMENT"
    assert not evidence.exists()


def test_begin_deletes_prompt_evidence_when_recorder_initialization_fails(
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "prompt-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "fingerprints": [],
                "counts": [],
                "line_fingerprints": [],
                "normalized_line_count": 0,
                "normalized_token_count": 0,
            }
        ),
        encoding="utf-8",
    )
    not_a_repository = tmp_path / "not-a-repository"
    not_a_repository.mkdir()

    result, payload = invoke(
        cli_env,
        "begin",
        "--repo",
        not_a_repository,
        "--session",
        "s-1",
        "--prompt-evidence",
        evidence,
        "--json",
    )

    assert result.returncode == 0
    assert payload["status"] == "unavailable"
    assert not evidence.exists()


def test_recorder_constructor_lock_is_fail_open_in_under_500_ms(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    initialized, _ = invoke(cli_env, "status", "--repo", repo, "--json")
    assert initialized.returncode == 0
    state_root = Path(cli_env["AIGIT_STATE_DIR"])
    database_path = next(state_root.rglob("state.sqlite3"))
    blocker = sqlite3.connect(database_path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "aigit.cli",
                "status",
                "--repo",
                str(repo),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=cli_env,
            timeout=2,
        )
        elapsed = time.perf_counter() - started
    finally:
        blocker.rollback()
        blocker.close()

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["status"] == "unavailable"
    assert elapsed < 0.5


def test_report_labels_counts_as_lifetime_ledger_not_revision_stock(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    initial_revision = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    begin_result, begun = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-1", "--json"
    )
    assert begin_result.returncode == 0
    (repo / "dirty.py").write_text("generated_after_revision = 1\n", encoding="utf-8")
    end_result, ended = invoke(
        cli_env,
        "end",
        "--repo",
        repo,
        "--transaction",
        begun["transaction_id"],
        "--validation",
        "passed",
        "--json",
    )
    assert end_result.returncode == 0
    assert ended["counts"] == {"AI_SKILL": 1}

    report_result, report = invoke(
        cli_env,
        "report",
        "--repo",
        repo,
        "--rev",
        initial_revision,
        "--json",
    )

    assert report_result.returncode == 0
    assert report["scope"] == "lifetime_local_ledger"
    assert report["counts"] == {"AI_SKILL": 1}
    assert report["revision_stock_status"] == "unavailable"
    assert "revision_stock" not in report


def test_auxiliary_commands_emit_json_and_linking_preserves_counts(
    repo: Path,
    cli_env: dict[str, str],
) -> None:
    status_result, status = invoke(cli_env, "status", "--repo", repo, "--json")
    assert status_result.returncode == 0
    assert {"ok", "status"} <= status.keys()

    begin_result, begun = invoke(
        cli_env, "begin", "--repo", repo, "--session", "s-1", "--json"
    )
    assert begin_result.returncode == 0
    (repo / "dirty.py").write_text("ai = 1\n", encoding="utf-8")
    end_result, ended = invoke(
        cli_env,
        "end",
        "--repo",
        repo,
        "--transaction",
        begun["transaction_id"],
        "--validation",
        "passed",
        "--json",
    )
    assert end_result.returncode == 0
    assert ended["counts"] == {"AI_SKILL": 1}
    commit = commit_all(repo, "recorded change")

    report_result, before_link = invoke(
        cli_env, "report", "--repo", repo, "--rev", "HEAD", "--json"
    )
    assert report_result.returncode == 0
    assert {"ok", "status", "counts"} <= before_link.keys()

    link_result, linked = invoke(
        cli_env, "link-commit", "--repo", repo, "--commit", commit, "--json"
    )
    assert link_result.returncode == 0
    assert {"ok", "status"} <= linked.keys()

    after_result, after_link = invoke(
        cli_env, "report", "--repo", repo, "--rev", "HEAD", "--json"
    )
    assert after_result.returncode == 0
    assert after_link["counts"] == before_link["counts"]

    started = time.perf_counter()
    upload_result, upload = invoke(
        cli_env, "upload", "--repo", repo, "--once", "--json"
    )
    elapsed = time.perf_counter() - started

    assert upload_result.returncode == 0
    assert upload["ok"] is True
    assert upload["status"] == "local-only"
    assert elapsed < 0.5
