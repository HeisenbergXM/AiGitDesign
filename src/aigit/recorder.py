"""Transaction coordinator for local AI contribution provenance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
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


@dataclass(frozen=True)
class _ProposedHunk:
    action: ActionKind
    path_hmac: str
    old_path_hmac: str | None
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_line_fingerprints: tuple[str, ...]
    new_line_fingerprints: tuple[str, ...]


@dataclass(frozen=True)
class _TerminalClaim:
    active: _ActiveTransaction
    fresh: bool
    token: str | None
    generation: int


SQLITE_TIMEOUT_SECONDS = 0.2
SQLITE_INITIALIZATION_TIMEOUT_SECONDS = 0.001
TERMINAL_CLAIM_LEASE_SECONDS = 5
_EVENT_NAMESPACE = UUID("7a207a4d-e4df-49a6-a976-75ef255f33aa")
_PROMPT_EVIDENCE_KEYS = frozenset(
    {
        "fingerprints",
        "counts",
        "normalized_line_count",
        "normalized_token_count",
        "line_fingerprints",
        "proposed_patch_hunks",
    }
)
_PROPOSED_HUNK_KEYS = frozenset(
    {
        "action",
        "path_hmac",
        "old_path_hmac",
        "old_start",
        "old_end",
        "new_start",
        "new_end",
        "old_line_fingerprints",
        "new_line_fingerprints",
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
        state_path = root / self.repo_id.removeprefix("sha256:")
        self._probe_existing_database(state_path / "state.sqlite3")
        self._sqlite_timeout = SQLITE_INITIALIZATION_TIMEOUT_SECONDS
        self.store = LocalStore(
            state_path,
            connection_timeout=SQLITE_INITIALIZATION_TIMEOUT_SECONDS,
        )
        self._migrate_active_transactions()
        self._sqlite_timeout = SQLITE_TIMEOUT_SECONDS
        self.store.connection_timeout = SQLITE_TIMEOUT_SECONDS
        self._prompt_hmac_key = self._load_or_create_prompt_hmac_key()

    @staticmethod
    def _probe_existing_database(database_path: Path) -> None:
        if not database_path.is_file():
            return
        connection = sqlite3.connect(database_path, timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()

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
        self._repair_started_event(transaction_id)
        claimed = self._claim_terminal(
            transaction_id,
            operation,
            persist_fallback=True,
        )
        if isinstance(claimed, dict):
            return claimed
        active = claimed.active
        if not claimed.fresh:
            existing_plan = self._load_terminal_plan(transaction_id, operation)
            if isinstance(existing_plan, dict):
                return existing_plan
            if existing_plan is None:
                raise RecorderStateError("claimed end has no terminal plan")
            return self._execute_terminal_plan(
                transaction_id, operation, existing_plan
            )
        try:
            after = capture_snapshot(self.repository, self.store)
        except Exception:
            return self._degrade_terminal(
                claimed,
                operation,
                "CAPTURE_FAILED",
            )
        try:
            raw_spans = diff_snapshots(active.snapshot, after, self.store)
        except Exception:
            return self._degrade_terminal(
                claimed,
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
        prepared = self._replace_terminal_fallback(
            active.transaction_id,
            operation,
            _TerminalPlan(tuple(events), result),
            "exact",
            claimed.token,
            claimed.generation,
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
        active = claimed.active
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
        claim: _TerminalClaim,
        operation: str,
        reason_code: str,
    ) -> dict[str, object]:
        active = claim.active
        if reason_code == "CAPTURE_FAILED":
            prepared = self._load_terminal_plan(active.transaction_id, operation)
        else:
            proposed = self._degradation_plan(active, reason_code)
            try:
                prepared = self._replace_terminal_fallback(
                    active.transaction_id,
                    operation,
                    proposed,
                    "degraded",
                    claim.token,
                    claim.generation,
                )
            except sqlite3.DatabaseError:
                prepared = self._load_terminal_plan(active.transaction_id, operation)
        if isinstance(prepared, dict):
            return prepared
        if prepared is None:
            raise RecorderStateError("claimed end has no fallback plan")
        return self._execute_terminal_plan(
            active.transaction_id,
            operation,
            prepared,
        )

    def _degradation_plan(
        self,
        active: _ActiveTransaction,
        reason_code: str,
    ) -> _TerminalPlan:
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
        return _TerminalPlan((recovery,), result)

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
            if "terminal_plan_kind" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_plan_kind TEXT"
                )
            if "terminal_claim_expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_claim_expires_at TEXT"
                )
            if "terminal_claim_token" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_claim_token TEXT"
                )
            if "terminal_claim_generation" not in columns:
                connection.execute(
                    "ALTER TABLE active_transactions "
                    "ADD COLUMN terminal_claim_generation "
                    "INTEGER NOT NULL DEFAULT 0"
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
        proposed_hunks = parsed["proposed_patch_hunks"]
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
        if not isinstance(proposed_hunks, list):
            raise InvalidRecorderInput("proposed_patch_hunks must be a list")
        normalized_hunks = [
            Recorder._validate_proposed_hunk(item) for item in proposed_hunks
        ]
        return {
            "counts": counts,
            "fingerprints": fingerprints,
            "line_fingerprints": line_fingerprints,
            "normalized_line_count": normalized_line_count,
            "normalized_token_count": normalized_token_count,
            "proposed_patch_hunks": normalized_hunks,
        }

    @staticmethod
    def _validate_proposed_hunk(value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != _PROPOSED_HUNK_KEYS:
            raise InvalidRecorderInput(
                "each proposed patch hunk must contain only the strict hunk schema"
            )
        action_value = value["action"]
        try:
            action = ActionKind(action_value)
        except (TypeError, ValueError) as exc:
            raise InvalidRecorderInput("proposed hunk action is invalid") from exc

        path_hmac = value["path_hmac"]
        old_path_hmac = value["old_path_hmac"]
        if not Recorder._is_sha256_hmac(path_hmac):
            raise InvalidRecorderInput("proposed hunk path_hmac is invalid")
        if old_path_hmac is not None and not Recorder._is_sha256_hmac(old_path_hmac):
            raise InvalidRecorderInput("proposed hunk old_path_hmac is invalid")

        coordinates: dict[str, int] = {}
        for name in ("old_start", "old_end", "new_start", "new_end"):
            coordinate = value[name]
            if (
                not isinstance(coordinate, int)
                or isinstance(coordinate, bool)
                or coordinate < 0
            ):
                raise InvalidRecorderInput("proposed hunk coordinates are invalid")
            coordinates[name] = coordinate
        if (
            coordinates["old_end"] < coordinates["old_start"]
            or coordinates["new_end"] < coordinates["new_start"]
        ):
            raise InvalidRecorderInput("proposed hunk ranges are invalid")

        old_fingerprints = Recorder._validate_hunk_fingerprints(
            value["old_line_fingerprints"],
            "old",
        )
        new_fingerprints = Recorder._validate_hunk_fingerprints(
            value["new_line_fingerprints"],
            "new",
        )
        old_count = coordinates["old_end"] - coordinates["old_start"]
        new_count = coordinates["new_end"] - coordinates["new_start"]
        if old_count != len(old_fingerprints) or new_count != len(new_fingerprints):
            raise InvalidRecorderInput(
                "proposed hunk ranges must match line fingerprint counts"
            )

        if action is ActionKind.ADDED:
            valid_shape = old_count == 0 and new_count > 0 and old_path_hmac is None
        elif action is ActionKind.DELETED:
            valid_shape = old_count > 0 and new_count == 0 and old_path_hmac is None
        elif action is ActionKind.MOVED:
            valid_shape = old_count > 0 and new_count > 0 and old_path_hmac is not None
        else:
            valid_shape = old_count > 0 and new_count > 0 and old_path_hmac is None
        if not valid_shape:
            raise InvalidRecorderInput(
                "proposed hunk action is inconsistent with its paths and ranges"
            )
        return {
            "action": action.value,
            "path_hmac": path_hmac,
            "old_path_hmac": old_path_hmac,
            **coordinates,
            "old_line_fingerprints": old_fingerprints,
            "new_line_fingerprints": new_fingerprints,
        }

    @staticmethod
    def _validate_hunk_fingerprints(value: object, side: str) -> list[str]:
        if not isinstance(value, list) or not all(
            Recorder._is_sha256_hmac(item) for item in value
        ):
            raise InvalidRecorderInput(
                f"proposed hunk {side} line fingerprints are invalid"
            )
        return list(value)

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
        *,
        persist_fallback: bool = False,
    ) -> _TerminalClaim | dict[str, object]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT transaction_id, repo_id, session_id, started_at,
                       snapshot_json, prompt_evidence_json,
                       terminal_state, terminal_operation, terminal_plan_kind,
                       terminal_claim_expires_at, terminal_claim_token,
                       terminal_claim_generation
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
            active = self._active_from_row(row)
            fresh = False
            claim_token: str | None = None
            claim_generation = 0
            if state == "active":
                fresh = True
                if persist_fallback:
                    if (
                        not isinstance(row[11], int)
                        or isinstance(row[11], bool)
                        or row[11] < 0
                    ):
                        raise RecorderStateError(
                            "terminal claim generation is corrupt"
                        )
                    claim_token = uuid4().hex
                    claim_generation = row[11] + 1
                    fallback = self._degradation_plan(active, "CAPTURE_FAILED")
                    connection.execute(
                        """
                        UPDATE active_transactions
                        SET terminal_state = 'claimed',
                            terminal_operation = ?,
                            terminal_plan_json = ?,
                            terminal_plan_kind = 'fallback',
                            terminal_claim_expires_at = ?,
                            terminal_claim_token = ?,
                            terminal_claim_generation = ?
                        WHERE transaction_id = ? AND repo_id = ?
                        """,
                        (
                            operation,
                            self._encode_terminal_plan(fallback),
                            self._new_terminal_claim_expiry(),
                            claim_token,
                            claim_generation,
                            transaction_id,
                            self.repo_id,
                        ),
                    )
                else:
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
            elif persist_fallback and row[8] == "fallback":
                current_token, current_generation = self._terminal_claim_identity(
                    row[10],
                    row[11],
                )
                if not self._terminal_claim_lease_expired(row[9]):
                    connection.commit()
                    return {
                        "ok": False,
                        "status": "unavailable",
                        "error": "TERMINAL_OPERATION_IN_PROGRESS",
                        "event_ids": [],
                        "counts": {},
                    }
                claim_token = uuid4().hex
                claim_generation = current_generation + 1
                cursor = connection.execute(
                    """
                    UPDATE active_transactions
                    SET terminal_claim_expires_at = ?,
                        terminal_claim_token = ?,
                        terminal_claim_generation = ?
                    WHERE transaction_id = ? AND repo_id = ?
                      AND terminal_state = 'claimed'
                      AND terminal_operation = ?
                      AND terminal_plan_kind = 'fallback'
                      AND terminal_claim_expires_at = ?
                      AND terminal_claim_token = ?
                      AND terminal_claim_generation = ?
                    """,
                    (
                        self._new_terminal_claim_expiry(),
                        claim_token,
                        claim_generation,
                        transaction_id,
                        self.repo_id,
                        operation,
                        row[9],
                        current_token,
                        current_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RecorderStateError(
                        "terminal fallback claim changed concurrently"
                    )
            else:
                if isinstance(row[10], str):
                    claim_token = row[10]
                if isinstance(row[11], int) and not isinstance(row[11], bool):
                    claim_generation = row[11]
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return _TerminalClaim(active, fresh, claim_token, claim_generation)

    @staticmethod
    def _new_terminal_claim_expiry() -> str:
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=TERMINAL_CLAIM_LEASE_SECONDS)
        ).isoformat()

    @staticmethod
    def _terminal_claim_lease_expired(value: object) -> bool:
        if not isinstance(value, str) or not value:
            raise RecorderStateError("terminal fallback claim lease is corrupt")
        try:
            expires_at = datetime.fromisoformat(value)
        except ValueError as exc:
            raise RecorderStateError(
                "terminal fallback claim lease is corrupt"
            ) from exc
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise RecorderStateError("terminal fallback claim lease is corrupt")
        return expires_at <= datetime.now(timezone.utc)

    @staticmethod
    def _terminal_claim_identity(token: object, generation: object) -> tuple[str, int]:
        if not isinstance(token, str) or not token:
            raise RecorderStateError("terminal fallback claim identity is corrupt")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
        ):
            raise RecorderStateError("terminal fallback claim identity is corrupt")
        return token, generation

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
        encoded = self._encode_terminal_plan(proposed)
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
                    SET terminal_plan_json = ?, terminal_plan_kind = 'exact'
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

    def _replace_terminal_fallback(
        self,
        transaction_id: str,
        operation: str,
        proposed: _TerminalPlan,
        plan_kind: str,
        expected_token: str | None,
        expected_generation: int,
    ) -> _TerminalPlan | dict[str, object]:
        encoded = self._encode_terminal_plan(proposed)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT terminal_operation, terminal_plan_json, terminal_plan_kind,
                       terminal_claim_token, terminal_claim_generation
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
            if row[2] == "fallback":
                current_token, current_generation = self._terminal_claim_identity(
                    row[3],
                    row[4],
                )
                if (
                    expected_token != current_token
                    or expected_generation != current_generation
                ):
                    if not isinstance(row[1], str):
                        raise RecorderStateError("terminal plan state is corrupt")
                    selected = self._decode_terminal_plan(row[1])
                else:
                    cursor = connection.execute(
                        """
                        UPDATE active_transactions
                        SET terminal_plan_json = ?, terminal_plan_kind = ?,
                            terminal_claim_expires_at = NULL
                        WHERE transaction_id = ? AND repo_id = ?
                          AND terminal_operation = ?
                          AND terminal_plan_kind = 'fallback'
                          AND terminal_claim_token = ?
                          AND terminal_claim_generation = ?
                        """,
                        (
                            encoded,
                            plan_kind,
                            transaction_id,
                            self.repo_id,
                            operation,
                            expected_token,
                            expected_generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RecorderStateError(
                            "terminal fallback claim changed concurrently"
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

    @staticmethod
    def _encode_terminal_plan(plan: _TerminalPlan) -> str:
        return canonical_json(
            {
                "events": [asdict(event) for event in plan.events],
                "result": plan.result,
            }
        ).decode("utf-8")

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
        proposed_hunks = self._proposed_hunks(prompt_metadata)
        spans_list = self._coalesce_evidenced_moves(list(spans), proposed_hunks)
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
        consumed_hunks: set[int] = set()
        classified: list[PatchSpan] = []
        for span in spans_list:
            if span.action is ActionKind.ADDED:
                classified.extend(
                    self._classify_added_span(
                        span,
                        context,
                        proposed_hunks,
                        consumed_hunks,
                    )
                )
                continue
            candidates = [
                index
                for index, hunk in enumerate(proposed_hunks)
                if index not in consumed_hunks
                and self._hunk_matches_span(hunk, span)
            ]
            if len(candidates) != 1:
                classified.append(self._unknown_classification(span))
                continue
            selected_hunk = proposed_hunks[candidates[0]]
            consumed_hunks.add(candidates[0])
            if span.action is ActionKind.DELETED:
                classified.append(
                    replace(
                        span,
                        classification=Classification.LEGACY_UNKNOWN,
                        confidence=1.0,
                    )
                )
            else:
                authorized = self._classify_one_span(span, context)
                if selected_hunk.action is ActionKind.FORMATTED and any(
                    item.action is not ActionKind.FORMATTED for item in authorized
                ):
                    classified.append(self._unknown_classification(span))
                else:
                    classified.extend(authorized)
        return classified

    def _classify_added_span(
        self,
        span: PatchSpan,
        context: ClassificationContext,
        hunks: tuple[_ProposedHunk, ...],
        consumed: set[int],
    ) -> list[PatchSpan]:
        matched: list[tuple[int, int, int]] = []
        for index, hunk in enumerate(hunks):
            if index in consumed:
                continue
            matched_range = self._added_hunk_range(span, hunk)
            if matched_range is not None:
                matched.append((*matched_range, index))
        if not matched:
            return [self._unknown_classification(span)]
        consumed.update(index for _, _, index in matched)

        coverage = [0] * len(span.new_lines)
        for start, end, _ in matched:
            for position in range(start, end):
                coverage[position] += 1

        result: list[PatchSpan] = []
        start = 0
        while start < len(coverage):
            authorized = coverage[start] == 1
            end = start + 1
            while end < len(coverage) and (coverage[end] == 1) == authorized:
                end += 1
            subspan = self._added_subspan(span, start, end)
            if authorized:
                result.extend(self._classify_one_span(subspan, context))
            else:
                result.append(self._unknown_classification(subspan))
            start = end
        return result

    def _added_hunk_range(
        self,
        span: PatchSpan,
        hunk: _ProposedHunk,
    ) -> tuple[int, int] | None:
        if (
            hunk.action is not ActionKind.ADDED
            or not self._path_matches(span.path, hunk.path_hmac)
            or hunk.old_start != span.old_start
            or hunk.old_end != span.old_end
            or hunk.new_start < span.new_start
            or hunk.new_end > span.new_end
        ):
            return None
        start = hunk.new_start - span.new_start
        end = hunk.new_end - span.new_start
        if not self._lines_match(
            span.new_lines[start:end],
            hunk.new_line_fingerprints,
        ):
            return None
        return start, end

    def _hunk_matches_span(self, hunk: _ProposedHunk, span: PatchSpan) -> bool:
        if hunk.action is ActionKind.MOVED and (
            hunk.old_end - hunk.old_start != hunk.new_end - hunk.new_start
            or hunk.old_line_fingerprints != hunk.new_line_fingerprints
        ):
            return False
        action_matches = hunk.action is span.action or (
            hunk.action is ActionKind.FORMATTED
            and span.action is ActionKind.REPLACED
        )
        if (
            not action_matches
            or not self._path_matches(span.path, hunk.path_hmac)
            or hunk.old_start != span.old_start
            or hunk.old_end != span.old_end
            or hunk.new_start != span.new_start
            or hunk.new_end != span.new_end
        ):
            return False
        if hunk.old_path_hmac is None:
            if span.old_path is not None:
                return False
        elif span.old_path is None or not self._path_matches(
            span.old_path,
            hunk.old_path_hmac,
        ):
            return False
        return self._lines_match(
            span.old_lines,
            hunk.old_line_fingerprints,
        ) and self._lines_match(
            span.new_lines,
            hunk.new_line_fingerprints,
        )

    def _coalesce_evidenced_moves(
        self,
        spans: list[PatchSpan],
        hunks: tuple[_ProposedHunk, ...],
    ) -> list[PatchSpan]:
        replacements: dict[int, PatchSpan] = {}
        consumed_span_indexes: set[int] = set()
        for hunk in hunks:
            if (
                hunk.action is not ActionKind.MOVED
                or hunk.old_end - hunk.old_start
                != hunk.new_end - hunk.new_start
                or hunk.old_line_fingerprints != hunk.new_line_fingerprints
            ):
                continue
            deleted = [
                index
                for index, span in enumerate(spans)
                if index not in consumed_span_indexes
                and span.action is ActionKind.DELETED
                and hunk.old_path_hmac is not None
                and self._path_matches(span.path, hunk.old_path_hmac)
                and span.old_start == hunk.old_start
                and span.old_end == hunk.old_end
                and self._lines_match(
                    span.old_lines,
                    hunk.old_line_fingerprints,
                )
            ]
            added = [
                index
                for index, span in enumerate(spans)
                if index not in consumed_span_indexes
                and span.action is ActionKind.ADDED
                and self._path_matches(span.path, hunk.path_hmac)
                and span.new_start == hunk.new_start
                and span.new_end == hunk.new_end
                and self._lines_match(
                    span.new_lines,
                    hunk.new_line_fingerprints,
                )
            ]
            if len(deleted) != 1 or len(added) != 1:
                continue
            deleted_index, added_index = deleted[0], added[0]
            source, destination = spans[deleted_index], spans[added_index]
            combined = PatchSpan(
                path=destination.path,
                old_path=source.path,
                old_start=source.old_start,
                old_end=source.old_end,
                new_start=destination.new_start,
                new_end=destination.new_end,
                old_lines=source.old_lines,
                new_lines=destination.new_lines,
                classification=Classification.UNKNOWN,
                action=ActionKind.MOVED,
                confidence=0.0,
            )
            anchor = min(deleted_index, added_index)
            replacements[anchor] = combined
            consumed_span_indexes.update((deleted_index, added_index))
        return [
            replacements[index]
            if index in replacements
            else span
            for index, span in enumerate(spans)
            if index not in consumed_span_indexes or index in replacements
        ]

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

    def _proposed_hunks(
        self,
        metadata: dict[str, object],
    ) -> tuple[_ProposedHunk, ...]:
        if not metadata:
            return ()
        try:
            raw_hunks = metadata["proposed_patch_hunks"]
            if not isinstance(raw_hunks, list):
                raise TypeError
            normalized = [self._validate_proposed_hunk(item) for item in raw_hunks]
            return tuple(
                _ProposedHunk(
                    action=ActionKind(item["action"]),
                    path_hmac=str(item["path_hmac"]),
                    old_path_hmac=(
                        str(item["old_path_hmac"])
                        if item["old_path_hmac"] is not None
                        else None
                    ),
                    old_start=int(item["old_start"]),
                    old_end=int(item["old_end"]),
                    new_start=int(item["new_start"]),
                    new_end=int(item["new_end"]),
                    old_line_fingerprints=tuple(item["old_line_fingerprints"]),
                    new_line_fingerprints=tuple(item["new_line_fingerprints"]),
                )
                for item in normalized
            )
        except (InvalidRecorderInput, KeyError, TypeError, ValueError) as exc:
            raise RecorderStateError("proposed patch hunk metadata is corrupt") from exc

    def _path_matches(self, path: str, expected_hmac: str) -> bool:
        normalized = path.replace("\\", "/")
        actual = hmac.new(
            self._prompt_hmac_key,
            b"path\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(actual, expected_hmac)

    def _lines_match(
        self,
        lines: Iterable[str],
        expected_hmacs: tuple[str, ...],
    ) -> bool:
        materialized = tuple(lines)
        if len(materialized) != len(expected_hmacs):
            return False
        return all(
            hmac.compare_digest(
                hmac.new(
                    self._prompt_hmac_key,
                    b"line\0" + line.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest(),
                expected,
            )
            for line, expected in zip(materialized, expected_hmacs, strict=True)
        )

    @staticmethod
    def _span_counts(spans: Iterable[PatchSpan]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for span in spans:
            if span.action in {
                ActionKind.DELETED,
                ActionKind.MOVED,
                ActionKind.FORMATTED,
            }:
                continue
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
        span_evidence: list[dict[str, object]] = []
        for span in spans:
            evidence: dict[str, object] = {
                "action": span.action.value,
                "classification": span.classification.value,
                "confidence": span.confidence,
                "old_start": span.old_start,
                "old_end": span.old_end,
                "new_start": span.new_start,
                "new_end": span.new_end,
            }
            if span.action in {
                ActionKind.DELETED,
                ActionKind.MOVED,
                ActionKind.FORMATTED,
            } and span.confidence == 1.0:
                evidence["edit_actor"] = "AI"
            if span.action is ActionKind.MOVED and span.old_path is not None:
                evidence["old_path_hmac"] = hmac.new(
                    self._prompt_hmac_key,
                    b"path\0" + span.old_path.replace("\\", "/").encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
            span_evidence.append(evidence)
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
