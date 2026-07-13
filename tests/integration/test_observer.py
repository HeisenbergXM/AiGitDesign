from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import aigit.observer as observer_module
import aigit.process as process_module
import aigit.recorder as recorder_module
from aigit.domain import Event, GitSnapshot
from aigit.git_state import MAX_FILE_BYTES, capture_snapshot, repo_id
from aigit.observer import Observer
from aigit.process import ensure_observer
from aigit.recorder import Recorder


PROMPT_HMAC_KEY = b"task-6-observer-test-key-32bytes"


@dataclass
class FakeClock:
    current: datetime = datetime(2000, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _initialize_repo(root: Path, filename: str = "app.py") -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", root, "config", "user.name", "Observer Tests"],
        check=True,
    )
    (root / filename).write_text("committed = 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", filename], check=True)
    subprocess.run(
        ["git", "-C", root, "commit", "-q", "-m", "initial"],
        check=True,
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _initialize_repo(tmp_path / "repo")


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setenv("AIGIT_STATE_DIR", str(root))
    monkeypatch.setenv("AIGIT_PROMPT_HMAC_KEY", PROMPT_HMAC_KEY.hex())
    return root


@pytest.fixture
def observer(repo: Path, state_root: Path) -> Observer:
    return Observer(repo, state_root=state_root)


def _state_path(repo: Path, state_root: Path) -> Path:
    return state_root / repo_id(repo).removeprefix("sha256:")


def _ledger_records(repo: Path, state_root: Path) -> list[dict[str, object]]:
    ledger = _state_path(repo, state_root) / "events.jsonl"
    if not ledger.exists():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _queued_records(repo: Path, state_root: Path) -> list[dict[str, object]]:
    database = _state_path(repo, state_root) / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_json FROM upload_queue ORDER BY event_id"
        ).fetchall()
    return [json.loads(bytes(row[0])) for row in rows]


def _events_of_type(events: list[Event], event_type: str) -> list[Event]:
    return [event for event in events if event.event_type == event_type]


def _only_contribution(events: list[Event]) -> Event:
    contributions = [
        event
        for event in events
        if event.event_type in {"workspace_edit", "recovery_detected"}
    ]
    assert len(contributions) == 1
    return contributions[0]


def _assert_metadata_only(event: Event) -> None:
    encoded = json.dumps(event.payload, sort_keys=True)
    assert "old_lines" not in encoded
    assert "new_lines" not in encoded


@pytest.mark.parametrize("age_seconds", [10, 30])
def test_healthy_external_edit_is_manual_candidate_through_thirty_seconds(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    age_seconds: int,
) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=age_seconds)
    (repo / "app.py").write_text(
        "committed = 0\nmanual = 1\n", encoding="utf-8"
    )

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == "workspace_edit"
    assert event.payload["classification"] == "MANUAL_CANDIDATE"
    assert event.payload["normalized_lines"] == 1
    assert event.payload["path"] == "app.py"
    _assert_metadata_only(event)


def test_thirty_one_second_gap_emits_one_unknown_recovery_delta(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=31)
    (repo / "app.py").write_text(
        "committed = 0\ngap_change = 1\n", encoding="utf-8"
    )

    events = observer.tick(clock.now())

    recoveries = _events_of_type(events, "recovery_detected")
    assert len(recoveries) == 1
    assert recoveries[0].payload["classification"] == "UNKNOWN"
    assert recoveries[0].payload["normalized_lines"] == 1
    assert recoveries[0].payload["path"] == "app.py"
    assert _events_of_type(events, "workspace_edit") == []
    _assert_metadata_only(recoveries[0])


def test_active_transaction_is_deferred_entirely_to_recorder_end(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    transaction_id = str(recorder.begin("agent-session")["transaction_id"])
    (repo / "app.py").write_text(
        "committed = 0\nai_transaction_change = 1\n", encoding="utf-8"
    )
    clock.advance(seconds=10)

    during_transaction = observer.tick(clock.now())

    assert _events_of_type(during_transaction, "workspace_edit") == []
    assert _events_of_type(during_transaction, "recovery_detected") == []
    ended = recorder.end(transaction_id, "passed")
    assert ended["ok"] is True
    patch_records = [
        record
        for record in _ledger_records(repo, state_root)
        if record["event_type"] == "patch_applied"
    ]
    assert len(patch_records) == 1

    (repo / "app.py").write_text(
        "committed = 0\nai_transaction_change = 1\nmanual_after = 2\n",
        encoding="utf-8",
    )
    clock.advance(seconds=10)
    after_transaction = _only_contribution(observer.tick(clock.now()))

    assert after_transaction.event_type == "workspace_edit"
    assert after_transaction.payload["classification"] == "MANUAL_CANDIDATE"
    assert after_transaction.payload["normalized_lines"] == 1
    assert after_transaction.payload["spans"][0]["new_start"] == 2


@pytest.mark.parametrize(
    ("age_seconds", "event_type", "classification"),
    [
        pytest.param(10, "workspace_edit", "MANUAL_CANDIDATE", id="healthy"),
        pytest.param(31, "recovery_detected", "UNKNOWN", id="gap"),
    ],
)
def test_reconciliation_preserves_external_edit_before_transaction_on_same_path(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    age_seconds: int,
    event_type: str,
    classification: str,
) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=age_seconds)
    (repo / "app.py").write_text(
        "committed = 0\nexternal_before = 1\n", encoding="utf-8"
    )
    recorder = Recorder(repo, state_root)
    transaction_id = str(recorder.begin("agent-after-external")["transaction_id"])
    (repo / "app.py").write_text(
        "committed = 0\nexternal_before = 1\nai_change = 2\n",
        encoding="utf-8",
    )
    assert recorder.end(transaction_id, "passed")["ok"] is True

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == event_type
    assert event.payload["classification"] == classification
    assert event.payload["normalized_lines"] == 1
    assert event.payload["spans"][0]["new_start"] == 1
    assert event.payload["spans"][0]["new_end"] == 2


def test_capture_failure_keeps_delta_unknown_until_successful_recovery(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer.tick(clock.now())
    (repo / "app.py").write_text(
        "committed = 0\nunreadable_during_poll = 1\n", encoding="utf-8"
    )
    real_capture = observer_module.capture_snapshot

    def fail_capture(*_args: object, **_kwargs: object) -> GitSnapshot:
        raise OSError("injected unreadable snapshot")

    monkeypatch.setattr(observer_module, "capture_snapshot", fail_capture)
    clock.advance(seconds=10)
    failed_tick = observer.tick(clock.now())

    failed_recovery = _only_contribution(failed_tick)
    assert failed_recovery.event_type == "recovery_detected"
    assert failed_recovery.payload["classification"] == "UNKNOWN"
    assert _events_of_type(failed_tick, "heartbeat")[0].payload["healthy"] is False

    monkeypatch.setattr(observer_module, "capture_snapshot", real_capture)
    clock.advance(seconds=10)
    recovered = _only_contribution(observer.tick(clock.now()))

    assert recovered.event_type == "recovery_detected"
    assert recovered.payload["classification"] == "UNKNOWN"
    assert recovered.payload["normalized_lines"] == 1


def test_transaction_completed_during_capture_is_never_manual_candidate(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    real_capture = observer_module.capture_snapshot
    raced = False

    def capture_while_transaction_completes(
        *args: object, **kwargs: object
    ) -> GitSnapshot:
        nonlocal raced
        if not raced:
            raced = True
            transaction_id = str(
                recorder.begin("agent-capture-race")["transaction_id"]
            )
            (repo / "app.py").write_text(
                "committed = 0\nai_during_capture = 1\n", encoding="utf-8"
            )
            assert recorder.end(transaction_id, "passed")["ok"] is True
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(
        observer_module,
        "capture_snapshot",
        capture_while_transaction_completes,
    )
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))

    assert event.payload["classification"] == "UNKNOWN"


def test_transaction_completed_between_reconciliation_and_fence_is_not_manual(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    real_reconcile = observer._reconcile_completed_transactions
    raced = False

    def reconcile_then_complete(*args: object, **kwargs: object) -> object:
        nonlocal raced
        reconciled = real_reconcile(*args, **kwargs)
        if not raced:
            raced = True
            transaction_id = str(
                recorder.begin("agent-after-reconcile")["transaction_id"]
            )
            (repo / "app.py").write_text(
                "committed = 0\nai_after_reconcile = 1\n", encoding="utf-8"
            )
            assert recorder.end(transaction_id, "passed")["ok"] is True
        return reconciled

    monkeypatch.setattr(
        observer,
        "_reconcile_completed_transactions",
        reconcile_then_complete,
    )
    clock.advance(seconds=10)

    first = _only_contribution(observer.tick(clock.now()))

    assert first.payload["classification"] == "UNKNOWN"
    monkeypatch.setattr(
        observer,
        "_reconcile_completed_transactions",
        real_reconcile,
    )
    clock.advance(seconds=10)
    second = _only_contribution(observer.tick(clock.now()))
    assert second.payload["classification"] == "UNKNOWN"


def test_transaction_completed_after_snapshot_does_not_advance_ledger_watermark(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    real_capture = observer_module.capture_snapshot
    raced = False

    def capture_then_complete(*args: object, **kwargs: object) -> GitSnapshot:
        nonlocal raced
        snapshot = real_capture(*args, **kwargs)
        if not raced:
            raced = True
            transaction_id = str(
                recorder.begin("agent-after-snapshot")["transaction_id"]
            )
            (repo / "app.py").write_text(
                "committed = 0\nai_after_snapshot = 1\n", encoding="utf-8"
            )
            assert recorder.end(transaction_id, "passed")["ok"] is True
        return snapshot

    monkeypatch.setattr(observer_module, "capture_snapshot", capture_then_complete)
    clock.advance(seconds=10)
    first = _only_contribution(observer.tick(clock.now()))
    assert first.payload["classification"] == "UNKNOWN"

    monkeypatch.setattr(observer_module, "capture_snapshot", real_capture)
    clock.advance(seconds=10)
    second = _only_contribution(observer.tick(clock.now()))

    assert second.payload["classification"] == "UNKNOWN"


def test_gap_with_active_transaction_preserves_unknown_pretransaction_delta(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=31)
    (repo / "app.py").write_text(
        "committed = 0\ngap_before_transaction = 1\n", encoding="utf-8"
    )
    recorder = Recorder(repo, state_root)
    transaction_id = str(recorder.begin("agent-during-gap")["transaction_id"])
    (repo / "app.py").write_text(
        "committed = 0\ngap_before_transaction = 1\nai_in_transaction = 2\n",
        encoding="utf-8",
    )

    active_tick = observer.tick(clock.now())

    active_recovery = _only_contribution(active_tick)
    assert active_recovery.event_type == "recovery_detected"
    assert active_recovery.payload["classification"] == "UNKNOWN"
    assert _events_of_type(active_tick, "heartbeat")[0].payload["healthy"] is False
    assert recorder.end(transaction_id, "passed")["ok"] is True

    clock.advance(seconds=10)
    recovered = _only_contribution(observer.tick(clock.now()))
    assert recovered.event_type == "recovery_detected"
    assert recovered.payload["classification"] == "UNKNOWN"
    assert recovered.payload["normalized_lines"] == 1


def test_aborted_transaction_delta_is_unknown_not_manual_candidate(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    transaction_id = str(recorder.begin("agent-aborted-edit")["transaction_id"])
    (repo / "app.py").write_text(
        "committed = 0\npartial_before_abort = 1\n", encoding="utf-8"
    )
    assert recorder.abort(transaction_id, "apply interrupted")["ok"] is True
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == "recovery_detected"
    assert event.payload["classification"] == "UNKNOWN"
    assert event.payload["normalized_lines"] == 1


def test_transaction_scoped_degradation_delta_is_unknown(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer.tick(clock.now())
    recorder = Recorder(repo, state_root)
    transaction_id = str(recorder.begin("agent-degraded-edit")["transaction_id"])
    (repo / "app.py").write_text(
        "committed = 0\nuncaptured_before_recovery = 1\n", encoding="utf-8"
    )

    def fail_recorder_capture(*_args: object, **_kwargs: object) -> GitSnapshot:
        raise OSError("injected recorder capture failure")

    monkeypatch.setattr(recorder_module, "capture_snapshot", fail_recorder_capture)
    degraded = recorder.end(transaction_id, "failed")
    assert degraded["coverage"] == "UNKNOWN"
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == "recovery_detected"
    assert event.payload["classification"] == "UNKNOWN"
    assert event.payload["normalized_lines"] == 1


def test_first_recovery_heartbeat_is_the_next_healthy_baseline(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
) -> None:
    observer.tick(clock.now())
    clock.advance(seconds=31)
    (repo / "app.py").write_text(
        "committed = 0\ngap_change = 1\n", encoding="utf-8"
    )
    recovery_tick = observer.tick(clock.now())
    assert len(_events_of_type(recovery_tick, "recovery_detected")) == 1
    assert len(_events_of_type(recovery_tick, "heartbeat")) == 1

    clock.advance(seconds=10)
    (repo / "app.py").write_text(
        "committed = 0\ngap_change = 1\nmanual_after = 2\n", encoding="utf-8"
    )
    healthy_tick = observer.tick(clock.now())

    event = _only_contribution(healthy_tick)
    assert event.event_type == "workspace_edit"
    assert event.payload["classification"] == "MANUAL_CANDIDATE"
    assert event.payload["normalized_lines"] == 1
    assert event.payload["spans"][0]["new_start"] == 2


def test_persisted_heartbeat_and_snapshot_survive_process_restart(
    repo: Path,
    state_root: Path,
    clock: FakeClock,
) -> None:
    first_process = Observer(repo, state_root=state_root)
    first_process.tick(clock.now())
    clock.advance(seconds=20)
    (repo / "app.py").write_text(
        "committed = 0\nrestart_edit = 1\n", encoding="utf-8"
    )

    restarted_process = Observer(repo, state_root=state_root)
    event = _only_contribution(restarted_process.tick(clock.now()))

    assert event.event_type == "workspace_edit"
    assert event.payload["classification"] == "MANUAL_CANDIDATE"
    assert event.payload["normalized_lines"] == 1


def test_restart_after_crash_records_lifecycle_and_only_gap_delta(
    repo: Path,
    state_root: Path,
    clock: FakeClock,
) -> None:
    first_process = Observer(repo, state_root=state_root)
    startup = first_process.tick(clock.now())
    assert len(_events_of_type(startup, "observer_started")) == 1
    assert len(_events_of_type(startup, "heartbeat")) == 1

    clock.advance(seconds=31)
    (repo / "app.py").write_text(
        "committed = 0\nduring_gap = 1\n", encoding="utf-8"
    )
    restarted_process = Observer(repo, state_root=state_root)
    recovery_tick = restarted_process.tick(clock.now())

    assert len(_events_of_type(recovery_tick, "observer_started")) == 1
    assert len(_events_of_type(recovery_tick, "observer_crashed")) == 1
    recovery = _only_contribution(recovery_tick)
    assert recovery.event_type == "recovery_detected"
    assert recovery.payload["classification"] == "UNKNOWN"
    assert recovery.payload["spans"] == [
        {
            "action": "ADDED",
            "classification": "UNKNOWN",
            "confidence": 0.0,
            "old_start": 1,
            "old_end": 1,
            "new_start": 1,
            "new_end": 2,
        }
    ]

    clock.advance(seconds=10)
    (repo / "app.py").write_text(
        "committed = 0\nduring_gap = 1\nafter_recovery = 2\n", encoding="utf-8"
    )
    after_recovery = _only_contribution(restarted_process.tick(clock.now()))

    assert after_recovery.event_type == "workspace_edit"
    assert after_recovery.payload["normalized_lines"] == 1
    assert after_recovery.payload["spans"][0]["new_start"] == 2


@pytest.mark.parametrize("reason", ["unreadable", "concurrent"])
def test_ambiguous_file_state_is_unknown_instead_of_skipped(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    observer.tick(clock.now())
    (repo / "app.py").write_text(
        "committed = 0\nambiguous = 1\n", encoding="utf-8"
    )
    real_capture = capture_snapshot

    def ambiguous_snapshot(*args: object, **kwargs: object) -> GitSnapshot:
        snapshot = real_capture(*args, **kwargs)
        files = dict(snapshot.files)
        files["app.py"] = f"unknown:{reason}:" + "0" * 64
        return replace(snapshot, files=files)

    monkeypatch.setattr(observer_module, "capture_snapshot", ambiguous_snapshot)
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == "workspace_edit"
    assert event.payload["classification"] == "UNKNOWN"
    assert event.payload["path"] == "app.py"
    assert event.payload["spans"][0]["classification"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("asset.bin", b"private\x00bytes", id="binary"),
        pytest.param(
            "oversized.py",
            b"x" * (MAX_FILE_BYTES + 1),
            id="oversized",
        ),
    ],
)
def test_binary_and_oversized_changes_are_unknown_instead_of_skipped(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    filename: str,
    content: bytes,
) -> None:
    observer.tick(clock.now())
    (repo / filename).write_bytes(content)
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))

    assert event.event_type == "workspace_edit"
    assert event.payload["classification"] == "UNKNOWN"
    assert event.payload["path"] == filename
    assert event.payload["spans"][0]["classification"] == "UNKNOWN"


def test_heartbeat_is_emitted_exactly_every_ten_seconds_without_sleep(
    observer: Observer,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_sleep(_seconds: float) -> None:
        raise AssertionError("Observer.tick must never sleep")

    monkeypatch.setattr("time.sleep", forbidden_sleep)
    observed: list[Event] = []
    observed.extend(observer.tick(clock.now()))
    clock.advance(seconds=9)
    observed.extend(observer.tick(clock.now()))
    clock.advance(seconds=1)
    observed.extend(observer.tick(clock.now()))
    observed.extend(observer.tick(clock.now()))
    clock.advance(seconds=9)
    observed.extend(observer.tick(clock.now()))
    clock.advance(seconds=1)
    observed.extend(observer.tick(clock.now()))

    heartbeats = _events_of_type(observed, "heartbeat")
    assert [event.observed_at for event in heartbeats] == [
        "2000-01-01T00:00:00Z",
        "2000-01-01T00:00:10Z",
        "2000-01-01T00:00:20Z",
    ]


def test_raw_source_and_local_identity_are_absent_from_event_and_upload_payloads(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "TOP_SECRET_SOURCE_9cb42 = 1"
    monkeypatch.setenv("USER", "FORGED_OS_USER")
    monkeypatch.setenv("USERNAME", "FORGED_OS_USER")
    monkeypatch.setenv("EDITOR", "FORGED_EDITOR_PROCESS")
    monkeypatch.setattr(
        os,
        "getlogin",
        lambda: (_ for _ in ()).throw(AssertionError("must not read OS user")),
    )
    monkeypatch.setattr(
        getpass,
        "getuser",
        lambda: (_ for _ in ()).throw(AssertionError("must not read OS user")),
    )
    observer.tick(clock.now())
    (repo / "app.py").write_text(
        f"committed = 0\n{source}\n", encoding="utf-8"
    )
    clock.advance(seconds=10)

    event = _only_contribution(observer.tick(clock.now()))
    ledger_text = (_state_path(repo, state_root) / "events.jsonl").read_text(
        encoding="utf-8"
    )
    queued_text = json.dumps(_queued_records(repo, state_root), sort_keys=True)

    assert event.payload["classification"] == "MANUAL_CANDIDATE"
    assert source not in ledger_text
    assert source not in queued_text
    assert "FORGED_OS_USER" not in ledger_text + queued_text
    assert "FORGED_EDITOR_PROCESS" not in ledger_text + queued_text
    forbidden_identity_keys = {"actor", "editor", "process", "user", "username"}
    assert forbidden_identity_keys.isdisjoint(event.payload)


def test_observer_events_are_queued_once_and_tick_retry_is_idempotent(
    observer: Observer,
    clock: FakeClock,
    repo: Path,
    state_root: Path,
) -> None:
    first = observer.tick(clock.now())
    repeated = observer.tick(clock.now())

    ledger = _ledger_records(repo, state_root)
    queued = _queued_records(repo, state_root)
    assert repeated == []
    assert len(first) == 2
    assert {record["event_id"] for record in queued} == {
        record["event_id"] for record in ledger
    }
    assert len({record["event_id"] for record in ledger}) == len(ledger)


class _FakePopen:
    def __init__(
        self,
        calls: list[tuple[tuple[object, ...], dict[str, object]]],
        pid: int,
    ) -> None:
        self._calls = calls
        self.pid = pid

    def __call__(self, *args: object, **kwargs: object) -> _FakePopen:
        self._calls.append((args, kwargs))
        return self

    def wait(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("ensure_observer must not wait for the child")


def _observer_pid_path(repo: Path, state_root: Path) -> Path:
    return _state_path(repo, state_root) / "observer.pid"


def test_ensure_observer_is_idempotent_for_a_live_pid(
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = _observer_pid_path(repo, state_root)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("4242\n", encoding="ascii")
    checked: list[tuple[int, int]] = []
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        process_module.os,
        "kill",
        lambda pid, signal: checked.append((pid, signal)),
    )
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        _FakePopen(launches, 9001),
    )

    ensure_observer(repo)
    ensure_observer(repo)

    assert checked == [(4242, 0), (4242, 0)]
    assert launches == []
    assert pid_path.read_text(encoding="ascii") == "4242\n"


def test_ensure_observer_replaces_stale_pid_and_starts_one_child_per_repo(
    repo: Path,
    state_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_repo = _initialize_repo(tmp_path / "second-repo", "second.py")
    first_pid_path = _observer_pid_path(repo, state_root)
    first_pid_path.parent.mkdir(parents=True)
    first_pid_path.write_text("111\n", encoding="ascii")
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    next_pids = iter((7001, 7002))

    def pid_is_live(pid: int, _signal: int) -> None:
        if pid == 111:
            raise ProcessLookupError

    def launch(*args: object, **kwargs: object) -> _FakePopen:
        child = _FakePopen(launches, next(next_pids))
        return child(*args, **kwargs)

    monkeypatch.setattr(process_module.os, "kill", pid_is_live)
    monkeypatch.setattr(process_module.subprocess, "Popen", launch)

    ensure_observer(repo)
    ensure_observer(repo)
    ensure_observer(second_repo)
    ensure_observer(second_repo)

    assert len(launches) == 2
    assert first_pid_path.read_text(encoding="ascii") == "7001\n"
    assert _observer_pid_path(second_repo, state_root).read_text(
        encoding="ascii"
    ) == "7002\n"


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
def test_ensure_observer_uses_platform_detachment_and_returns_without_waiting(
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
) -> None:
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(process_module.platform, "system", lambda: system_name)
    monkeypatch.setattr(process_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(process_module.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        _FakePopen(launches, 8080),
    )

    ensure_observer(repo)

    assert len(launches) == 1
    args, kwargs = launches[0]
    command = args[0]
    assert command[0] == sys.executable
    assert "aigit.observer" in command
    if system_name == "Windows":
        assert kwargs["creationflags"] == 0x08000008
        assert kwargs.get("start_new_session", False) is False
    else:
        assert kwargs["start_new_session"] is True
        assert kwargs.get("creationflags", 0) == 0


def test_corrupt_pid_file_recovers_atomically_and_launch_failure_is_safe(
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = _observer_pid_path(repo, state_root)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("12 trailing-junk\n", encoding="ascii")
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        _FakePopen(launches, 7331),
    )

    ensure_observer(repo)

    assert pid_path.read_text(encoding="ascii") == "7331\n"
    assert list(pid_path.parent.glob("observer.pid.*.tmp")) == []

    pid_path.write_text("invalid-again\n", encoding="ascii")

    def launch_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected launch failure")

    monkeypatch.setattr(process_module.subprocess, "Popen", launch_failure)
    with pytest.raises(OSError, match="injected launch failure"):
        ensure_observer(repo)

    if pid_path.exists():
        assert pid_path.read_text(encoding="ascii") == "invalid-again\n"
    assert list(pid_path.parent.glob("observer.pid.*.tmp")) == []


def test_ensure_observer_recovers_lock_owned_by_dead_process(
    repo: Path,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = _observer_pid_path(repo, state_root)
    pid_path.parent.mkdir(parents=True)
    lock_path = pid_path.parent / "observer.pid.lock"
    lock_path.write_text("31337\n", encoding="ascii")
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def pid_is_dead(pid: int, _signal: int) -> None:
        assert pid == 31337
        raise ProcessLookupError

    monkeypatch.setattr(process_module.os, "kill", pid_is_dead)
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        _FakePopen(launches, 8448),
    )

    ensure_observer(repo)

    assert len(launches) == 1
    assert pid_path.read_text(encoding="ascii") == "8448\n"
    assert not lock_path.exists()
