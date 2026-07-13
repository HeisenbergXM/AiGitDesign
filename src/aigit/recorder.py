"""Transaction coordinator for local AI contribution provenance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
from typing import Any, Iterable
from uuid import UUID, uuid4, uuid5

from aigit.canonical import canonical_json, hash_bytes
from aigit.classifier import (
    ClassificationContext,
    RepositoryBlock,
    classify_spans,
)
from aigit.domain import ActionKind, Classification, Event, GitSnapshot, PatchSpan
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


@dataclass(frozen=True)
class _TerminalPlan:
    events: tuple[Event, ...]
    result: dict[str, object]


SQLITE_TIMEOUT_SECONDS = 0.2
SQLITE_INITIALIZATION_TIMEOUT_SECONDS = 0.01
_EVENT_NAMESPACE = UUID("7a207a4d-e4df-49a6-a976-75ef255f33aa")
_PROMPT_EVIDENCE_KEYS = frozenset(
    {
        "fingerprints",
        "counts",
        "normalized_line_count",
        "normalized_token_count",
        "line_fingerprints",
        "applied_patch_fingerprints",
        "applied_patch_counts",
        "applied_patch_normalized_line_count",
        "applied_patch_normalized_token_count",
        "applied_patch_line_fingerprints",
    }
)


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
        self._sqlite_timeout = SQLITE_INITIALIZATION_TIMEOUT_SECONDS
        self.store = LocalStore(
            root / self.repo_id.removeprefix("sha256:"),
            connection_timeout=SQLITE_INITIALIZATION_TIMEOUT_SECONDS,
        )
        self._migrate_active_transactions()
        self._sqlite_timeout = SQLITE_TIMEOUT_SECONDS
        self.store.connection_timeout = SQLITE_TIMEOUT_SECONDS
        self._prompt_hmac_key = self._load_or_create_prompt_hmac_key()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.store.database_path,
            timeout=self._sqlite_timeout,
        )

    def _load_or_create_prompt_hmac_key(self) -> bytes:
        configured = os.environ.get("AIGIT_PROMPT_HMAC_KEY")
        if configured is not None:
            try:
                key = bytes.fromhex(configured)
            except ValueError as exc:
                raise ValueError(
                    "AIGIT_PROMPT_HMAC_KEY must be hexadecimal"
                ) from exc
            if len(key) < 16:
                raise ValueError(
                    "AIGIT_PROMPT_HMAC_KEY must contain at least 16 bytes"
                )
            return key

        key_path = self.store.state_path / "prompt-hmac.key"
        try:
            key = key_path.read_bytes()
        except FileNotFoundError:
            generated = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = key_path.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as destination:
                    destination.write(generated)
                    destination.flush()
                    os.fsync(destination.fileno())
                key = generated
        if len(key) < 16:
            raise RecorderStateError("prompt HMAC key state is corrupt")
        return key

    def begin(
        self,
        session_id: str,
        prompt_evidence_path: str | Path | None = None,
    ) -> dict[str, object]:
        """Capture the transaction boundary, deleting temporary evidence always."""

        evidence_path = (
            Path(prompt_evidence_path) if prompt_evidence_path is not None else None
        )
        try:
            if not session_id.strip():
                raise InvalidRecorderInput("session must not be empty")
            prompt_metadata = self._read_prompt_metadata(evidence_path)
            repair_event: Event | None = None
            repair_result: dict[str, object] | None = None
            connection = self._connect()
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
                    """
                    SELECT transaction_id, session_id,
                           started_event_json, begin_result_json
                    FROM active_transactions
                    WHERE repo_id = ?
                    """,
                    (self.repo_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[1]) != session_id:
                        connection.rollback()
                        return {
                            "ok": False,
                            "status": "unavailable",
                            "error": "ACTIVE_TRANSACTION",
                        }
                    if not isinstance(existing[2], str) or not isinstance(
                        existing[3], str
                    ):
                        connection.rollback()
                        return {
                            "ok": False,
                            "status": "unavailable",
                            "error": "ACTIVE_TRANSACTION",
                        }
                    repair_event = self._decode_event(
                        existing[2],
                        "transaction start event is corrupt",
                    )
                    repair_result = self._decode_result(
                        existing[3],
                        "transaction begin result is corrupt",
                    )
                else:
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
                    started = replace(
                        started,
                        event_id=str(
                            uuid5(
                                _EVENT_NAMESPACE,
                                f"{self.repo_id}:{transaction_id}:started",
                            )
                        ),
                    )
                    result = {
                        "ok": True,
                        "status": "local-only",
                        "transaction_id": transaction_id,
                        "event_ids": [started.event_id],
                        "queue_status": "pending",
                    }
                    connection.execute(
                        """
                        INSERT INTO active_transactions (
                            transaction_id,
                            repo_id,
                            session_id,
                            started_at,
                            snapshot_json,
                            prompt_evidence_json,
                            started_event_json,
                            begin_result_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            transaction_id,
                            self.repo_id,
                            session_id,
                            started.observed_at,
                            canonical_json(asdict(snapshot)).decode("utf-8"),
                            canonical_json(prompt_metadata).decode("utf-8"),
                            canonical_json(asdict(started)).decode("utf-8"),
                            canonical_json(result).decode("utf-8"),
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

            if repair_event is not None and repair_result is not None:
                self._append_and_enqueue(repair_event)
                return repair_result
            self._append_and_enqueue(started)
            return result
        finally:
            if evidence_path is not None:
                try:
                    evidence_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def end(self, transaction_id: str, validation: str) -> dict[str, object]:
        """Record only the net snapshot delta produced by one transaction."""

        if validation not in {"passed", "failed", "not-run"}:
            raise InvalidRecorderInput("validation must be passed, failed, or not-run")
        operation = f"end:{validation}"
        claimed = self._claim_terminal(transaction_id, operation)
        if isinstance(claimed, dict):
            return claimed
        active = claimed
        self._repair_started_event(transaction_id)
        existing_plan = self._load_terminal_plan(transaction_id, operation)
        if isinstance(existing_plan, dict):
            return existing_plan
        if existing_plan is not None:
            return self._execute_terminal_plan(
                transaction_id,
                operation,
                existing_plan,
            )
        try:
            after = capture_snapshot(self.repository, self.store)
        except Exception:
            return self._degrade_terminal(
                active,
                operation,
                "CAPTURE_FAILED",
            )
        try:
            raw_spans = diff_snapshots(active.snapshot, after, self.store)
        except Exception:
            return self._degrade_terminal(
                active,
                operation,
                "DIFF_FAILED",
            )
        classified = self._classify(
            raw_spans,
            active.snapshot,
            active.prompt_metadata,
        )
        grouped: dict[str, list[PatchSpan]] = defaultdict(list)
        for span in classified:
            grouped[span.path].append(span)

        events: list[Event] = []
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
            events.append(
                self._planned_event(
                    active,
                    "patch_applied",
                    payload,
                    f"patch:{path}",
                )
            )

        events.append(
            self._planned_event(
                active,
                "transaction_finished",
                {
                    "transaction_id": active.transaction_id,
                    "head_before": active.snapshot.head,
                    "head_after": after.head,
                    "validation": validation,
                    "counts": dict(sorted(total_counts.items())),
                },
                "finished",
            )
        )
        result = {
            "ok": True,
            "status": "local-only",
            "event_ids": [event.event_id for event in events],
            "queue_status": "pending",
            "counts": dict(sorted(total_counts.items())),
        }
        prepared = self._store_or_load_terminal_plan(
            active.transaction_id,
            operation,
            _TerminalPlan(tuple(events), result),
        )
        if isinstance(prepared, dict):
            return prepared
        return self._execute_terminal_plan(
            active.transaction_id,
            operation,
            prepared,
        )

    def abort(self, transaction_id: str, reason: str) -> dict[str, object]:
        """Clear a transaction and record no patch contribution."""

        operation = "abort"
        claimed = self._claim_terminal(transaction_id, operation)
        if isinstance(claimed, dict):
            return claimed
        active = claimed
        self._repair_started_event(transaction_id)
        existing_plan = self._load_terminal_plan(transaction_id, operation)
        if isinstance(existing_plan, dict):
            return existing_plan
        if existing_plan is not None:
            return self._execute_terminal_plan(
                transaction_id,
                operation,
                existing_plan,
            )
        aborted = self._planned_event(
            active,
            "transaction_aborted",
            {
                "transaction_id": transaction_id,
                "reason_hash": hash_bytes(reason.encode("utf-8")),
            },
            "aborted",
        )
        result = {
            "ok": True,
            "status": "local-only",
            "event_ids": [aborted.event_id],
            "queue_status": "pending",
            "counts": {},
        }
        prepared = self._store_or_load_terminal_plan(
            active.transaction_id,
            operation,
            _TerminalPlan((aborted,), result),
        )
        if isinstance(prepared, dict):
            return prepared
        return self._execute_terminal_plan(
            active.transaction_id,
            operation,
            prepared,
        )

    def _degrade_terminal(
        self,
        active: _ActiveTransaction,
        operation: str,
        reason_code: str,
    ) -> dict[str, object]:
        recovery = self._planned_event(
            active,
            "recovery_detected",
            {
                "classification": Classification.UNKNOWN.value,
                "reason_code": reason_code,
                "transaction_id": active.transaction_id,
            },
            f"recovery:{reason_code}",
        )
        result = {
            "ok": False,
            "status": "unavailable",
            "error": reason_code,
            "coverage": Classification.UNKNOWN.value,
            "event_ids": [recovery.event_id],
            "queue_status": "pending",
            "counts": {},
        }
        prepared = self._store_or_load_terminal_plan(
            active.transaction_id,
            operation,
            _TerminalPlan((recovery,), result),
        )
        if isinstance(prepared, dict):
            return prepared
        return self._execute_terminal_plan(
            active.transaction_id,
            operation,
            prepared,
        )

    def status(self) -> dict[str, object]:
        try:
            corrupt = self.store.verify_chain()
        except (OSError, UnicodeError, ValueError) as exc:
            raise RecorderStateError("local ledger is corrupt") from exc
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
            "scope": "lifetime_local_ledger",
            "counts": dict(sorted(counts.items())),
            "revision_stock_status": "unavailable",
            "coverage": "unavailable",
        }

    def _migrate_active_transactions(self) -> None:
        connection = self._connect()
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
            if "terminal_state" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_state TEXT NOT NULL DEFAULT 'active'"
                )
            if "terminal_operation" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_operation TEXT"
                )
            if "terminal_plan_json" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_plan_json TEXT"
                )
            if "started_event_json" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN started_event_json TEXT"
                )
            if "begin_result_json" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN begin_result_json TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS completed_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
                """
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
        if set(parsed) != _PROMPT_EVIDENCE_KEYS:
            raise InvalidRecorderInput(
                "prompt evidence must contain only the HMAC evidence schema"
            )

        fingerprints = parsed["fingerprints"]
        counts = parsed["counts"]
        line_fingerprints = parsed["line_fingerprints"]
        normalized_line_count = parsed["normalized_line_count"]
        normalized_token_count = parsed["normalized_token_count"]
        applied_fingerprints = parsed["applied_patch_fingerprints"]
        applied_counts = parsed["applied_patch_counts"]
        applied_line_fingerprints = parsed["applied_patch_line_fingerprints"]
        applied_normalized_line_count = parsed[
            "applied_patch_normalized_line_count"
        ]
        applied_normalized_token_count = parsed[
            "applied_patch_normalized_token_count"
        ]
        if not isinstance(fingerprints, list) or not all(
            Recorder._is_sha256_hmac(item) for item in fingerprints
        ):
            raise InvalidRecorderInput("fingerprints must be SHA-256 HMAC strings")
        if not isinstance(counts, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in counts
        ):
            raise InvalidRecorderInput("counts must be positive integer values")
        if not isinstance(line_fingerprints, list) or not all(
            isinstance(block, list)
            and all(Recorder._is_sha256_hmac(item) for item in block)
            for block in line_fingerprints
        ):
            raise InvalidRecorderInput(
                "line_fingerprints must be nested SHA-256 HMAC strings"
            )
        if not (
            len(fingerprints) == len(counts) == len(line_fingerprints)
            and all(
                count == len(block)
                for count, block in zip(counts, line_fingerprints, strict=True)
            )
        ):
            raise InvalidRecorderInput("prompt evidence block counts do not match")
        if (
            not isinstance(normalized_line_count, int)
            or isinstance(normalized_line_count, bool)
            or normalized_line_count < 0
            or normalized_line_count != sum(counts)
        ):
            raise InvalidRecorderInput("normalized line count does not match")
        if (
            not isinstance(normalized_token_count, int)
            or isinstance(normalized_token_count, bool)
            or normalized_token_count < 0
        ):
            raise InvalidRecorderInput(
                "normalized token count must be a non-negative integer"
            )
        if not isinstance(applied_fingerprints, list) or not all(
            Recorder._is_sha256_hmac(item) for item in applied_fingerprints
        ):
            raise InvalidRecorderInput(
                "applied patch fingerprints must be SHA-256 HMAC strings"
            )
        if not isinstance(applied_counts, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in applied_counts
        ):
            raise InvalidRecorderInput(
                "applied patch counts must be positive integer values"
            )
        if not isinstance(applied_line_fingerprints, list) or not all(
            isinstance(block, list)
            and all(Recorder._is_sha256_hmac(item) for item in block)
            for block in applied_line_fingerprints
        ):
            raise InvalidRecorderInput(
                "applied patch line fingerprints must be nested SHA-256 HMAC strings"
            )
        if not (
            len(applied_fingerprints)
            == len(applied_counts)
            == len(applied_line_fingerprints)
            and all(
                count == len(block)
                for count, block in zip(
                    applied_counts,
                    applied_line_fingerprints,
                    strict=True,
                )
            )
        ):
            raise InvalidRecorderInput(
                "applied patch evidence block counts do not match"
            )
        if (
            not isinstance(applied_normalized_line_count, int)
            or isinstance(applied_normalized_line_count, bool)
            or applied_normalized_line_count < 0
            or applied_normalized_line_count != sum(applied_counts)
        ):
            raise InvalidRecorderInput(
                "applied patch normalized line count does not match"
            )
        if (
            not isinstance(applied_normalized_token_count, int)
            or isinstance(applied_normalized_token_count, bool)
            or applied_normalized_token_count < 0
        ):
            raise InvalidRecorderInput(
                "applied patch normalized token count must be a non-negative integer"
            )
        return {
            "applied_patch_counts": applied_counts,
            "applied_patch_fingerprints": applied_fingerprints,
            "applied_patch_line_fingerprints": applied_line_fingerprints,
            "applied_patch_normalized_line_count": applied_normalized_line_count,
            "applied_patch_normalized_token_count": (
                applied_normalized_token_count
            ),
            "counts": counts,
            "fingerprints": fingerprints,
            "line_fingerprints": line_fingerprints,
            "normalized_line_count": normalized_line_count,
            "normalized_token_count": normalized_token_count,
        }

    @staticmethod
    def _is_sha256_hmac(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _claim_terminal(
        self,
        transaction_id: str,
        operation: str,
    ) -> _ActiveTransaction | dict[str, object]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT transaction_id, repo_id, session_id, started_at,
                       snapshot_json, prompt_evidence_json,
                       terminal_state, terminal_operation
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
            if row is None:
                completed = self._completed_result(
                    connection,
                    transaction_id,
                    operation,
                )
                connection.commit()
                if completed is not None:
                    return completed
                raise InvalidRecorderInput(
                    "transaction does not exist for this repository"
                )

            state = str(row[6] or "active")
            claimed_operation = row[7]
            if state == "active":
                connection.execute(
                    """
                    UPDATE active_transactions
                    SET terminal_state = 'claimed', terminal_operation = ?
                    WHERE transaction_id = ? AND repo_id = ?
                    """,
                    (operation, transaction_id, self.repo_id),
                )
            elif state != "claimed":
                raise RecorderStateError("active transaction terminal state is corrupt")
            elif claimed_operation != operation:
                connection.commit()
                return {
                    "ok": False,
                    "status": "unavailable",
                    "error": "TERMINAL_OPERATION_IN_PROGRESS",
                    "event_ids": [],
                    "counts": {},
                }
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._active_from_row(row)

    def _active_from_row(self, row: tuple[object, ...]) -> _ActiveTransaction:
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

    def _repair_started_event(self, transaction_id: str) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT started_event_json
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return
        if row[0] is None:
            return
        if not isinstance(row[0], str):
            raise RecorderStateError("transaction start event is corrupt")
        self._append_and_enqueue(
            self._decode_event(row[0], "transaction start event is corrupt")
        )

    def _store_or_load_terminal_plan(
        self,
        transaction_id: str,
        operation: str,
        proposed: _TerminalPlan,
    ) -> _TerminalPlan | dict[str, object]:
        encoded = canonical_json(
            {
                "events": [asdict(event) for event in proposed.events],
                "result": proposed.result,
            }
        ).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT terminal_operation, terminal_plan_json
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
            if row is None:
                completed = self._completed_result(
                    connection,
                    transaction_id,
                    operation,
                )
                connection.commit()
                if completed is not None:
                    return completed
                raise RecorderStateError("claimed transaction disappeared")
            if row[0] != operation:
                connection.commit()
                return {
                    "ok": False,
                    "status": "unavailable",
                    "error": "TERMINAL_OPERATION_IN_PROGRESS",
                    "event_ids": [],
                    "counts": {},
                }
            if row[1] is None:
                connection.execute(
                    """
                    UPDATE active_transactions
                    SET terminal_plan_json = ?
                    WHERE transaction_id = ? AND repo_id = ?
                    """,
                    (encoded, transaction_id, self.repo_id),
                )
                selected = proposed
            elif isinstance(row[1], str):
                selected = self._decode_terminal_plan(row[1])
            else:
                raise RecorderStateError("terminal plan state is corrupt")
            connection.commit()
            return selected
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load_terminal_plan(
        self,
        transaction_id: str,
        operation: str,
    ) -> _TerminalPlan | dict[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT terminal_operation, terminal_plan_json
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
            if row is None:
                completed = self._completed_result(
                    connection,
                    transaction_id,
                    operation,
                )
                if completed is None:
                    raise RecorderStateError("claimed transaction disappeared")
                return completed
            if row[0] != operation:
                return {
                    "ok": False,
                    "status": "unavailable",
                    "error": "TERMINAL_OPERATION_IN_PROGRESS",
                    "event_ids": [],
                    "counts": {},
                }
            if row[1] is None:
                return None
            if not isinstance(row[1], str):
                raise RecorderStateError("terminal plan state is corrupt")
            return self._decode_terminal_plan(row[1])
        finally:
            connection.close()

    def _execute_terminal_plan(
        self,
        transaction_id: str,
        operation: str,
        plan: _TerminalPlan,
    ) -> dict[str, object]:
        for event in plan.events:
            self._append_and_enqueue(event)
        return self._complete_terminal(
            transaction_id,
            operation,
            plan.result,
        )

    def _complete_terminal(
        self,
        transaction_id: str,
        operation: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        encoded_result = canonical_json(result).decode("utf-8")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT terminal_operation
                FROM active_transactions
                WHERE transaction_id = ? AND repo_id = ?
                """,
                (transaction_id, self.repo_id),
            ).fetchone()
            if row is None:
                completed = self._completed_result(
                    connection,
                    transaction_id,
                    operation,
                )
                connection.commit()
                if completed is None:
                    raise RecorderStateError("terminal transaction disappeared")
                return completed
            if row[0] != operation:
                raise RecorderStateError("terminal operation changed concurrently")
            connection.execute(
                """
                INSERT INTO completed_transactions (
                    transaction_id, repo_id, operation, result_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO NOTHING
                """,
                (transaction_id, self.repo_id, operation, encoded_result),
            )
            cursor = connection.execute(
                "DELETE FROM active_transactions "
                "WHERE transaction_id = ? AND repo_id = ?",
                (transaction_id, self.repo_id),
            )
            if cursor.rowcount != 1:
                raise RecorderStateError("active transaction changed concurrently")
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _completed_result(
        self,
        connection: sqlite3.Connection,
        transaction_id: str,
        requested_operation: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT operation, result_json
            FROM completed_transactions
            WHERE transaction_id = ? AND repo_id = ?
            """,
            (transaction_id, self.repo_id),
        ).fetchone()
        if row is None:
            return None
        if not isinstance(row[0], str) or not isinstance(row[1], str):
            raise RecorderStateError("completed transaction result is corrupt")
        if row[0] != requested_operation:
            return {
                "ok": False,
                "status": "unavailable",
                "error": "TERMINAL_OPERATION_MISMATCH",
                "winning_operation": row[0],
            }
        try:
            result = json.loads(row[1])
        except json.JSONDecodeError as exc:
            raise RecorderStateError("completed transaction result is corrupt") from exc
        if not isinstance(result, dict):
            raise RecorderStateError("completed transaction result is corrupt")
        return result

    @staticmethod
    def _decode_terminal_plan(encoded: str) -> _TerminalPlan:
        try:
            parsed = json.loads(encoded)
            events = tuple(Event(**event) for event in parsed["events"])
            result = parsed["result"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecorderStateError("terminal event plan is corrupt") from exc
        if not isinstance(result, dict):
            raise RecorderStateError("terminal event plan result is corrupt")
        return _TerminalPlan(events, result)

    @staticmethod
    def _decode_event(encoded: str, message: str) -> Event:
        try:
            parsed = json.loads(encoded)
            return Event(**parsed)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecorderStateError(message) from exc

    @staticmethod
    def _decode_result(encoded: str, message: str) -> dict[str, object]:
        try:
            parsed = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise RecorderStateError(message) from exc
        if not isinstance(parsed, dict):
            raise RecorderStateError(message)
        return parsed

    def _planned_event(
        self,
        active: _ActiveTransaction,
        event_type: str,
        payload: dict[str, object],
        discriminator: str,
    ) -> Event:
        event = Event.new(
            self.repo_id,
            active.session_id,
            event_type,
            payload,
        )
        deterministic_id = uuid5(
            _EVENT_NAMESPACE,
            f"{self.repo_id}:{active.transaction_id}:{discriminator}",
        )
        return replace(event, event_id=str(deterministic_id))

    def _classify(
        self,
        spans: Iterable[PatchSpan],
        before: GitSnapshot,
        prompt_metadata: dict[str, object],
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
            prompt_evidence=self._prompt_evidence(prompt_metadata),
            repository_blocks=tuple(repository_blocks),
            removed_blocks=removed_blocks,
        )
        applied_evidence = self._applied_patch_evidence(prompt_metadata)
        classified: list[PatchSpan] = []
        for span in spans_list:
            ranges = applied_evidence.matching_ranges(span.new_lines)
            if ranges == ((0, len(span.new_lines)),):
                classified.extend(self._classify_one_span(span, context))
                continue
            if not ranges or span.action is not ActionKind.ADDED:
                classified.append(self._unknown_classification(span))
                continue

            position = 0
            for start, end in ranges:
                if position < start:
                    classified.append(
                        self._unknown_classification(
                            self._added_subspan(span, position, start)
                        )
                    )
                classified.extend(
                    self._classify_one_span(
                        self._added_subspan(span, start, end),
                        context,
                    )
                )
                position = end
            if position < len(span.new_lines):
                classified.append(
                    self._unknown_classification(
                        self._added_subspan(span, position, len(span.new_lines))
                    )
                )
        return classified

    @staticmethod
    def _classify_one_span(
        span: PatchSpan,
        context: ClassificationContext,
    ) -> list[PatchSpan]:
        try:
            return classify_spans((span,), context)
        except Exception:
            return [Recorder._unknown_classification(span)]

    @staticmethod
    def _unknown_classification(span: PatchSpan) -> PatchSpan:
        return replace(
            span,
            classification=Classification.UNKNOWN,
            confidence=0.0,
        )

    @staticmethod
    def _added_subspan(span: PatchSpan, start: int, end: int) -> PatchSpan:
        return replace(
            span,
            new_start=span.new_start + start,
            new_end=span.new_start + end,
            new_lines=span.new_lines[start:end],
        )

    def _prompt_evidence(
        self,
        metadata: dict[str, object],
    ) -> PromptEvidence:
        if not metadata:
            return PromptEvidence((), (), self._prompt_hmac_key, ())
        try:
            fingerprints = tuple(metadata["fingerprints"])
            counts = tuple(metadata["counts"])
            line_fingerprints = tuple(
                tuple(block) for block in metadata["line_fingerprints"]
            )
        except (KeyError, TypeError) as exc:
            raise RecorderStateError("prompt evidence metadata is corrupt") from exc
        if not all(isinstance(item, str) for item in fingerprints):
            raise RecorderStateError("prompt evidence metadata is corrupt")
        if not all(isinstance(item, int) for item in counts):
            raise RecorderStateError("prompt evidence metadata is corrupt")
        if not all(
            all(isinstance(item, str) for item in block)
            for block in line_fingerprints
        ):
            raise RecorderStateError("prompt evidence metadata is corrupt")
        return PromptEvidence(
            fingerprints,
            counts,
            self._prompt_hmac_key,
            line_fingerprints,
        )

    def _applied_patch_evidence(
        self,
        metadata: dict[str, object],
    ) -> PromptEvidence:
        if not metadata:
            return PromptEvidence((), (), self._prompt_hmac_key, ())
        try:
            fingerprints = tuple(metadata["applied_patch_fingerprints"])
            counts = tuple(metadata["applied_patch_counts"])
            line_fingerprints = tuple(
                tuple(block)
                for block in metadata["applied_patch_line_fingerprints"]
            )
        except (KeyError, TypeError) as exc:
            raise RecorderStateError(
                "applied patch evidence metadata is corrupt"
            ) from exc
        if not all(isinstance(item, str) for item in fingerprints):
            raise RecorderStateError("applied patch evidence metadata is corrupt")
        if not all(isinstance(item, int) for item in counts):
            raise RecorderStateError("applied patch evidence metadata is corrupt")
        if not all(
            all(isinstance(item, str) for item in block)
            for block in line_fingerprints
        ):
            raise RecorderStateError("applied patch evidence metadata is corrupt")
        return PromptEvidence(
            fingerprints,
            counts,
            self._prompt_hmac_key,
            line_fingerprints,
        )

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
        connection = self._connect()
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
        connection = self._connect()
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
