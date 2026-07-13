"""Privacy-preserving evidence for code supplied directly in a prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
from typing import Iterable, Sequence


def _normalize_lines(lines: Iterable[str]) -> tuple[str, ...]:
    """Normalize line endings and trailing whitespace without retaining a blob."""

    normalized: list[str] = []
    for line in lines:
        text = line.replace("\r\n", "\n")
        parts = text.split("\n")
        if text.endswith("\n"):
            parts.pop()
        normalized.extend(part.rstrip() for part in parts)
    return tuple(normalized)


def _metric_lines(lines: Iterable[str]) -> tuple[str, ...]:
    return tuple(line for line in _normalize_lines(lines) if line)


def _fingerprint(lines: Sequence[str], key: bytes) -> str:
    payload = "\n".join(lines).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class PromptEvidence:
    """HMAC fingerprints and line counts for normalized prompt code blocks.

    The key is retained only so candidate patch blocks can be fingerprinted for
    comparison. It is excluded from representation and equality, and no prompt
    source text is retained.
    """

    fingerprints: tuple[str, ...]
    counts: tuple[int, ...]
    _key: bytes = field(repr=False, compare=False)

    def overlaps(self, lines: Iterable[str]) -> bool:
        candidate = _metric_lines(lines)
        for fingerprint, count in zip(self.fingerprints, self.counts, strict=True):
            if count == 0 or count > len(candidate):
                continue
            for start in range(len(candidate) - count + 1):
                block = candidate[start : start + count]
                if hmac.compare_digest(_fingerprint(block, self._key), fingerprint):
                    return True
        return False


def build_prompt_evidence(
    code_blocks: Iterable[Iterable[str]],
    key: bytes,
) -> PromptEvidence:
    """Build HMAC-only evidence and discard normalized prompt text immediately."""

    if not key:
        raise ValueError("key must not be empty")

    fingerprints: list[str] = []
    counts: list[int] = []
    for code_block in code_blocks:
        normalized = _metric_lines(code_block)
        if not normalized:
            continue
        fingerprints.append(_fingerprint(normalized, key))
        counts.append(len(normalized))

    return PromptEvidence(tuple(fingerprints), tuple(counts), bytes(key))
