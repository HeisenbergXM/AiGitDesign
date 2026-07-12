import json
import math
import sqlite3

import pytest

from aigit.canonical import canonical_json, hash_bytes
from aigit.domain import Event
from aigit.local_store import LocalStore


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
