"""Durable transaction-external worktree observation and heartbeats."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from threading import Event as StopEvent
from typing import Any, Iterable
from uuid import UUID, uuid5

from aigit.canonical import canonical_json, hash_bytes
from aigit.classifier import ClassificationContext, classify_spans
from aigit.domain import Classification, Event, GitSnapshot, PatchSpan
from aigit.git_state import capture_snapshot, diff_snapshots, find_repo, repo_id
from aigit.local_store import LocalStore
from aigit.prompt_evidence import build_prompt_evidence


HEARTBEAT_INTERVAL_SECONDS = 10
HEALTHY_WINDOW_SECONDS = 30
_OBSERVER_SESSION = "observer"
_EVENT_NAMESPACE = UUID("f733871b-bdf8-46f6-a27c-334ea37473d7")
_EMPTY_PROMPT_EVIDENCE_KEY = b"aigit-observer-empty-evidence"


@dataclass(frozen=True)
class _ObserverState:
    snapshot: GitSnapshot
    last_heartbeat_at: datetime
    last_healthy_at: datetime
    ledger_sequence: int
    coverage_uncertain: bool


@dataclass(frozen=True)
class _ReconciledDelta:
    before: GitSnapshot
    after: GitSnapshot
    spans: tuple[PatchSpan, ...]


@dataclass(frozen=True)
class _Reconciliation:
    snapshot: GitSnapshot
    deltas: tuple[_ReconciledDelta, ...]
    ledger_sequence: int


class Observer:
    """Poll one repository without assigning external edits to an OS identity."""

    def __init__(
        self,
        repository: str | Path,
        state_root: str | Path | None = None,
    ) -> None:
        self.repository = find_repo(repository)
        self.repo_id = repo_id(self.repository)
        configured_root = state_root or os.environ.get("AIGIT_STATE_DIR")
        root = Path(configured_root) if configured_root else Path.home() / ".aigit"
        self.store = LocalStore(root / self.repo_id.removeprefix("sha256:"))
        self._initialize_state()
        self._started = False

    def tick(self, now: datetime) -> list[Event]:
        """Run one due observation cycle and return newly emitted events."""

        observed_at = _as_utc(now)
        state = self._load_state()
        if state is not None:
            elapsed = observed_at - state.last_heartbeat_at
            if elapsed < timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS):
                return []

        emitted: list[Event] = []
        if state is None:
            snapshot = capture_snapshot(self.repository, self.store)
            emitted.append(
                self._emit(
                    "observer_started",
                    {
                        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                        "healthy_window_seconds": HEALTHY_WINDOW_SECONDS,
                    },
                    observed_at,
                )
            )
            self._started = True
            emitted.append(self._heartbeat(snapshot, observed_at))
            self._save_state(
                _ObserverState(
                    snapshot,
                    observed_at,
                    observed_at,
                    self._current_sequence(),
                    False,
                )
            )
            return emitted

        gap = (
            state.coverage_uncertain
            or observed_at - state.last_healthy_at
            > timedelta(seconds=HEALTHY_WINDOW_SECONDS)
        )
        if not self._started:
            emitted.append(
                self._emit(
                    "observer_started",
                    {
                        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                        "healthy_window_seconds": HEALTHY_WINDOW_SECONDS,
                    },
                    observed_at,
                )
            )
            self._started = True
        if gap:
            emitted.append(
                self._emit(
                    "observer_crashed",
                    {
                        "reason_code": "HEARTBEAT_GAP",
                        "last_heartbeat_at": _timestamp(state.last_heartbeat_at),
                        "alert_audience": "configured_owners",
                    },
                    observed_at,
                )
            )

        if self._has_active_transaction():
            healthy = not state.coverage_uncertain
            emitted.append(
                self._heartbeat(state.snapshot, observed_at, healthy=healthy)
            )
            self._save_state(
                _ObserverState(
                    state.snapshot,
                    observed_at,
                    observed_at if healthy else state.last_healthy_at,
                    state.ledger_sequence,
                    state.coverage_uncertain,
                )
            )
            return emitted

        reconciliation = self._reconcile_completed_transactions(state)
        baseline = reconciliation.snapshot
        contribution_emitted = False
        for delta in reconciliation.deltas:
            grouped_delta: dict[str, list[PatchSpan]] = defaultdict(list)
            for span in delta.spans:
                grouped_delta[span.path].append(span)
            for path in sorted(grouped_delta):
                ambiguous = _path_is_ambiguous(path, delta.before, delta.after)
                classified = self._classify(
                    grouped_delta[path],
                    healthy=not gap and not ambiguous,
                )
                emitted.append(
                    self._emit(
                        "recovery_detected" if gap else "workspace_edit",
                        self._delta_payload(
                            delta.before,
                            delta.after,
                            path,
                            classified,
                        ),
                        observed_at,
                    )
                )
                contribution_emitted = True

        capture_fence = self._current_sequence()
        try:
            current = capture_snapshot(self.repository, self.store)
            spans = diff_snapshots(baseline, current, self.store)
        except Exception:
            emitted.append(
                self._emit(
                    "recovery_detected",
                    {
                        "classification": Classification.UNKNOWN.value,
                        "normalized_lines": 0,
                        "dirty_diff_hash_before": baseline.worktree_hash,
                        "dirty_diff_hash_after": None,
                        "spans": [],
                        "reason_code": "CAPTURE_FAILED",
                    },
                    observed_at,
                )
            )
            emitted.append(
                self._heartbeat(baseline, observed_at, healthy=False)
            )
            self._save_state(
                _ObserverState(
                    baseline,
                    observed_at,
                    state.last_healthy_at,
                    self._current_sequence(),
                    True,
                )
            )
            return emitted

        # A transaction that began during capture owns this boundary. Do not
        # consume it or advance the persisted snapshot past it.
        if self._has_active_transaction():
            healthy = not state.coverage_uncertain
            emitted.append(
                self._heartbeat(baseline, observed_at, healthy=healthy)
            )
            self._save_state(
                _ObserverState(
                    baseline,
                    observed_at,
                    observed_at if healthy else state.last_healthy_at,
                    capture_fence,
                    state.coverage_uncertain,
                )
            )
            return emitted

        transaction_race = self._transaction_event_after(capture_fence)
        if transaction_race:
            gap = True

        grouped: dict[str, list[PatchSpan]] = defaultdict(list)
        for span in spans:
            grouped[span.path].append(span)

        for path in sorted(grouped):
            path_spans = grouped[path]
            ambiguous = _path_is_ambiguous(path, baseline, current)
            classified = self._classify(
                path_spans,
                healthy=not gap and not ambiguous,
            )
            event_type = "recovery_detected" if gap else "workspace_edit"
            emitted.append(
                self._emit(
                    event_type,
                    self._delta_payload(baseline, current, path, classified),
                    observed_at,
                )
            )
            contribution_emitted = True

        if gap and not contribution_emitted:
            emitted.append(
                self._emit(
                    "recovery_detected",
                    {
                        "classification": Classification.UNKNOWN.value,
                        "normalized_lines": 0,
                        "dirty_diff_hash_before": baseline.worktree_hash,
                        "dirty_diff_hash_after": current.worktree_hash,
                        "spans": [],
                        "reason_code": (
                            "TRANSACTION_RACE"
                            if transaction_race
                            else "HEARTBEAT_GAP"
                        ),
                    },
                    observed_at,
                )
            )

        emitted.append(self._heartbeat(current, observed_at))
        self._save_state(
            _ObserverState(
                current,
                observed_at,
                observed_at,
                self._current_sequence(),
                False,
            )
        )
        return emitted

    def _classify(
        self,
        spans: Iterable[PatchSpan],
        *,
        healthy: bool,
    ) -> list[PatchSpan]:
        context = ClassificationContext(
            in_transaction=False,
            observer_healthy=healthy,
            prompt_evidence=build_prompt_evidence(
                (), key=_EMPTY_PROMPT_EVIDENCE_KEY
            ),
            repository_blocks=(),
            removed_blocks=(),
        )
        return classify_spans(spans, context)

    def _delta_payload(
        self,
        before: GitSnapshot,
        after: GitSnapshot,
        path: str,
        spans: list[PatchSpan],
    ) -> dict[str, object]:
        classifications = {span.classification.value for span in spans}
        classification = (
            next(iter(classifications))
            if len(classifications) == 1
            else Classification.UNKNOWN.value
        )
        span_evidence = [
            {
                "action": span.action.value,
                "classification": span.classification.value,
                "confidence": span.confidence,
                "old_start": span.old_start,
                "old_end": span.old_end,
                "new_start": span.new_start,
                "new_end": span.new_end,
            }
            for span in spans
        ]
        return {
            "path": path,
            "head_before": before.head,
            "head_after": after.head,
            "dirty_diff_hash_before": before.worktree_hash,
            "dirty_diff_hash_after": after.worktree_hash,
            "patch_hash": hash_bytes(canonical_json(span_evidence)),
            "before_blob": before.files.get(path),
            "after_blob": after.files.get(path),
            "classification": classification,
            "normalized_lines": sum(
                1 for span in spans for line in span.new_lines if line.rstrip()
            ),
            "spans": span_evidence,
        }

    def _heartbeat(
        self,
        snapshot: GitSnapshot,
        now: datetime,
        *,
        healthy: bool = True,
    ) -> Event:
        return self._emit(
            "heartbeat",
            {
                "healthy": healthy,
                "interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                "snapshot_hash": snapshot.worktree_hash,
            },
            now,
        )

    def _emit(
        self,
        event_type: str,
        payload: dict[str, object],
        now: datetime,
    ) -> Event:
        stamp = _timestamp(now)
        identity = canonical_json(
            {
                "repo_id": self.repo_id,
                "event_type": event_type,
                "observed_at": stamp,
                "payload": payload,
            }
        )
        event = Event.new(self.repo_id, _OBSERVER_SESSION, event_type, payload)
        event = replace(
            event,
            event_id=str(uuid5(_EVENT_NAMESPACE, identity.decode("utf-8"))),
            observed_at=stamp,
        )
        appended = self.store.append(event)
        encoded = canonical_json(asdict(appended))
        connection = self.store._connect()
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO upload_queue (event_id, event_json)
                VALUES (?, ?)
                """,
                (appended.event_id, encoded),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return appended

    def _initialize_state(self) -> None:
        connection = self.store._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observer_state (
                    repo_id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    last_healthy_at TEXT NOT NULL,
                    coverage_uncertain INTEGER NOT NULL DEFAULT 0,
                    ledger_sequence INTEGER NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(observer_state)"
                ).fetchall()
            }
            if "last_healthy_at" not in columns:
                connection.execute(
                    "ALTER TABLE observer_state ADD COLUMN last_healthy_at TEXT"
                )
                connection.execute(
                    "UPDATE observer_state SET last_healthy_at = last_heartbeat_at"
                )
            if "coverage_uncertain" not in columns:
                connection.execute(
                    "ALTER TABLE observer_state ADD COLUMN coverage_uncertain "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()
        finally:
            connection.close()

    def _load_state(self) -> _ObserverState | None:
        connection = self.store._connect()
        try:
            row = connection.execute(
                """
                SELECT snapshot_json, last_heartbeat_at, last_healthy_at,
                       ledger_sequence, coverage_uncertain
                FROM observer_state WHERE repo_id = ?
                """,
                (self.repo_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            snapshot_data = json.loads(str(row[0]))
            snapshot = GitSnapshot(
                head=str(snapshot_data["head"]),
                index_hash=str(snapshot_data["index_hash"]),
                worktree_hash=str(snapshot_data["worktree_hash"]),
                files={
                    str(path): str(reference)
                    for path, reference in snapshot_data["files"].items()
                },
            )
            heartbeat = _parse_timestamp(str(row[1]))
            healthy = _parse_timestamp(str(row[2]))
            sequence = int(row[3])
            uncertain = bool(int(row[4]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("observer state is corrupt") from exc
        return _ObserverState(snapshot, heartbeat, healthy, sequence, uncertain)

    def _save_state(self, state: _ObserverState) -> None:
        snapshot_json = canonical_json(asdict(state.snapshot)).decode("utf-8")
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO observer_state (
                    repo_id, snapshot_json, last_heartbeat_at, last_healthy_at,
                    ledger_sequence, coverage_uncertain
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_healthy_at = excluded.last_healthy_at,
                    ledger_sequence = excluded.ledger_sequence,
                    coverage_uncertain = excluded.coverage_uncertain
                """,
                (
                    self.repo_id,
                    snapshot_json,
                    _timestamp(state.last_heartbeat_at),
                    _timestamp(state.last_healthy_at),
                    state.ledger_sequence,
                    int(state.coverage_uncertain),
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _has_active_transaction(self) -> bool:
        connection = self.store._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM active_transactions WHERE repo_id = ? LIMIT 1",
                (self.repo_id,),
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    def _current_sequence(self) -> int:
        connection = self.store._connect()
        try:
            row = connection.execute(
                "SELECT sequence FROM sequences WHERE repo_id = ?",
                (self.repo_id,),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def _reconcile_completed_transactions(
        self,
        state: _ObserverState,
    ) -> _Reconciliation:
        records = [
            record
            for record in self._ledger_records()
            if int(record.get("sequence", 0)) > state.ledger_sequence
        ]
        completed = {
            str(record.get("payload", {}).get("transaction_id"))
            for record in records
            if record.get("event_type") == "transaction_finished"
            and isinstance(record.get("payload"), dict)
        }
        if not completed:
            return _Reconciliation(
                state.snapshot,
                (),
                max(
                    (int(record.get("sequence", 0)) for record in records),
                    default=state.ledger_sequence,
                ),
            )

        baseline = state.snapshot
        deltas: list[_ReconciledDelta] = []
        finished_records = sorted(
            (
                record
                for record in records
                if record.get("event_type") == "transaction_finished"
                and isinstance(record.get("payload"), dict)
            ),
            key=lambda record: int(record.get("sequence", 0)),
        )
        for finished in finished_records:
            finished_payload = finished["payload"]
            transaction_id = str(finished_payload.get("transaction_id"))
            patches = [
                record
                for record in records
                if record.get("event_type") == "patch_applied"
                and isinstance(record.get("payload"), dict)
                and str(record["payload"].get("transaction_id"))
                == transaction_id
            ]
            before_files = dict(baseline.files)
            head_before = baseline.head
            dirty_before = baseline.worktree_hash
            for patch in patches:
                payload = patch["payload"]
                path = payload.get("path")
                before_blob = payload.get("before_blob")
                if isinstance(path, str):
                    if isinstance(before_blob, str):
                        before_files[path] = before_blob
                    elif before_blob is None:
                        before_files.pop(path, None)
                candidate_head = payload.get("head_before")
                if isinstance(candidate_head, str):
                    head_before = candidate_head
                candidate_hash = payload.get("dirty_diff_hash_before")
                if isinstance(candidate_hash, str):
                    dirty_before = candidate_hash
            transaction_before = GitSnapshot(
                head=head_before,
                index_hash=baseline.index_hash,
                worktree_hash=dirty_before,
                files=before_files,
            )
            external_spans = tuple(
                diff_snapshots(baseline, transaction_before, self.store)
            )
            if external_spans:
                deltas.append(
                    _ReconciledDelta(
                        baseline,
                        transaction_before,
                        external_spans,
                    )
                )

            after_files = dict(transaction_before.files)
            dirty_after = transaction_before.worktree_hash
            for patch in patches:
                payload = patch["payload"]
                path = payload.get("path")
                after_blob = payload.get("after_blob")
                if isinstance(path, str):
                    if isinstance(after_blob, str):
                        after_files[path] = after_blob
                    elif after_blob is None:
                        after_files.pop(path, None)
                candidate_hash = payload.get("dirty_diff_hash_after")
                if isinstance(candidate_hash, str):
                    dirty_after = candidate_hash
            head_after = finished_payload.get("head_after")
            baseline = GitSnapshot(
                head=(
                    str(head_after)
                    if isinstance(head_after, str)
                    else transaction_before.head
                ),
                index_hash=transaction_before.index_hash,
                worktree_hash=dirty_after,
                files=after_files,
            )

        return _Reconciliation(
            baseline,
            tuple(deltas),
            max(
                (int(record.get("sequence", 0)) for record in records),
                default=state.ledger_sequence,
            ),
        )

    def _transaction_event_after(self, sequence: int) -> bool:
        transaction_events = {
            "transaction_started",
            "patch_applied",
            "transaction_finished",
            "transaction_aborted",
        }
        return any(
            int(record.get("sequence", 0)) > sequence
            and record.get("event_type") in transaction_events
            for record in self._ledger_records()
        )

    def _ledger_records(self) -> list[dict[str, Any]]:
        if not self.store.ledger_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.store.ledger_path.open("r", encoding="utf-8") as ledger:
            for line in ledger:
                if not line.strip():
                    continue
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
        return records


def _path_is_ambiguous(
    path: str,
    before: GitSnapshot,
    after: GitSnapshot,
) -> bool:
    return any(
        isinstance(reference, str) and reference.startswith("unknown:")
        for reference in (before.files.get(path), after.files.get(path))
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observer time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aigit.observer")
    parser.add_argument("--repo", required=True)
    arguments = parser.parse_args(argv)
    observer = Observer(arguments.repo)
    stop = StopEvent()
    try:
        while not stop.is_set():
            observer.tick(datetime.now(timezone.utc))
            stop.wait(HEARTBEAT_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
