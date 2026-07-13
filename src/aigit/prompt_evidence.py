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
    _line_fingerprints: tuple[tuple[str, ...], ...] = field(
        repr=False,
        compare=False,
    )

    def matching_ranges(self, lines: Iterable[str]) -> tuple[tuple[int, int], ...]:
        """Return maximal candidate line ranges present contiguously in prompts."""

        normalized = _normalize_lines(lines)
        indexed_candidate = tuple(
            (index, _line_fingerprint(line, self._key))
            for index, line in enumerate(normalized)
            if line
        )
        if not indexed_candidate:
            return ()

        candidate_fingerprints = tuple(item[1] for item in indexed_candidate)
        matches: list[tuple[int, int]] = []
        for prompt_fingerprints in self._line_fingerprints:
            for candidate_start, candidate_fingerprint in enumerate(candidate_fingerprints):
                for prompt_start, prompt_fingerprint in enumerate(prompt_fingerprints):
                    if not hmac.compare_digest(candidate_fingerprint, prompt_fingerprint):
                        continue
                    length = 0
                    while (
                        candidate_start + length < len(candidate_fingerprints)
                        and prompt_start + length < len(prompt_fingerprints)
                        and hmac.compare_digest(
                            candidate_fingerprints[candidate_start + length],
                            prompt_fingerprints[prompt_start + length],
                        )
                    ):
                        length += 1
                    raw_start = indexed_candidate[candidate_start][0]
                    raw_end = indexed_candidate[candidate_start + length - 1][0] + 1
                    while raw_start > 0 and not normalized[raw_start - 1]:
                        raw_start -= 1
                    while raw_end < len(normalized) and not normalized[raw_end]:
                        raw_end += 1
                    matches.append((raw_start, raw_end))

        if not matches:
            return ()

        maximal: list[tuple[int, int]] = []
        for start, end in sorted(matches):
            if maximal and start <= maximal[-1][1]:
                previous_start, previous_end = maximal[-1]
                maximal[-1] = (previous_start, max(previous_end, end))
            else:
                maximal.append((start, end))
        return tuple(maximal)

    def overlaps(self, lines: Iterable[str]) -> bool:
        return bool(self.matching_ranges(lines))


def _line_fingerprint(line: str, key: bytes) -> str:
    return hmac.new(key, b"line\0" + line.encode("utf-8"), hashlib.sha256).hexdigest()


def build_prompt_evidence(
    code_blocks: Iterable[Iterable[str]],
    key: bytes,
) -> PromptEvidence:
    """Build HMAC-only evidence and discard normalized prompt text immediately."""

    if not key:
        raise ValueError("key must not be empty")

    fingerprints: list[str] = []
    counts: list[int] = []
    line_fingerprints: list[tuple[str, ...]] = []
    for code_block in code_blocks:
        normalized = _metric_lines(code_block)
        if not normalized:
            continue
        fingerprints.append(_fingerprint(normalized, key))
        counts.append(len(normalized))
        line_fingerprints.append(tuple(_line_fingerprint(line, key) for line in normalized))

    return PromptEvidence(
        tuple(fingerprints),
        tuple(counts),
        bytes(key),
        tuple(line_fingerprints),
    )
