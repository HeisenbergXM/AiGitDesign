"""Append-only local evidence storage backed by JSONL and SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator

from aigit.canonical import canonical_json, hash_bytes
from aigit.domain import Event


class BlobIntegrityError(RuntimeError):
    """Raised when blob bytes do not match their content-addressed digest."""


class EventCollisionError(ValueError):
    """Raised when an event ID is reused for different request content."""


def event_hash(event_dict: dict[str, object]) -> str:
    """Hash an event with its signature field cleared."""
    unsigned = dict(event_dict)
    unsigned["event_hash"] = ""
    return hash_bytes(canonical_json(unsigned))


class LocalStore:
    """Durable local ledger whose SQLite state can be rebuilt from JSONL."""

    def __init__(
        self,
        state_path: str | Path,
        connection_timeout: float = 30,
    ) -> None:
        if connection_timeout <= 0:
            raise ValueError("connection_timeout must be positive")
        self.state_path = Path(state_path)
        self.blobs_path = self.state_path / "blobs"
        self.ledger_path = self.state_path / "events.jsonl"
        self.database_path = self.state_path / "state.sqlite3"
        self.connection_timeout = connection_timeout

        self.blobs_path.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        self._rebuild_sequences()

    def append(self, event: Event) -> Event:
        """Allocate an event sequence, append it durably, and return it."""
        connection = self._connect()
        appended = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._find_event(event.event_id)
            if existing is not None:
                existing_request = canonical_json(self._request_content(existing))
                incoming_request = canonical_json(self._request_content(asdict(event)))
                if existing_request != incoming_request:
                    raise EventCollisionError(
                        f"event_id {event.event_id} already has different content"
                    )
                self._replace_sequences_from_ledger(connection)
                connection.commit()
                return Event(**existing)

            row = connection.execute(
                "SELECT sequence, event_hash FROM sequences WHERE repo_id = ?",
                (event.repo_id,),
            ).fetchone()
            if row is None:
                sequence = 1
                previous_event_hash = ""
            else:
                sequence = int(row[0]) + 1
                previous_event_hash = str(row[1])

            sequenced = replace(
                event,
                sequence=sequence,
                previous_event_hash=previous_event_hash,
                event_hash="",
            )
            unsigned = asdict(sequenced)
            hashed = replace(sequenced, event_hash=event_hash(unsigned))
            encoded = canonical_json(asdict(hashed)) + b"\n"

            connection.execute(
                """
                INSERT INTO sequences (repo_id, sequence, event_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    event_hash = excluded.event_hash
                """,
                (hashed.repo_id, hashed.sequence, hashed.event_hash),
            )
            with self.ledger_path.open("ab") as ledger:
                ledger.write(encoded)
                ledger.flush()
                os.fsync(ledger.fileno())
            appended = True
            connection.commit()
            return hashed
        except BaseException:
            connection.rollback()
            if appended:
                self._rebuild_sequences()
            raise
        finally:
            connection.close()

    def verify_chain(self) -> list[str]:
        """Return event IDs whose hash, predecessor, or sequence is invalid."""
        corrupt: list[str] = []
        previous_hashes: dict[str, str] = {}
        sequences: dict[str, int] = {}

        for record in self._ledger_records():
            repo_id = record.get("repo_id")
            event_id = record.get("event_id")
            stored_hash = record.get("event_hash")
            previous_hash = record.get("previous_event_hash")
            sequence = record.get("sequence")

            valid_identity = isinstance(repo_id, str) and isinstance(event_id, str)
            expected_previous = previous_hashes.get(repo_id, "") if valid_identity else ""
            expected_sequence = sequences.get(repo_id, 0) + 1 if valid_identity else 1

            try:
                calculated_hash = event_hash(record)
            except (TypeError, ValueError, UnicodeError):
                calculated_hash = None

            invalid = (
                not valid_identity
                or not isinstance(stored_hash, str)
                or stored_hash != calculated_hash
                or previous_hash != expected_previous
                or sequence != expected_sequence
            )
            if invalid:
                corrupt.append(event_id if isinstance(event_id, str) else "")

            if valid_identity:
                previous_hashes[repo_id] = stored_hash if isinstance(stored_hash, str) else ""
                sequences[repo_id] = sequence if isinstance(sequence, int) else expected_sequence

        return corrupt

    def put_blob(self, data: bytes) -> str:
        """Store bytes by SHA-256 digest and return that digest."""
        digest = hash_bytes(data)
        hexadecimal = digest.removeprefix("sha256:")
        destination = self.blobs_path / hexadecimal[:2] / hexadecimal[2:]
        destination.parent.mkdir(parents=True, exist_ok=True)

        corrupt_existing = False
        try:
            existing = destination.read_bytes()
        except FileNotFoundError:
            pass
        else:
            if hash_bytes(existing) == digest:
                return digest
            corrupt_existing = True

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as blob:
                blob.write(data)
                blob.flush()
                os.fsync(blob.fileno())
            os.replace(temporary_path, destination)
        except OSError as exc:
            if corrupt_existing:
                raise BlobIntegrityError(
                    f"could not replace corrupt blob {digest}"
                ) from exc
            raise
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return digest

    def get_blob(self, digest: str) -> bytes:
        """Retrieve bytes previously stored under a SHA-256 digest."""
        hexadecimal = self._digest_hexadecimal(digest)
        data = (self.blobs_path / hexadecimal[:2] / hexadecimal[2:]).read_bytes()
        actual = hash_bytes(data)
        if actual != digest:
            raise BlobIntegrityError(
                f"blob digest mismatch: expected {digest}, found {actual}"
            )
        return data

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.database_path,
            timeout=self.connection_timeout,
        )

    def _initialize_database(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sequences (
                    repo_id TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    event_hash TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS active_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    started_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_queue (
                    event_id TEXT PRIMARY KEY,
                    event_json BLOB NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT
                )
                """
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _rebuild_sequences(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_sequences_from_ledger(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _replace_sequences_from_ledger(
        self, connection: sqlite3.Connection
    ) -> None:
        connection.execute("DELETE FROM sequences")
        for record in self._ledger_records():
            repo_id = record.get("repo_id")
            sequence = record.get("sequence")
            digest = record.get("event_hash")
            if (
                not isinstance(repo_id, str)
                or not isinstance(sequence, int)
                or not isinstance(digest, str)
            ):
                continue
            connection.execute(
                """
                INSERT INTO sequences (repo_id, sequence, event_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    event_hash = excluded.event_hash
                """,
                (repo_id, sequence, digest),
            )

    def _find_event(self, event_id: str) -> dict[str, Any] | None:
        for record in self._ledger_records():
            if record.get("event_id") == event_id:
                return record
        return None

    @staticmethod
    def _request_content(event_dict: dict[str, Any]) -> dict[str, Any]:
        return {
            key: event_dict.get(key)
            for key in (
                "repo_id",
                "session_id",
                "event_type",
                "payload",
            )
        }

    def _ledger_records(self) -> Iterator[dict[str, Any]]:
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open("rb") as ledger:
            for line in ledger:
                if line.strip():
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("ledger entries must be JSON objects")
                    yield record

    @staticmethod
    def _digest_hexadecimal(digest: str) -> str:
        prefix = "sha256:"
        if not digest.startswith(prefix):
            raise ValueError("digest must use the sha256 prefix")
        hexadecimal = digest[len(prefix) :]
        if len(hexadecimal) != 64 or any(
            character not in "0123456789abcdef" for character in hexadecimal
        ):
            raise ValueError("digest must contain 64 lowercase hexadecimal characters")
        return hexadecimal
