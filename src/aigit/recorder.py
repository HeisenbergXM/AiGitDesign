"""Transaction coordinator for local AI contribution provenance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any, Iterable
from uuid import uuid4

from aigit.canonical import canonical_json, hash_bytes
from aigit.classifier import (
    ClassificationContext,
    RepositoryBlock,
    classify_spans,
)
from aigit.domain import Classification, Event, GitSnapshot, PatchSpan
from aigit.git_state import capture_snapshot, diff_snapshots, find_repo, repo_id
from aigit.local_store import LocalStore
from aigit.prompt_evidence import PromptEvidence


class InvalidRecorderInput(ValueError):
    """Raised when a public recorder argument is invalid."""


class RecorderStateError(RuntimeError):
    """Raised when durable recorder state is missing or inconsistent."""


@dataclass(frozen=True)
class _ActiveTransaction:
    transaction_id: str
    repo_id: str
    session_id: str
    started_at: str
    snapshot: GitSnapshot
    prompt_metadata: dict[str, object]


class Recorder:
    """Delimit apply transactions and persist their evidence locally."""

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
        self._migrate_active_transactions()

    def begin(
        self,
        session_id: str,
        prompt_evidence_path: str | Path | None = None,
    ) -> dict[str, object]:
        """Capture the transaction boundary, deleting temporary evidence always."""

        if not session_id.strip():
            raise InvalidRecorderInput("session must not be empty")
        evidence_path = (
            Path(prompt_evidence_path) if prompt_evidence_path is not None else None
        )
        try:
            prompt_metadata = self._read_prompt_metadata(evidence_path)
            connection = sqlite3.connect(self.store.database_path, timeout=0.25)
            try:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).casefold():
                        return {
                            "ok": False,
                            "status": "unavailable",
                            "error": "LOCK_TIMEOUT",
                        }
                    raise

                existing = connection.execute(
                    "SELECT transaction_id FROM active_transactions WHERE repo_id = ?",
                    (self.repo_id,),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return {
                        "ok": False,
                        "status": "unavailable",
                        "error": "ACTIVE_TRANSACTION",
                    }

                snapshot = capture_snapshot(self.repository, self.store)
                transaction_id = str(uuid4())
                started = Event.new(
                    self.repo_id,
                    session_id,
                    "transaction_started",
                    {
                        "transaction_id": transaction_id,
                        "head_before": snapshot.head,
                        "dirty_diff_hash_before": snapshot.worktree_hash,
                        "prompt_evidence": prompt_metadata,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO active_transactions (
                        transaction_id,
                        repo_id,
                        session_id,
                        started_at,
                        snapshot_json,
                        prompt_evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        transaction_id,
                        self.repo_id,
                        session_id,
                        started.observed_at,
                        canonical_json(asdict(snapshot)).decode("utf-8"),
                        canonical_json(prompt_metadata).decode("utf-8"),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

            appended = self._append_and_enqueue(started)
            return {
                "ok": True,
                "status": "local-only",
                "transaction_id": transaction_id,
                "event_ids": [appended.event_id],
                "queue_status": "pending",
            }
        finally:
            if evidence_path is not None:
                evidence_path.unlink(missing_ok=True)

    def end(self, transaction_id: str, validation: str) -> dict[str, object]:
        """Record only the net snapshot delta produced by one transaction."""

        if validation not in {"passed", "failed", "not-run"}:
            raise InvalidRecorderInput("validation must be passed, failed, or not-run")
        active = self._load_active(transaction_id)
        after = capture_snapshot(self.repository, self.store)
        raw_spans = diff_snapshots(active.snapshot, after, self.store)
        classified = self._classify(raw_spans, active.snapshot)
        grouped: dict[str, list[PatchSpan]] = defaultdict(list)
        for span in classified:
            grouped[span.path].append(span)

        event_ids: list[str] = []
        total_counts: dict[str, int] = defaultdict(int)
        for path in sorted(grouped):
            spans = grouped[path]
            counts = self._span_counts(spans)
            for classification, count in counts.items():
                total_counts[classification] += count
            payload = self._patch_payload(
                active,
                after,
                path,
                spans,
                counts,
                validation,
            )
            appended = self._append_and_enqueue(
                Event.new(
                    self.repo_id,
                    active.session_id,
                    "patch_applied",
                    payload,
                )
            )
            event_ids.append(appended.event_id)

        finished = self._append_and_enqueue(
            Event.new(
                self.repo_id,
                active.session_id,
                "transaction_finished",
                {
                    "transaction_id": active.transaction_id,
                    "head_before": active.snapshot.head,
                    "head_after": after.head,
                    "validation": validation,
                    "counts": dict(sorted(total_counts.items())),
                },
            )
        )
        event_ids.append(finished.event_id)
        self._clear_active(active.transaction_id)
        return {
            "ok": True,
            "status": "local-only",
            "event_ids": event_ids,
            "queue_status": "pending",
            "counts": dict(sorted(total_counts.items())),
        }

    def abort(self, transaction_id: str, reason: str) -> dict[str, object]:
        """Clear a transaction and record no patch contribution."""

        active = self._load_active(transaction_id)
        aborted = self._append_and_enqueue(
            Event.new(
                self.repo_id,
                active.session_id,
                "transaction_aborted",
                {
                    "transaction_id": transaction_id,
                    "reason": reason,
                },
            )
        )
        self._clear_active(transaction_id)
        return {
            "ok": True,
            "status": "local-only",
            "event_ids": [aborted.event_id],
            "queue_status": "pending",
            "counts": {},
        }

    def status(self) -> dict[str, object]:
        corrupt = self.store.verify_chain()
        if corrupt:
            return {
                "ok": False,
                "status": "unavailable",
                "error": "STATE_CORRUPTION",
                "corrupt_event_ids": corrupt,
            }
        return {
            "ok": True,
            "status": "local-only",
            "repo_id": self.repo_id,
            "queue_status": "pending" if self._queue_count() else "empty",
        }

    def link_commit(self, commit: str) -> dict[str, object]:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(self.repository),
                    "rev-parse",
                    "--verify",
                    f"{commit}^{{commit}}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise InvalidRecorderInput("commit is not a valid commit object") from exc
        resolved = completed.stdout.strip()
        linked = self._append_and_enqueue(
            Event.new(
                self.repo_id,
                "system",
                "commit_linked",
                {"commit": resolved},
            )
        )
        return {
            "ok": True,
            "status": "local-only",
            "event_ids": [linked.event_id],
            "queue_status": "pending",
        }

    def upload_once(self) -> dict[str, object]:
        """Report durable local queue state without attempting later-task I/O."""

        queued_events = self._queue_count()
        return {
            "ok": True,
            "status": "local-only",
            "queue_status": "pending" if queued_events else "empty",
            "queued_events": queued_events,
        }

    def report(self, revision: str) -> dict[str, object]:
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(self.repository),
                    "rev-parse",
                    "--verify",
                    revision,
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise InvalidRecorderInput("revision is not valid") from exc

        counts: dict[str, int] = defaultdict(int)
        for record in self._ledger_records():
            if record.get("repo_id") != self.repo_id:
                continue
            if record.get("event_type") != "patch_applied":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise RecorderStateError("patch event payload is corrupt")
            event_counts = payload.get("counts")
            if not isinstance(event_counts, dict):
                raise RecorderStateError("patch event counts are corrupt")
            for classification, count in event_counts.items():
                if not isinstance(classification, str) or not isinstance(count, int):
                    raise RecorderStateError("patch event count is corrupt")
                counts[classification] += count
        return {
            "ok": True,
            "status": "local-only",
            "revision": revision,
            "counts": dict(sorted(counts.items())),
            "revision_stock": "unavailable",
            "coverage": "unavailable",
        }

    def _migrate_active_transactions(self) -> None:
        connection = sqlite3.connect(self.store.database_path)
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(active_transactions)"
                ).fetchall()
            }
            if "snapshot_json" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions ADD COLUMN snapshot_json TEXT"
                )
            if "prompt_evidence_json" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN prompt_evidence_json TEXT"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _read_prompt_metadata(path: Path | None) -> dict[str, object]:
        if path is None:
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidRecorderInput("prompt evidence must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise InvalidRecorderInput("prompt evidence must be a JSON object")
        allowed = {
            "fingerprints",
            "counts",
            "normalized_line_count",
            "normalized_token_count",
            "line_fingerprints",
        }
        return {key: parsed[key] for key in sorted(allowed & parsed.keys())}

    def _load_active(self, transaction_id: str) -> _ActiveTransaction:
        connection = sqlite3.connect(self.store.database_path)
        try:
            row = connection.execute(
                """
                SELECT transaction_id, repo_id, session_id, started_at,
                       snapshot_json, prompt_evidence_json
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise InvalidRecorderInput("transaction does not exist for this repository")
        if not isinstance(row[4], str):
            raise RecorderStateError("active transaction has no before snapshot")
        try:
            snapshot_dict = json.loads(row[4])
            prompt_metadata = json.loads(row[5] or "{}")
            snapshot = GitSnapshot(**snapshot_dict)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecorderStateError("active transaction state is corrupt") from exc
        if not isinstance(prompt_metadata, dict):
            raise RecorderStateError("prompt evidence metadata is corrupt")
        return _ActiveTransaction(
            transaction_id=str(row[0]),
            repo_id=str(row[1]),
            session_id=str(row[2]),
            started_at=str(row[3]),
            snapshot=snapshot,
            prompt_metadata=prompt_metadata,
        )

    def _clear_active(self, transaction_id: str) -> None:
        connection = sqlite3.connect(self.store.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM active_transactions "
                "WHERE transaction_id = ? AND repo_id = ?",
                (transaction_id, self.repo_id),
            )
            if cursor.rowcount != 1:
                raise RecorderStateError("active transaction changed concurrently")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _classify(
        self,
        spans: Iterable[PatchSpan],
        before: GitSnapshot,
    ) -> list[PatchSpan]:
        spans_list = list(spans)
        repository_blocks: list[RepositoryBlock] = []
        for path, reference in before.files.items():
            if not reference.startswith("sha256:"):
                continue
            try:
                lines = tuple(
                    self.store.get_blob(reference).decode("utf-8").splitlines()
                )
                repository_blocks.append(
                    RepositoryBlock(path, lines, Classification.LEGACY_UNKNOWN)
                )
            except (OSError, UnicodeError, ValueError):
                continue
        removed_blocks = tuple(
            RepositoryBlock(
                span.old_path or span.path,
                span.old_lines,
                Classification.LEGACY_UNKNOWN,
            )
            for span in spans_list
            if span.old_lines
        )
        context = ClassificationContext(
            in_transaction=True,
            observer_healthy=True,
            prompt_evidence=PromptEvidence((), (), b"aigit-empty-evidence", ()),
            repository_blocks=tuple(repository_blocks),
            removed_blocks=removed_blocks,
        )
        try:
            return classify_spans(spans_list, context)
        except Exception:
            return [
                replace(span, classification=Classification.UNKNOWN, confidence=0.0)
                for span in spans_list
            ]

    @staticmethod
    def _span_counts(spans: Iterable[PatchSpan]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for span in spans:
            count = sum(1 for line in span.new_lines if line.rstrip())
            if count:
                counts[span.classification.value] += count
        return dict(sorted(counts.items()))

    def _patch_payload(
        self,
        active: _ActiveTransaction,
        after: GitSnapshot,
        path: str,
        spans: list[PatchSpan],
        counts: dict[str, int],
        validation: str,
    ) -> dict[str, object]:
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
        classification = (
            next(iter(counts)) if len(counts) == 1 else Classification.UNKNOWN.value
        )
        return {
            "transaction_id": active.transaction_id,
            "path": path,
            "head_before": active.snapshot.head,
            "head_after": after.head,
            "dirty_diff_hash_before": active.snapshot.worktree_hash,
            "dirty_diff_hash_after": after.worktree_hash,
            "patch_hash": hash_bytes(canonical_json(span_evidence)),
            "before_blob": active.snapshot.files.get(path),
            "after_blob": after.files.get(path),
            "classification": classification,
            "normalized_lines": sum(counts.values()),
            "prompt_code_overlap_lines": counts.get(
                Classification.USER_SUPPLIED.value, 0
            ),
            "counts": counts,
            "validation": validation,
            "spans": span_evidence,
        }

    def _append_and_enqueue(self, event: Event) -> Event:
        appended = self.store.append(event)
        encoded = canonical_json(asdict(appended))
        connection = sqlite3.connect(self.store.database_path)
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

    def _queue_count(self) -> int:
        connection = sqlite3.connect(self.store.database_path)
        try:
            row = connection.execute("SELECT COUNT(*) FROM upload_queue").fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None else 0

    def _ledger_records(self) -> Iterable[dict[str, Any]]:
        if not self.store.ledger_path.exists():
            return ()
        records: list[dict[str, Any]] = []
        try:
            with self.store.ledger_path.open("r", encoding="utf-8") as ledger:
                for line in ledger:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise RecorderStateError("ledger entry is not an object")
                    records.append(record)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecorderStateError("ledger is corrupt") from exc
        return records
