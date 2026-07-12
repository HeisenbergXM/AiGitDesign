import json
import math
import sqlite3
from dataclasses import replace

import pytest

from aigit.canonical import canonical_json, hash_bytes
from aigit.domain import Event
from aigit.local_store import BlobIntegrityError, EventCollisionError, LocalStore


def test_append_builds_a_verifiable_chain(tmp_path) -> None:
    store = LocalStore(tmp_path)
    first = store.append(Event.new("repo-1", "s-1", "session_started", {}))
    second = store.append(
        Event.new("repo-1", "s-1", "heartbeat", {"healthy": True})
    )
    assert second.previous_event_hash == first.event_hash
    assert store.verify_chain() == []


def test_tampering_is_reported(tmp_path) -> None:
    store = LocalStore(tmp_path)
    store.append(Event.new("repo-1", "s-1", "heartbeat", {"healthy": True}))
    line = json.loads(
        store.ledger_path.read_text(encoding="utf-8").splitlines()[0]
    )
    line["payload"]["healthy"] = False
    store.ledger_path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert store.verify_chain() == [line["event_id"]]


def test_canonical_json_is_compact_sorted_utf8_and_rejects_nan() -> None:
    assert canonical_json({"z": "café", "a": [2, 1]}) == b'{"a":[2,1],"z":"caf\xc3\xa9"}'
    with pytest.raises(ValueError):
        canonical_json({"invalid": math.nan})


def test_hash_bytes_returns_prefixed_sha256() -> None:
    assert hash_bytes(b"evidence") == (
        "sha256:ee8250fb76e094b34b471f13a73dbbe51d1ae142e9df59d7c0d31ec20f0a0a8e"
    )


def test_blobs_are_content_addressed_and_retrievable(tmp_path) -> None:
    store = LocalStore(tmp_path)
    digest = store.put_blob(b"evidence")
    hexadecimal = digest.removeprefix("sha256:")

    assert (tmp_path / "blobs" / hexadecimal[:2] / hexadecimal[2:]).is_file()
    assert store.get_blob(digest) == b"evidence"


def test_put_blob_atomically_replaces_corrupt_existing_content(tmp_path) -> None:
    store = LocalStore(tmp_path)
    digest = hash_bytes(b"evidence")
    hexadecimal = digest.removeprefix("sha256:")
    destination = tmp_path / "blobs" / hexadecimal[:2] / hexadecimal[2:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"corrupt")

    assert store.put_blob(b"evidence") == digest
    assert destination.read_bytes() == b"evidence"


def test_get_blob_rejects_content_with_a_mismatched_digest(tmp_path) -> None:
    store = LocalStore(tmp_path)
    digest = store.put_blob(b"evidence")
    hexadecimal = digest.removeprefix("sha256:")
    destination = tmp_path / "blobs" / hexadecimal[:2] / hexadecimal[2:]
    destination.write_bytes(b"corrupt")

    with pytest.raises(BlobIntegrityError, match="digest mismatch"):
        store.get_blob(digest)


def test_sqlite_index_has_required_tables(tmp_path) -> None:
    store = LocalStore(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"sequences", "active_transactions", "upload_queue"} <= names


def test_sqlite_sequence_index_rebuilds_from_the_ledger(tmp_path) -> None:
    store = LocalStore(tmp_path)
    first = store.append(Event.new("repo-1", "s-1", "session_started", {}))
    store.database_path.unlink()

    rebuilt = LocalStore(tmp_path)
    second = rebuilt.append(Event.new("repo-1", "s-1", "heartbeat", {}))

    assert second.sequence == 2
    assert second.previous_event_hash == first.event_hash
    assert rebuilt.verify_chain() == []


def test_retry_after_post_fsync_commit_failure_does_not_duplicate_event(
    tmp_path, monkeypatch
) -> None:
    store = LocalStore(tmp_path)
    event = Event.new("repo-1", "s-1", "heartbeat", {"healthy": True})
    real_connect = store._connect
    failure_injected = False

    class CommitFailingConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self) -> None:
            raise sqlite3.OperationalError("injected commit failure")

    def connect_with_one_commit_failure():
        nonlocal failure_injected
        connection = real_connect()
        if not failure_injected:
            failure_injected = True
            return CommitFailingConnection(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect_with_one_commit_failure)

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        store.append(event)

    retried = store.append(event)
    lines = store.ledger_path.read_text(encoding="utf-8").splitlines()

    assert retried.event_id == event.event_id
    assert retried.sequence == 1
    assert len(lines) == 1
    assert store.verify_chain() == []


def test_append_rejects_event_id_reused_with_different_request_content(
    tmp_path,
) -> None:
    store = LocalStore(tmp_path)
    event = Event.new("repo-1", "s-1", "heartbeat", {"healthy": True})
    store.append(event)

    with pytest.raises(EventCollisionError, match=event.event_id):
        store.append(replace(event, payload={"healthy": False}))

    assert len(store.ledger_path.read_text(encoding="utf-8").splitlines()) == 1
