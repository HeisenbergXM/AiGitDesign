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
from aigit.domain import Classification, GitSnapshot, PatchSpan
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


def _expire_terminal_claim_lease(recorder: Recorder, transaction_id: str) -> None:
    with sqlite3.connect(recorder.store.database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE active_transactions
            SET terminal_claim_expires_at = '1970-01-01T00:00:00+00:00'
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        )
    assert cursor.rowcount == 1


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


def test_same_end_retry_waits_for_live_fallback_lease_then_takes_over_after_expiry(
    recorder: Recorder,
    repository: Path,
) -> None:
    transaction_id = str(recorder.begin("session-fallback-crash")["transaction_id"])
    (repository / "tracked.py").write_text("unobserved_interval = 1\n", encoding="utf-8")
    operation = "end:passed"

    claimed = recorder._claim_terminal(
        transaction_id,
        operation,
        persist_fallback=True,
    )
    assert not isinstance(claimed, dict)
    with sqlite3.connect(recorder.store.database_path) as connection:
        stored = connection.execute(
            """
            SELECT terminal_plan_json, terminal_plan_kind
            FROM active_transactions
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
    assert stored is not None
    assert stored[1] == "fallback"
    planned = json.loads(stored[0])
    planned_event_id = planned["events"][0]["event_id"]

    contended = recorder.end(transaction_id, "passed")

    assert contended["error"] == "TERMINAL_OPERATION_IN_PROGRESS"
    assert _event_records(recorder, "recovery_detected") == []
    _expire_terminal_claim_lease(recorder, transaction_id)
    retried = recorder.end(transaction_id, "passed")

    assert retried["error"] == "CAPTURE_FAILED"
    assert retried["coverage"] == "UNKNOWN"
    assert retried["event_ids"] == [planned_event_id]
    recoveries = _event_records(recorder, "recovery_detected")
    assert [record["event_id"] for record in recoveries] == [planned_event_id]
    fresh = recorder.begin("session-after-fallback-crash")
    assert fresh["ok"] is True


@pytest.mark.parametrize("failure_point", ["ledger", "queue"])
def test_same_end_retry_completes_fallback_after_fallback_write_failure(
    recorder: Recorder,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    transaction_id = str(recorder.begin("session-fallback-write")["transaction_id"])
    (repository / "tracked.py").write_text("failed_interval = 1\n", encoding="utf-8")
    real_capture = recorder_module.capture_snapshot
    real_append = recorder.store.append

    def fail_capture(*_args, **_kwargs):
        raise RuntimeError("injected capture failure")

    def fail_recovery_append(event):
        if event.event_type == "recovery_detected":
            raise OSError("injected recovery ledger failure")
        return real_append(event)

    monkeypatch.setattr(recorder_module, "capture_snapshot", fail_capture)
    if failure_point == "ledger":
        monkeypatch.setattr(recorder.store, "append", fail_recovery_append)
        expected_error: type[BaseException] = OSError
        expected_message = "injected recovery ledger failure"
    else:
        with sqlite3.connect(recorder.store.database_path) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_recovery_queue_write
                BEFORE INSERT ON upload_queue
                WHEN instr(
                    CAST(NEW.event_json AS TEXT),
                    '"event_type":"recovery_detected"'
                ) > 0
                BEGIN
                    SELECT RAISE(FAIL, 'injected recovery queue failure');
                END
                """
            )
        expected_error = sqlite3.DatabaseError
        expected_message = "injected recovery queue failure"

    with pytest.raises(expected_error, match=expected_message):
        recorder.end(transaction_id, "failed")

    with sqlite3.connect(recorder.store.database_path) as connection:
        stored = connection.execute(
            """
            SELECT terminal_plan_json, terminal_plan_kind
            FROM active_transactions
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
    assert stored is not None
    assert stored[1] == "fallback"
    planned = json.loads(stored[0])
    planned_event_id = planned["events"][0]["event_id"]

    monkeypatch.setattr(recorder.store, "append", real_append)
    if failure_point == "queue":
        _drop_trigger(recorder, "reject_recovery_queue_write")
    monkeypatch.setattr(recorder_module, "capture_snapshot", real_capture)
    recoveries_before_retry = _event_records(recorder, "recovery_detected")
    assert len(recoveries_before_retry) == (1 if failure_point == "queue" else 0)

    contended = recorder.end(transaction_id, "failed")

    assert contended["error"] == "TERMINAL_OPERATION_IN_PROGRESS"
    assert _event_records(recorder, "recovery_detected") == recoveries_before_retry
    _expire_terminal_claim_lease(recorder, transaction_id)
    retried = recorder.end(transaction_id, "failed")

    assert retried["error"] == "CAPTURE_FAILED"
    assert retried["coverage"] == "UNKNOWN"
    assert retried["event_ids"] == [planned_event_id]
    recoveries = _event_records(recorder, "recovery_detected")
    assert [record["event_id"] for record in recoveries] == [planned_event_id]
    fresh = recorder.begin(f"session-after-{failure_point}-failure")
    assert fresh["ok"] is True


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

    exact = [
        result
        for result in results
        if isinstance(result, dict) and result.get("counts") == {"AI_SKILL": 1}
    ]
    assert exact
    assert all(
        result is None
        or (
            isinstance(result, dict)
            and (
                result.get("counts") == {"AI_SKILL": 1}
                or result.get("error") == "TERMINAL_OPERATION_IN_PROGRESS"
            )
        )
        for result in results
    )
    assert len(_event_records(recorder, "patch_applied")) == 1
    assert len(_event_records(recorder, "transaction_finished")) == 1
    assert _event_records(recorder, "recovery_detected") == []


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


def _path_hmac(path: str) -> str:
    normalized = path.replace("\\", "/")
    return hmac.new(
        PROMPT_HMAC_KEY,
        b"path\0" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _block_hmac(lines: list[str]) -> str:
    return hmac.new(
        PROMPT_HMAC_KEY,
        "\n".join(lines).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _proposed_hunk(
    *,
    action: str,
    path: str,
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
    old_lines: list[str] | None = None,
    new_lines: list[str] | None = None,
    old_path: str | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "path_hmac": _path_hmac(path),
        "old_path_hmac": _path_hmac(old_path) if old_path is not None else None,
        "old_start": old_start,
        "old_end": old_end,
        "new_start": new_start,
        "new_end": new_end,
        "old_line_fingerprints": [
            _line_hmac(line) for line in (old_lines or [])
        ],
        "new_line_fingerprints": [
            _line_hmac(line) for line in (new_lines or [])
        ],
    }


def _valid_evidence(
    prompt_lines: list[str] | None = None,
    applied_lines: list[str] | None = None,
    *,
    proposed_hunks: list[dict[str, object]] | None = None,
    applied_path: str = "tracked.py",
    applied_new_start: int = 0,
) -> dict[str, object]:
    prompt_lines = prompt_lines or []
    applied_lines = applied_lines or []
    if proposed_hunks is None and applied_lines:
        proposed_hunks = [
            _proposed_hunk(
                action="ADDED",
                path=applied_path,
                old_start=applied_new_start,
                old_end=applied_new_start,
                new_start=applied_new_start,
                new_end=applied_new_start + len(applied_lines),
                new_lines=applied_lines,
            )
        ]
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
        "proposed_patch_hunks": proposed_hunks or [],
    }


def _begin_with_evidence(
    recorder: Recorder,
    session_id: str,
    *,
    applied_lines: list[str] | None = None,
    prompt_lines: list[str] | None = None,
    proposed_hunks: list[dict[str, object]] | None = None,
    applied_path: str = "tracked.py",
    applied_new_start: int = 0,
) -> dict[str, object]:
    evidence_path = (
        recorder.store.state_path.parent / f"{session_id}-prompt-evidence.json"
    )
    evidence_path.write_text(
        json.dumps(
            _valid_evidence(
                prompt_lines,
                applied_lines,
                proposed_hunks=proposed_hunks,
                applied_path=applied_path,
                applied_new_start=applied_new_start,
            )
        ),
        encoding="utf-8",
    )
    return recorder.begin(session_id, evidence_path)


def _replace_first_hunk_field(
    value: dict[str, object],
    field: str,
    replacement: object,
) -> dict[str, object]:
    hunks = [dict(hunk) for hunk in value["proposed_patch_hunks"]]
    hunks[0][field] = replacement
    return {**value, "proposed_patch_hunks": hunks}


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
            lambda value: {**value, "proposed_patch_hunks": "not-a-list"},
            id="proposed-hunks-shape",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(value, "action", "COPIED"),
            id="hunk-action-enum",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(value, "path_hmac", "f" * 63),
            id="hunk-path-hmac",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(value, "old_path_hmac", "raw.py"),
            id="hunk-old-path-hmac",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(value, "new_start", True),
            id="hunk-coordinate-type",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(value, "new_end", 2),
            id="hunk-coordinate-line-count",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(
                value,
                "new_line_fingerprints",
                ["xyz"],
            ),
            id="hunk-new-line-hmac",
        ),
        pytest.param(
            lambda value: _replace_first_hunk_field(
                value,
                "old_line_fingerprints",
                [_line_hmac("unexpected old line")],
            ),
            id="hunk-old-range-line-count",
        ),
        pytest.param(lambda value: {**value, "raw_prompt": "secret source = 41"}, id="unknown-raw-field"),
    ],
)
def test_prompt_evidence_rejects_invalid_hmac_only_schema_and_deletes_file(
    recorder: Recorder, tmp_path: Path, mutate
) -> None:
    evidence_path = tmp_path / "prompt-evidence.json"
    evidence_path.write_text(
        json.dumps(
            mutate(
                _valid_evidence(
                    ["user_value = 7"],
                    ["generated_value = 8"],
                )
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(InvalidRecorderInput):
        recorder.begin("session-1", evidence_path)

    assert not evidence_path.exists()
    persisted = json.dumps(_records(recorder), sort_keys=True)
    assert "secret source = 41" not in persisted


@pytest.mark.parametrize(
    "hunk",
    [
        pytest.param(
            _proposed_hunk(
                action="ADDED",
                path="added.py",
                old_start=0,
                old_end=0,
                new_start=0,
                new_end=1,
                new_lines=["added_value = 1"],
            ),
            id="added",
        ),
        pytest.param(
            _proposed_hunk(
                action="REPLACED",
                path="replaced.py",
                old_start=2,
                old_end=3,
                new_start=2,
                new_end=3,
                old_lines=["old_value = 1"],
                new_lines=["new_value = 2"],
            ),
            id="replaced",
        ),
        pytest.param(
            _proposed_hunk(
                action="DELETED",
                path="deleted.py",
                old_start=4,
                old_end=5,
                new_start=4,
                new_end=4,
                old_lines=["deleted_value = 3"],
            ),
            id="deleted",
        ),
        pytest.param(
            _proposed_hunk(
                action="MOVED",
                path="destination.py",
                old_path="source.py",
                old_start=6,
                old_end=7,
                new_start=1,
                new_end=2,
                old_lines=["moved_value = 4"],
                new_lines=["moved_value = 4"],
            ),
            id="moved",
        ),
        pytest.param(
            _proposed_hunk(
                action="FORMATTED",
                path="formatted.py",
                old_start=8,
                old_end=9,
                new_start=8,
                new_end=9,
                old_lines=["formatted=5"],
                new_lines=["formatted = 5"],
            ),
            id="formatted",
        ),
    ],
)
def test_strict_proposed_hunk_schema_supports_every_action_without_raw_source(
    recorder: Recorder,
    tmp_path: Path,
    hunk: dict[str, object],
) -> None:
    evidence_path = tmp_path / "hunk-evidence.json"
    evidence = _valid_evidence(proposed_hunks=[hunk])
    encoded = json.dumps(evidence, sort_keys=True)
    evidence_path.write_text(encoded, encoding="utf-8")

    begun = recorder.begin("session-schema", evidence_path)

    assert begun["ok"] is True
    assert not evidence_path.exists()
    with sqlite3.connect(recorder.store.database_path) as connection:
        stored = connection.execute(
            "SELECT prompt_evidence_json FROM active_transactions"
        ).fetchone()
    assert stored is not None
    assert json.loads(stored[0]) == evidence
    raw_markers = (
        "added.py",
        "replaced.py",
        "deleted.py",
        "source.py",
        "destination.py",
        "formatted.py",
        "added_value = 1",
        "old_value = 1",
        "new_value = 2",
        "deleted_value = 3",
        "moved_value = 4",
        "formatted=5",
        "formatted = 5",
    )
    assert all(marker not in stored[0] for marker in raw_markers)


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
            applied_path="copy.py",
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


def test_one_evidenced_addition_authorizes_only_its_bound_path_and_occurrence(
    recorder: Recorder,
    repository: Path,
) -> None:
    repeated_line = "same_generated_value = 7"
    hunk = _proposed_hunk(
        action="ADDED",
        path="expected.py",
        old_start=0,
        old_end=0,
        new_start=0,
        new_end=1,
        new_lines=[repeated_line],
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-bound",
            proposed_hunks=[hunk],
        )["transaction_id"]
    )
    (repository / "expected.py").write_text(
        f"{repeated_line}\n{repeated_line}\n",
        encoding="utf-8",
    )
    (repository / "external.py").write_text(repeated_line + "\n", encoding="utf-8")

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"AI_SKILL": 1, "UNKNOWN": 2}
    patch_payloads = [record["payload"] for record in _event_records(recorder, "patch_applied")]
    expected = next(payload for payload in patch_payloads if payload["path"] == "expected.py")
    external = next(payload for payload in patch_payloads if payload["path"] == "external.py")
    assert [span["classification"] for span in expected["spans"]] == [
        "AI_SKILL",
        "UNKNOWN",
    ]
    assert external["counts"] == {"UNKNOWN": 1}
    serialized = recorder.store.ledger_path.read_text(encoding="utf-8")
    assert repeated_line not in serialized


def test_one_proposed_hunk_is_consumed_globally_at_most_once(
    recorder: Recorder,
) -> None:
    line = "one_authorized_occurrence = 1"
    span = PatchSpan.added("same.py", (line,))
    metadata = _valid_evidence(
        proposed_hunks=[
            _proposed_hunk(
                action="ADDED",
                path="same.py",
                old_start=0,
                old_end=0,
                new_start=0,
                new_end=1,
                new_lines=[line],
            )
        ]
    )

    classified = recorder._classify(
        (span, span),
        GitSnapshot("", "", "", {}),
        metadata,
    )

    assert [item.classification for item in classified] == [
        Classification.AI_SKILL,
        Classification.UNKNOWN,
    ]


def test_overlapping_added_hunks_authorize_only_uniquely_covered_lines_once(
    recorder: Recorder,
) -> None:
    lines = (
        "unique_to_first = 1",
        "ambiguous_overlap = 2",
        "unique_to_second = 3",
        "separate_unique_hunk = 4",
    )
    span = PatchSpan.added("overlap.py", lines)
    metadata = _valid_evidence(
        proposed_hunks=[
            _proposed_hunk(
                action="ADDED",
                path="overlap.py",
                old_start=0,
                old_end=0,
                new_start=0,
                new_end=2,
                new_lines=list(lines[0:2]),
            ),
            _proposed_hunk(
                action="ADDED",
                path="overlap.py",
                old_start=0,
                old_end=0,
                new_start=1,
                new_end=3,
                new_lines=list(lines[1:3]),
            ),
            _proposed_hunk(
                action="ADDED",
                path="overlap.py",
                old_start=0,
                old_end=0,
                new_start=3,
                new_end=4,
                new_lines=[lines[3]],
            ),
        ]
    )

    classified = recorder._classify(
        (span, span),
        GitSnapshot("", "", "", {}),
        metadata,
    )

    observed = [
        (line, item.classification)
        for item in classified
        for line in item.new_lines
    ]
    assert observed == [
        (lines[0], Classification.AI_SKILL),
        (lines[1], Classification.UNKNOWN),
        (lines[2], Classification.AI_SKILL),
        (lines[3], Classification.AI_SKILL),
        *((line, Classification.UNKNOWN) for line in lines),
    ]


@pytest.mark.parametrize(
    "mutate_hunk",
    [
        pytest.param(
            lambda hunk: {**hunk, "path_hmac": _path_hmac("wrong.py")},
            id="wrong-path",
        ),
        pytest.param(
            lambda hunk: {**hunk, "action": "FORMATTED"},
            id="wrong-action",
        ),
        pytest.param(
            lambda hunk: {**hunk, "old_start": 1, "old_end": 2},
            id="wrong-old-range",
        ),
        pytest.param(
            lambda hunk: {**hunk, "new_start": 1, "new_end": 2},
            id="wrong-new-range",
        ),
        pytest.param(
            lambda hunk: {
                **hunk,
                "old_line_fingerprints": [_line_hmac("wrong_old = 1")],
            },
            id="wrong-old-lines",
        ),
        pytest.param(
            lambda hunk: {
                **hunk,
                "new_line_fingerprints": [_line_hmac("wrong_new = 2")],
            },
            id="wrong-new-lines",
        ),
    ],
)
def test_replacement_requires_exact_hunk_binding_or_becomes_unknown(
    recorder: Recorder,
    repository: Path,
    mutate_hunk,
) -> None:
    old_line = "before_value = 1"
    new_line = "after_value = 2"
    (repository / "tracked.py").write_text(old_line + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "tracked.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "replacement base"],
        check=True,
    )
    exact = _proposed_hunk(
        action="REPLACED",
        path="tracked.py",
        old_start=0,
        old_end=1,
        new_start=0,
        new_end=1,
        old_lines=[old_line],
        new_lines=[new_line],
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-mismatch",
            proposed_hunks=[mutate_hunk(exact)],
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text(new_line + "\n", encoding="utf-8")

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"UNKNOWN": 1}


def test_exact_evidenced_deletion_records_ai_action_without_new_stock(
    recorder: Recorder,
    repository: Path,
) -> None:
    deleted_line = "delete_me = 1"
    (repository / "tracked.py").write_text(deleted_line + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "tracked.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "deletion base"],
        check=True,
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-delete",
            proposed_hunks=[
                _proposed_hunk(
                    action="DELETED",
                    path="tracked.py",
                    old_start=0,
                    old_end=1,
                    new_start=0,
                    new_end=0,
                    old_lines=[deleted_line],
                )
            ],
        )["transaction_id"]
    )
    (repository / "tracked.py").unlink()

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {}
    payload = _event_records(recorder, "patch_applied")[0]["payload"]
    assert payload["spans"] == [
        {
            "action": "DELETED",
            "classification": "LEGACY_UNKNOWN",
            "confidence": 1.0,
            "edit_actor": "AI",
            "old_start": 0,
            "old_end": 1,
            "new_start": 0,
            "new_end": 0,
        }
    ]
    assert deleted_line not in recorder.store.ledger_path.read_text(encoding="utf-8")


def test_exact_evidenced_move_records_ai_action_without_inflating_stock(
    recorder: Recorder,
    repository: Path,
) -> None:
    moved_lines = ["first_moved = 1", "second_moved = 2"]
    source = repository / "source.py"
    destination = repository / "destination.py"
    source.write_text("\n".join(moved_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "source.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "move base"],
        check=True,
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-move",
            proposed_hunks=[
                _proposed_hunk(
                    action="MOVED",
                    path="destination.py",
                    old_path="source.py",
                    old_start=0,
                    old_end=2,
                    new_start=0,
                    new_end=2,
                    old_lines=moved_lines,
                    new_lines=moved_lines,
                )
            ],
        )["transaction_id"]
    )
    source.replace(destination)

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {}
    spans = [
        span
        for record in _event_records(recorder, "patch_applied")
        for span in record["payload"]["spans"]
    ]
    assert spans == [
        {
            "action": "MOVED",
            "classification": "LEGACY_UNKNOWN",
            "confidence": 1.0,
            "edit_actor": "AI",
            "old_start": 0,
            "old_end": 2,
            "new_start": 0,
            "new_end": 2,
            "old_path_hmac": _path_hmac("source.py"),
        }
    ]
    persisted = recorder.store.ledger_path.read_text(encoding="utf-8")
    assert all(line not in persisted for line in moved_lines)


@pytest.mark.parametrize(
    "destination_lines",
    [
        pytest.param(
            ["changed_first = 10", "changed_second = 20"],
            id="different-content",
        ),
        pytest.param(["shorter_destination = 10"], id="different-length"),
    ],
)
def test_non_exact_move_evidence_remains_separate_unknown_changes(
    recorder: Recorder,
    repository: Path,
    destination_lines: list[str],
) -> None:
    source_lines = ["source_first = 1", "source_second = 2"]
    source = repository / "source.py"
    destination = repository / "destination.py"
    source.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", repository, "add", "source.py"], check=True)
    subprocess.run(
        ["git", "-C", repository, "commit", "-q", "-m", "non-exact move base"],
        check=True,
    )
    transaction_id = str(
        _begin_with_evidence(
            recorder,
            "session-non-exact-move",
            proposed_hunks=[
                _proposed_hunk(
                    action="MOVED",
                    path="destination.py",
                    old_path="source.py",
                    old_start=0,
                    old_end=len(source_lines),
                    new_start=0,
                    new_end=len(destination_lines),
                    old_lines=source_lines,
                    new_lines=destination_lines,
                )
            ],
        )["transaction_id"]
    )
    source.unlink()
    destination.write_text(
        "\n".join(destination_lines) + "\n",
        encoding="utf-8",
    )

    ended = recorder.end(transaction_id, "passed")

    assert ended["counts"] == {"UNKNOWN": len(destination_lines)}
    spans = [
        span
        for record in _event_records(recorder, "patch_applied")
        for span in record["payload"]["spans"]
    ]
    assert {span["action"] for span in spans} == {"ADDED", "DELETED"}
    assert all(span["classification"] == "UNKNOWN" for span in spans)
    assert all(span["confidence"] == 0.0 for span in spans)
    assert all("edit_actor" not in span for span in spans)
    assert all("old_path_hmac" not in span for span in spans)


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
            applied_new_start=1,
        )["transaction_id"]
    )
    (repository / "tracked.py").write_text(
        f"{failed_line}\n{later_line}\n",
        encoding="utf-8",
    )
    later = recorder.end(next_transaction, "passed")
    assert later["counts"] == {"AI_SKILL": 1}


def test_degradation_plan_store_failure_cannot_absorb_later_edits(
    recorder: Recorder,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction_id = str(recorder.begin("session-plan-failure")["transaction_id"])
    first_line = "failed_capture_interval = 1"
    later_line = "must_not_be_absorbed = 2"
    (repository / "tracked.py").write_text(first_line + "\n", encoding="utf-8")
    real_capture = recorder_module.capture_snapshot

    def fail_capture(*_args, **_kwargs):
        raise RuntimeError("injected capture failure before degradation plan")

    monkeypatch.setattr(recorder_module, "capture_snapshot", fail_capture)
    with sqlite3.connect(recorder.store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_degradation_plan
            BEFORE UPDATE OF terminal_plan_json ON active_transactions
            WHEN OLD.terminal_state = 'claimed'
                 AND NEW.terminal_plan_json IS NOT NULL
            BEGIN
                SELECT RAISE(FAIL, 'injected degradation plan store failure');
            END
            """
        )

    degraded = recorder.end(transaction_id, "failed")

    _drop_trigger(recorder, "reject_degradation_plan")
    monkeypatch.setattr(recorder_module, "capture_snapshot", real_capture)
    (repository / "tracked.py").write_text(
        f"{first_line}\n{later_line}\n",
        encoding="utf-8",
    )

    recovered = recorder.end(transaction_id, "failed")

    assert recovered == degraded
    assert degraded["ok"] is False
    assert degraded["status"] == "unavailable"
    assert degraded["error"] == "CAPTURE_FAILED"
    assert degraded["coverage"] == "UNKNOWN"
    assert degraded["counts"] == {}
    recoveries = _event_records(recorder, "recovery_detected")
    assert len(recoveries) == 1
    assert recoveries[0]["event_id"] in degraded["event_ids"]
    assert _event_records(recorder, "patch_applied") == []
    fresh = recorder.begin("session-after-plan-failure")
    assert fresh["ok"] is True


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
            proposed_hunks=[
                _proposed_hunk(
                    action="ADDED",
                    path="good.py",
                    old_start=0,
                    old_end=0,
                    new_start=0,
                    new_end=1,
                    new_lines=["good = 1"],
                ),
                _proposed_hunk(
                    action="ADDED",
                    path="bad.py",
                    old_start=0,
                    old_end=0,
                    new_start=0,
                    new_end=1,
                    new_lines=["bad = 1"],
                ),
            ],
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
