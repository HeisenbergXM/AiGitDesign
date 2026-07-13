from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import threading

import pytest

import aigit.cli as cli_module
import aigit.recorder as recorder_module
from aigit.recorder import InvalidRecorderInput, Recorder, RecorderStateError


PROMPT_HMAC_KEY = b"task-5-review-test-key" * 2


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Recorder Tests"],
        check=True,
    )
    (repo / "tracked.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "tracked.py"], check=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", "initial"],
        check=True,
    )
    return repo


@pytest.fixture
def recorder(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Recorder:
    monkeypatch.setenv("AIGIT_PROMPT_HMAC_KEY", PROMPT_HMAC_KEY.hex())
    return Recorder(repository, tmp_path / "state")


def _records(recorder: Recorder) -> list[dict[str, object]]:
    if not recorder.store.ledger_path.exists():
        return []
    return [
        json.loads(line)
        for line in recorder.store.ledger_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _event_records(recorder: Recorder, event_type: str) -> list[dict[str, object]]:
    return [record for record in _records(recorder) if record["event_type"] == event_type]


def _drop_trigger(recorder: Recorder, name: str) -> None:
    with sqlite3.connect(recorder.store.database_path) as connection:
        connection.execute(f"DROP TRIGGER {name}")


def _reject_queue_writes(recorder: Recorder) -> None:
    with sqlite3.connect(recorder.store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_queue_writes
            BEFORE INSERT ON upload_queue
            BEGIN
                SELECT RAISE(FAIL, 'injected queue failure');
            END
            """
        )


def _reject_active_clear(recorder: Recorder) -> None:
    with sqlite3.connect(recorder.store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_active_clear
            BEFORE DELETE ON active_transactions
            BEGIN
                SELECT RAISE(FAIL, 'injected clear failure');
            END
            """
        )


def test_begin_retry_repairs_half_queued_start_and_preserves_session_owner(
    recorder: Recorder,
) -> None:
    _reject_queue_writes(recorder)

    with pytest.raises(sqlite3.DatabaseError, match="injected queue failure"):
        recorder.begin("session-1")

    started = _event_records(recorder, "transaction_started")
    assert len(started) == 1
    transaction_id = str(started[0]["payload"]["transaction_id"])
    event_id = str(started[0]["event_id"])
    different_session = recorder.begin("session-2")
    assert different_session["error"] == "ACTIVE_TRANSACTION"

    _drop_trigger(recorder, "reject_queue_writes")
    repaired = recorder.begin("session-1")

    assert repaired["transaction_id"] == transaction_id
    assert repaired["event_ids"] == [event_id]
    assert len(_event_records(recorder, "transaction_started")) == 1
    with sqlite3.connect(recorder.store.database_path) as connection:
        queued = connection.execute(
            "SELECT COUNT(*) FROM upload_queue WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert queued == (1,)

    recorder.abort(transaction_id, "cleanup")
    fresh = recorder.begin("session-2")
    assert fresh["ok"] is True
    assert fresh["transaction_id"] != transaction_id


def test_end_retry_after_queue_failure_does_not_duplicate_contribution(
    recorder: Recorder, repository: Path
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")
    _reject_queue_writes(recorder)

    with pytest.raises(sqlite3.DatabaseError, match="injected queue failure"):
        recorder.end(transaction_id, "passed")

    _drop_trigger(recorder, "reject_queue_writes")
    retried = recorder.end(transaction_id, "passed")

    assert retried["counts"] == {"AI_SKILL": 1}
    assert len(_event_records(recorder, "patch_applied")) == 1
    assert len(_event_records(recorder, "transaction_finished")) == 1


def test_abort_retry_after_queue_failure_has_one_terminal_event(
    recorder: Recorder,
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    _reject_queue_writes(recorder)

    with pytest.raises(sqlite3.DatabaseError, match="injected queue failure"):
        recorder.abort(transaction_id, "not applied")

    _drop_trigger(recorder, "reject_queue_writes")
    retried = recorder.abort(transaction_id, "not applied")

    assert retried["counts"] == {}
    assert len(_event_records(recorder, "transaction_aborted")) == 1
    assert _event_records(recorder, "patch_applied") == []


def test_end_retry_after_clear_failure_reuses_terminal_events(
    recorder: Recorder, repository: Path
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")
    _reject_active_clear(recorder)

    with pytest.raises(sqlite3.DatabaseError, match="injected clear failure"):
        recorder.end(transaction_id, "passed")

    first_ids = {
        str(record["event_id"])
        for record in _event_records(recorder, "patch_applied")
        + _event_records(recorder, "transaction_finished")
    }
    _drop_trigger(recorder, "reject_active_clear")
    retried = recorder.end(transaction_id, "passed")

    assert set(retried["event_ids"]) == first_ids
    assert len(_event_records(recorder, "patch_applied")) == 1
    assert len(_event_records(recorder, "transaction_finished")) == 1


def test_abort_retry_after_clear_failure_reuses_terminal_event(
    recorder: Recorder,
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    _reject_active_clear(recorder)

    with pytest.raises(sqlite3.DatabaseError, match="injected clear failure"):
        recorder.abort(transaction_id, "not applied")

    event_id = str(_event_records(recorder, "transaction_aborted")[0]["event_id"])
    _drop_trigger(recorder, "reject_active_clear")
    retried = recorder.abort(transaction_id, "not applied")

    assert retried["event_ids"] == [event_id]
    assert len(_event_records(recorder, "transaction_aborted")) == 1


def test_repeated_end_returns_the_same_deterministic_result(
    recorder: Recorder, repository: Path
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")

    first = recorder.end(transaction_id, "passed")
    repeated = recorder.end(transaction_id, "passed")

    assert repeated["event_ids"] == first["event_ids"]
    assert repeated["counts"] == first["counts"]
    assert len(_event_records(recorder, "patch_applied")) == 1
    assert len(_event_records(recorder, "transaction_finished")) == 1


def test_repeated_abort_returns_the_same_deterministic_result(
    recorder: Recorder,
) -> None:
    transaction_id = str(recorder.begin("session-1")["transaction_id"])

    first = recorder.abort(transaction_id, "not applied")
    repeated = recorder.abort(transaction_id, "not applied")

    assert repeated["event_ids"] == first["event_ids"]
    assert len(_event_records(recorder, "transaction_aborted")) == 1


def test_abort_after_completed_end_reports_terminal_operation_mismatch(
    recorder: Recorder,
    repository: Path,
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")
    recorder.end(transaction_id, "passed")

    mismatch = recorder.abort(transaction_id, "too late")

    assert mismatch["ok"] is False
    assert mismatch["status"] == "unavailable"
    assert mismatch["error"] == "TERMINAL_OPERATION_MISMATCH"
    assert mismatch["winning_operation"] == "end:passed"
    assert mismatch.get("counts", {}) == {}


def test_end_after_completed_abort_reports_terminal_operation_mismatch(
    recorder: Recorder,
) -> None:
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    recorder.abort(transaction_id, "not applied")

    mismatch = recorder.end(transaction_id, "not-run")

    assert mismatch["ok"] is False
    assert mismatch["status"] == "unavailable"
    assert mismatch["error"] == "TERMINAL_OPERATION_MISMATCH"
    assert mismatch["winning_operation"] == "abort"
    assert mismatch.get("event_ids", []) == []


def test_racing_end_calls_persist_one_contribution_and_terminal(
    recorder: Recorder, repository: Path, tmp_path: Path
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")
    racers = [Recorder(repository, tmp_path / "state") for _ in range(6)]
    barrier = threading.Barrier(len(racers))

    def finish(candidate: Recorder) -> object:
        barrier.wait(timeout=2)
        try:
            return candidate.end(transaction_id, "passed")
        except (InvalidRecorderInput, RecorderStateError, sqlite3.DatabaseError):
            return None

    with ThreadPoolExecutor(max_workers=len(racers)) as executor:
        results = list(executor.map(finish, racers))

    assert any(result is not None for result in results)
    assert len(_event_records(recorder, "patch_applied")) == 1
    assert len(_event_records(recorder, "transaction_finished")) == 1


def test_racing_abort_calls_persist_one_terminal_event(
    recorder: Recorder, repository: Path, tmp_path: Path
) -> None:
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    racers = [Recorder(repository, tmp_path / "state") for _ in range(6)]
    barrier = threading.Barrier(len(racers))

    def abort(candidate: Recorder) -> object:
        barrier.wait(timeout=2)
        try:
            return candidate.abort(transaction_id, "not applied")
        except (InvalidRecorderInput, RecorderStateError, sqlite3.DatabaseError):
            return None

    with ThreadPoolExecutor(max_workers=len(racers)) as executor:
        results = list(executor.map(abort, racers))

    assert any(result is not None for result in results)
    assert len(_event_records(recorder, "transaction_aborted")) == 1
    assert _event_records(recorder, "patch_applied") == []


def test_racing_end_and_abort_choose_exactly_one_terminal_outcome(
    recorder: Recorder, repository: Path, tmp_path: Path
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")
    ending = Recorder(repository, tmp_path / "state")
    aborting = Recorder(repository, tmp_path / "state")
    barrier = threading.Barrier(2)

    def end() -> object:
        barrier.wait(timeout=2)
        try:
            return ending.end(transaction_id, "passed")
        except (InvalidRecorderInput, RecorderStateError, sqlite3.DatabaseError):
            return None

    def abort() -> object:
        barrier.wait(timeout=2)
        try:
            return aborting.abort(transaction_id, "not applied")
        except (InvalidRecorderInput, RecorderStateError, sqlite3.DatabaseError):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(end), executor.submit(abort)]
        assert any(future.result(timeout=3) is not None for future in results)

    finished = _event_records(recorder, "transaction_finished")
    aborted = _event_records(recorder, "transaction_aborted")
    patches = _event_records(recorder, "patch_applied")
    assert len(finished) + len(aborted) == 1
    assert not (finished and aborted)
    assert len(patches) == (1 if finished else 0)


def _line_hmac(line: str) -> str:
    return hmac.new(
        PROMPT_HMAC_KEY,
        b"line\0" + line.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _block_hmac(lines: list[str]) -> str:
    return hmac.new(
        PROMPT_HMAC_KEY,
        "\n".join(lines).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _valid_evidence(
    prompt_lines: list[str] | None = None,
    applied_lines: list[str] | None = None,
) -> dict[str, object]:
    prompt_lines = prompt_lines or []
    applied_lines = applied_lines or []
    return {
        "fingerprints": [_block_hmac(prompt_lines)] if prompt_lines else [],
        "counts": [len(prompt_lines)] if prompt_lines else [],
        "line_fingerprints": (
            [[_line_hmac(line) for line in prompt_lines]] if prompt_lines else []
        ),
        "normalized_line_count": len(prompt_lines),
        "normalized_token_count": sum(
            len(line.split()) for line in prompt_lines
        ),
        "applied_patch_fingerprints": (
            [_block_hmac(applied_lines)] if applied_lines else []
        ),
        "applied_patch_counts": [len(applied_lines)] if applied_lines else [],
        "applied_patch_line_fingerprints": (
            [[_line_hmac(line) for line in applied_lines]] if applied_lines else []
        ),
        "applied_patch_normalized_line_count": len(applied_lines),
        "applied_patch_normalized_token_count": sum(
            len(line.split()) for line in applied_lines
        ),
    }


def _begin_with_evidence(
    recorder: Recorder,
    session_id: str,
    *,
    applied_lines: list[str] | None = None,
    prompt_lines: list[str] | None = None,
) -> dict[str, object]:
    evidence_path = (
        recorder.store.state_path.parent / f"{session_id}-prompt-evidence.json"
    )
    evidence_path.write_text(
        json.dumps(_valid_evidence(prompt_lines, applied_lines)),
        encoding="utf-8",
    )
    return recorder.begin(session_id, evidence_path)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda value: [], id="non-object"),
        pytest.param(lambda value: {**value, "fingerprints": "not-a-list"}, id="fingerprints-shape"),
        pytest.param(lambda value: {**value, "fingerprints": [7]}, id="fingerprint-type"),
        pytest.param(lambda value: {**value, "fingerprints": ["0" * 63]}, id="fingerprint-sha256"),
        pytest.param(lambda value: {**value, "counts": ["1"]}, id="count-type"),
        pytest.param(lambda value: {**value, "counts": []}, id="count-cardinality"),
        pytest.param(lambda value: {**value, "line_fingerprints": "not-nested"}, id="line-hmac-shape"),
        pytest.param(lambda value: {**value, "line_fingerprints": [["xyz"]]}, id="line-hmac-sha256"),
        pytest.param(lambda value: {**value, "line_fingerprints": [[]]}, id="line-count-mismatch"),
        pytest.param(lambda value: {**value, "normalized_line_count": 2}, id="total-line-count-mismatch"),
        pytest.param(lambda value: {**value, "normalized_token_count": True}, id="token-count-type"),
        pytest.param(
            lambda value: {**value, "applied_patch_fingerprints": "not-a-list"},
            id="applied-fingerprints-shape",
        ),
        pytest.param(
            lambda value: {**value, "applied_patch_fingerprints": ["f" * 63]},
            id="applied-fingerprint-sha256",
        ),
        pytest.param(
            lambda value: {**value, "applied_patch_counts": [1]},
            id="applied-count-cardinality",
        ),
        pytest.param(
            lambda value: {**value, "applied_patch_line_fingerprints": [["xyz"]]},
            id="applied-line-hmac-sha256",
        ),
        pytest.param(
            lambda value: {
                **value,
                "applied_patch_normalized_line_count": 1,
            },
            id="applied-total-line-count-mismatch",
        ),
        pytest.param(
            lambda value: {
                **value,
                "applied_patch_normalized_token_count": True,
            },
            id="applied-token-count-type",
        ),
        pytest.param(lambda value: {**value, "raw_prompt": "secret source = 41"}, id="unknown-raw-field"),
    ],
)
def test_prompt_evidence_rejects_invalid_hmac_only_schema_and_deletes_file(
    recorder: Recorder, tmp_path: Path, mutate
) -> None:
    evidence_path = tmp_path / "prompt-evidence.json"
    evidence_path.write_text(
        json.dumps(mutate(_valid_evidence(["user_value = 7"]))),
        encoding="utf-8",
    )

    with pytest.raises(InvalidRecorderInput):
        recorder.begin("session-1", evidence_path)

    assert not evidence_path.exists()
    persisted = json.dumps(_records(recorder), sort_keys=True)
    assert "secret source = 41" not in persisted


def test_valid_prompt_hmac_evidence_classifies_matching_code_as_user_supplied(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIGIT_PROMPT_HMAC_KEY", PROMPT_HMAC_KEY.hex())
    recorder = Recorder(repository, tmp_path / "state")
    supplied_line = "user_value = 7"
    evidence_path = tmp_path / "prompt-evidence.json"
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                prompt_lines=[supplied_line],
                applied_lines=[supplied_line],
            )
        ),
        encoding="utf-8",
    )

    transaction_id = str(
        recorder.begin("session-1", evidence_path)["transaction_id"]
    )
    (repository / "tracked.py").write_text(supplied_line + "\n", encoding="utf-8")
    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"USER_SUPPLIED": 1}
    assert not evidence_path.exists()


def test_matching_applied_patch_line_is_classified_as_ai_skill(
    recorder: Recorder,
    repository: Path,
) -> None:
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["generated = 1"],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"AI_SKILL": 1}


def test_applied_repository_copy_can_still_be_classified_as_ai_reused(
    recorder: Recorder,
    repository: Path,
) -> None:
    copied_lines = ["first = 1", "second = 2", "third = 3"]
    (repository / "source.py").write_text(
        "\n".join(copied_lines) + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", repository, "add", "source.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "add source"],
        check=True,
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=copied_lines,
        )["transaction_id"]
    )
    (repository / "copy.py").write_text(
        "\n".join(copied_lines) + "\n",
        encoding="utf-8",
    )

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"AI_REUSED": 3}


def test_line_without_applied_patch_evidence_is_unknown(
    recorder: Recorder,
    repository: Path,
) -> None:
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    (repository / "tracked.py").write_text("unmatched = 1\n", encoding="utf-8")

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"UNKNOWN": 1}


def test_mixed_applied_patch_and_external_line_split_ai_from_unknown(
    recorder: Recorder,
    repository: Path,
) -> None:
    ai_line = "generated = 1"
    external_line = "external = 2"
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=[ai_line],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text(
        f"{ai_line}\n{external_line}\n",
        encoding="utf-8",
    )

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"AI_SKILL": 1, "UNKNOWN": 1}
    persisted = recorder.store.ledger_path.read_text(encoding="utf-8")
    assert ai_line not in persisted
    assert external_line not in persisted


def _invoke_main(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, object]]:
    exit_code = cli_module.main(list(arguments))
    output = capsys.readouterr().out
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(output)
    assert not output[end:].strip()
    assert isinstance(payload, dict)
    return exit_code, payload


@pytest.mark.parametrize("message", ["database is locked", "unable to open database file"])
def test_sqlite_unavailability_is_one_fail_open_json_object(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    def unavailable(_arguments) -> dict[str, object]:
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(cli_module, "_dispatch", unavailable)

    exit_code, payload = _invoke_main(capsys, "status", "--repo", ".", "--json")

    assert exit_code == 0
    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["error"] == "RECORDER_UNAVAILABLE"


def test_plain_sqlite_database_error_is_fail_open_unavailable(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_arguments) -> dict[str, object]:
        raise sqlite3.DatabaseError("generic SQLite storage failure")

    monkeypatch.setattr(cli_module, "_dispatch", unavailable)

    exit_code, payload = _invoke_main(capsys, "status", "--repo", ".", "--json")

    assert exit_code == 0
    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["error"] == "RECORDER_UNAVAILABLE"


def test_invalid_cli_arguments_remain_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, payload = _invoke_main(capsys, "end", "--repo", ".", "--validation", "passed")

    assert exit_code != 0
    assert payload["error"] == "INVALID_ARGUMENT"


def test_genuine_state_corruption_remains_nonzero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def corrupt(_arguments) -> dict[str, object]:
        raise RecorderStateError("hash chain is corrupt")

    monkeypatch.setattr(cli_module, "_dispatch", corrupt)

    exit_code, payload = _invoke_main(capsys, "status", "--repo", ".", "--json")

    assert exit_code != 0
    assert payload["error"] == "STATE_CORRUPTION"


def test_demonstrated_ledger_chain_corruption_remains_nonzero(
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("AIGIT_STATE_DIR", str(state_root))
    recorder = Recorder(repository, state_root)
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    recorder.abort(transaction_id, "not applied")
    records = _records(recorder)
    records[0]["payload"] = {"tampered": True}
    recorder.store.ledger_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    exit_code, payload = _invoke_main(
        capsys,
        "status",
        "--repo",
        str(repository),
        "--json",
    )

    assert exit_code != 0
    assert payload["error"] == "STATE_CORRUPTION"


def test_short_environment_hmac_key_is_fail_open_unavailable(
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIGIT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AIGIT_PROMPT_HMAC_KEY", b"short".hex())

    exit_code, payload = _invoke_main(
        capsys,
        "status",
        "--repo",
        str(repository),
        "--json",
    )

    assert exit_code == 0
    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["error"] == "RECORDER_UNAVAILABLE"


def test_git_capture_failure_is_fail_open_and_persists_no_ai_claim(
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("AIGIT_STATE_DIR", str(state_root))

    def capture_failure(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git", "status"])

    monkeypatch.setattr(recorder_module, "capture_snapshot", capture_failure)

    exit_code, payload = _invoke_main(
        capsys,
        "begin",
        "--repo",
        str(repository),
        "--session",
        "session-1",
        "--json",
    )

    assert exit_code == 0
    assert payload["status"] == "unavailable"
    recorder = Recorder(repository, state_root)
    assert _event_records(recorder, "patch_applied") == []


def test_git_diff_failure_is_fail_open_and_persists_no_ai_claim(
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("AIGIT_STATE_DIR", str(state_root))
    recorder = Recorder(repository, state_root)
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    (repository / "tracked.py").write_text("generated = 1\n", encoding="utf-8")

    def diff_failure(*_args, **_kwargs):
        raise RuntimeError("injected Git diff failure")

    monkeypatch.setattr(recorder_module, "diff_snapshots", diff_failure)

    exit_code, payload = _invoke_main(
        capsys,
        "end",
        "--repo",
        str(repository),
        "--transaction",
        transaction_id,
        "--validation",
        "passed",
        "--json",
    )

    assert exit_code == 0
    assert payload["status"] == "unavailable"
    assert _event_records(recorder, "patch_applied") == []


@pytest.mark.parametrize(
    ("failure_point", "error_code"),
    [("capture", "CAPTURE_FAILED"), ("diff", "DIFF_FAILED")],
)
def test_terminal_capture_or_diff_failure_closes_unknown_coverage_gap(
    recorder: Recorder,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    error_code: str,
) -> None:
    failed_line = "failed_interval = 1"
    later_line = "later_generated = 2"
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=[failed_line],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text(failed_line + "\n", encoding="utf-8")
    real_capture = recorder_module.capture_snapshot
    real_diff = recorder_module.diff_snapshots

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"injected {failure_point} failure")

    monkeypatch.setattr(
        recorder_module,
        "capture_snapshot" if failure_point == "capture" else "diff_snapshots",
        fail,
    )

    degraded = recorder.end(transaction_id, "failed")

    monkeypatch.setattr(recorder_module, "capture_snapshot", real_capture)
    monkeypatch.setattr(recorder_module, "diff_snapshots", real_diff)
    repeated = recorder.end(transaction_id, "failed")
    assert repeated == degraded
    assert degraded["status"] == "unavailable"
    assert degraded["error"] == error_code
    assert degraded["coverage"] == "UNKNOWN"
    recoveries = _event_records(recorder, "recovery_detected")
    assert len(recoveries) == 1
    assert recoveries[0]["event_id"] in degraded["event_ids"]
    assert recoveries[0]["payload"] == {
        "classification": "UNKNOWN",
        "reason_code": error_code,
        "transaction_id": transaction_id,
    }
    assert _event_records(recorder, "patch_applied") == []
    assert _event_records(recorder, "transaction_finished") == []

    next_transaction = str(
        _begin_with_evidence(
            recorder,
            "session-2",
            applied_lines=[later_line],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text(
        f"{failed_line}\n{later_line}\n",
        encoding="utf-8",
    )
    later = recorder.end(next_transaction, "passed")
    assert later["counts"] == {"AI_SKILL": 1}


def test_classifier_failure_downgrades_only_the_affected_span(
    recorder: Recorder,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / "good.py").write_text("", encoding="utf-8")
    (repository / "bad.py").write_text("", encoding="utf-8")
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-1",
            applied_lines=["good = 1", "bad = 1"],
        )["transaction_id"]
    )
    (repository / "good.py").write_text("good = 1\n", encoding="utf-8")
    (repository / "bad.py").write_text("bad = 1\n", encoding="utf-8")
    real_classify = recorder_module.classify_spans

    def fail_one_span(spans, context):
        materialized = list(spans)
        if any(span.path == "bad.py" for span in materialized):
            raise RuntimeError("injected span classifier failure")
        return real_classify(materialized, context)

    monkeypatch.setattr(recorder_module, "classify_spans", fail_one_span)

    ended = recorder.end(transaction_id, "not-run")

    assert ended["counts"] == {"AI_SKILL": 1, "UNKNOWN": 1}


def test_abort_reason_is_bounded_evidence_not_arbitrary_raw_text(
    recorder: Recorder,
) -> None:
    transaction_id = str(recorder.begin("session-1")["transaction_id"])
    sensitive_reason = "customer-token-7f91: do not store this arbitrary raw reason"

    recorder.abort(transaction_id, sensitive_reason)

    serialized = recorder.store.ledger_path.read_text(encoding="utf-8")
    assert sensitive_reason not in serialized
    payload = _event_records(recorder, "transaction_aborted")[0]["payload"]
    assert isinstance(payload, dict)
    assert "reason" not in payload
    if "reason_code" in payload:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(payload["reason_code"]))
    else:
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", str(payload["reason_hash"]))
