"""Stable domain contracts for contribution provenance events and patches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class Classification(str, Enum):
    AI_SKILL = "AI_SKILL"
    AI_REUSED = "AI_REUSED"
    AI_DERIVED = "AI_DERIVED"
    MANUAL_CANDIDATE = "MANUAL_CANDIDATE"
    USER_SUPPLIED = "USER_SUPPLIED"
    UNKNOWN = "UNKNOWN"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


class ActionKind(str, Enum):
    ADDED = "ADDED"
    REPLACED = "REPLACED"
    MOVED = "MOVED"
    FORMATTED = "FORMATTED"
    DELETED = "DELETED"


class ProvenanceStatus(str, Enum):
    RECORDED = "recorded"
    LOCAL_ONLY = "local-only"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Event:
    schema_version: int
    event_id: str
    event_type: str
    repo_id: str
    session_id: str
    sequence: int
    observed_at: str
    payload: dict[str, Any]
    previous_event_hash: str
    event_hash: str

    @classmethod
    def new(
        cls,
        repo_id: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> Event:
        if not repo_id.strip():
            raise ValueError("repo_id must not be empty")
        if not session_id.strip():
            raise ValueError("session_id must not be empty")

        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(
            schema_version=1,
            event_id=str(uuid4()),
            event_type=event_type,
            repo_id=repo_id,
            session_id=session_id,
            sequence=0,
            observed_at=observed_at,
            payload=dict(payload),
            previous_event_hash="",
            event_hash="",
        )


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    index_hash: str
    worktree_hash: str
    files: dict[str, str]


@dataclass(frozen=True)
class PatchSpan:
    path: str
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    classification: Classification
    action: ActionKind
    confidence: float
    old_path: str | None = None

    @classmethod
    def added(cls, path: str, lines: tuple[str, ...]) -> PatchSpan:
        normalized_lines = tuple(lines)
        return cls(
            path=path,
            old_start=0,
            old_end=0,
            new_start=0,
            new_end=len(normalized_lines),
            old_lines=(),
            new_lines=normalized_lines,
            classification=Classification.UNKNOWN,
            action=ActionKind.ADDED,
            confidence=0.0,
        )

    @classmethod
    def relocated(
        cls,
        old_path: str,
        new_path: str,
        lines: tuple[str, ...],
    ) -> PatchSpan:
        normalized_lines = tuple(lines)
        return cls(
            path=new_path,
            old_start=0,
            old_end=len(normalized_lines),
            new_start=0,
            new_end=len(normalized_lines),
            old_lines=normalized_lines,
            new_lines=normalized_lines,
            classification=Classification.UNKNOWN,
            action=ActionKind.MOVED,
            confidence=0.0,
            old_path=old_path,
        )
