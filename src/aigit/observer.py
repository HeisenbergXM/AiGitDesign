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
                _ObserverState(snapshot, observed_at, self._current_sequence())
            )
            return emitted

        gap = (
            observed_at - state.last_heartbeat_at
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
            emitted.append(self._heartbeat(state.snapshot, observed_at))
            self._save_state(
                _ObserverState(
                    state.snapshot,
                    observed_at,
                    state.ledger_sequence,
                )
            )
            return emitted

        baseline = self._reconcile_completed_transactions(state)
        try:
            current = capture_snapshot(self.repository, self.store)
            spans = diff_snapshots(baseline, current, self.store)
        except Exception:
            current = baseline
            spans = []
            gap = True

        # A transaction that began during capture owns this boundary. Do not
        # consume it or advance the persisted snapshot past it.
        if self._has_active_transaction():
            emitted.append(self._heartbeat(state.snapshot, observed_at))
            self._save_state(
                _ObserverState(
                    state.snapshot,
                    observed_at,
                    state.ledger_sequence,
                )
            )
            return emitted

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

        if gap and not spans:
            emitted.append(
                self._emit(
                    "recovery_detected",
                    {
                        "classification": Classification.UNKNOWN.value,
                        "normalized_lines": 0,
                        "dirty_diff_hash_before": baseline.worktree_hash,
                        "dirty_diff_hash_after": current.worktree_hash,
                        "spans": [],
                        "reason_code": "HEARTBEAT_GAP",
                    },
                    observed_at,
                )
            )

        emitted.append(self._heartbeat(current, observed_at))
        self._save_state(
            _ObserverState(current, observed_at, self._current_sequence())
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

    def _heartbeat(self, snapshot: GitSnapshot, now: datetime) -> Event:
        return self._emit(
            "heartbeat",
            {
                "healthy": True,
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
                    ledger_sequence INTEGER NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _load_state(self) -> _ObserverState | None:
        connection = self.store._connect()
        try:
            row = connection.execute(
                """
                SELECT snapshot_json, last_heartbeat_at, ledger_sequence
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
            sequence = int(row[2])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("observer state is corrupt") from exc
        return _ObserverState(snapshot, heartbeat, sequence)

    def _save_state(self, state: _ObserverState) -> None:
        snapshot_json = canonical_json(asdict(state.snapshot)).decode("utf-8")
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO observer_state (
                    repo_id, snapshot_json, last_heartbeat_at, ledger_sequence
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    snapshot_json = excluded.snapshot_json,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    ledger_sequence = excluded.ledger_sequence
                """,
                (
                    self.repo_id,
                    snapshot_json,
                    _timestamp(state.last_heartbeat_at),
                    state.ledger_sequence,
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
    ) -> GitSnapshot:
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
            return state.snapshot

        files = dict(state.snapshot.files)
        head = state.snapshot.head
        worktree_hash = state.snapshot.worktree_hash
        for record in records:
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            transaction_id = str(payload.get("transaction_id"))
            if transaction_id not in completed:
                continue
            if record.get("event_type") == "patch_applied":
                path = payload.get("path")
                after_blob = payload.get("after_blob")
                if isinstance(path, str):
                    if isinstance(after_blob, str):
                        files[path] = after_blob
                    elif after_blob is None:
                        files.pop(path, None)
                dirty_hash = payload.get("dirty_diff_hash_after")
                if isinstance(dirty_hash, str):
                    worktree_hash = dirty_hash
            elif record.get("event_type") == "transaction_finished":
                head_after = payload.get("head_after")
                if isinstance(head_after, str):
                    head = head_after
        return GitSnapshot(
            head=head,
            index_hash=state.snapshot.index_hash,
            worktree_hash=worktree_hash,
            files=files,
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
