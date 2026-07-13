"""Ordered provenance classification for isolated repository patch spans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re
from typing import Iterable, Sequence

from aigit.domain import ActionKind, Classification, PatchSpan
from aigit.prompt_evidence import PromptEvidence, _metric_lines, _normalize_lines


_TOKEN_PATTERN = re.compile(
    r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|==|!=|<=|>=|=>|::|&&|\|\||\S'''
)


@dataclass(frozen=True, slots=True)
class RepositoryBlock:
    path: str
    lines: tuple[str, ...]
    origin: Classification

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", _normalize_lines(self.lines))


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    in_transaction: bool
    observer_healthy: bool
    prompt_evidence: PromptEvidence
    repository_blocks: tuple[RepositoryBlock, ...]
    removed_blocks: tuple[RepositoryBlock, ...]


def _tokens(lines: Iterable[str]) -> tuple[str, ...]:
    text = "\n".join(_normalize_lines(lines))
    return tuple(_TOKEN_PATTERN.findall(text))


def _aligned_similarity(
    left_tokens: Sequence[str],
    right_tokens: Sequence[str],
) -> float:
    if not left_tokens or not right_tokens:
        return 0.0

    matcher = SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
    largest = matcher.find_longest_match()
    shorter_length = min(len(left_tokens), len(right_tokens))

    if len(left_tokens) <= len(right_tokens):
        start = max(0, min(largest.b - largest.a, len(right_tokens) - shorter_length))
        left = left_tokens
        right = right_tokens[start : start + shorter_length]
    else:
        start = max(0, min(largest.a - largest.b, len(left_tokens) - shorter_length))
        left = left_tokens[start : start + shorter_length]
        right = right_tokens

    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _same_normalized_block(left: Iterable[str], right: Iterable[str]) -> bool:
    return _metric_lines(left) == _metric_lines(right)


def _format_only_match(left: Iterable[str], right: Iterable[str]) -> bool:
    left_lines = _metric_lines(left)
    right_lines = _metric_lines(right)
    return bool(left_lines and right_lines and _tokens(left_lines) == _tokens(right_lines))


def _removed_source(
    span: PatchSpan,
    context: ClassificationContext,
) -> tuple[RepositoryBlock, ActionKind] | None:
    destination = _metric_lines(span.new_lines)
    if not destination:
        return None

    for source in context.removed_blocks:
        if span.old_path is not None and source.path != span.old_path:
            continue
        if _same_normalized_block(source.lines, destination):
            action = ActionKind.MOVED if source.path != span.path else ActionKind.FORMATTED
            return source, action
        if source.path == span.path and _format_only_match(source.lines, destination):
            return source, ActionKind.FORMATTED
    return None


def _reuse_confidence(
    destination: Iterable[str],
    repository_blocks: Iterable[RepositoryBlock],
) -> float | None:
    destination_lines = _metric_lines(destination)
    if len(destination_lines) >= 3:
        for source in repository_blocks:
            if destination_lines == _metric_lines(source.lines):
                return 1.0

    if len(destination_lines) < 5:
        return None

    destination_tokens = _tokens(destination_lines)
    best = 0.0
    for source in repository_blocks:
        source_lines = _metric_lines(source.lines)
        if len(source_lines) < 5:
            continue
        similarity = _aligned_similarity(destination_tokens, _tokens(source_lines))
        best = max(best, similarity)

    return best if best >= 0.85 else None


def _classify_without_prompt(
    span: PatchSpan,
    context: ClassificationContext,
) -> PatchSpan:
    destination = _metric_lines(span.new_lines)

    if context.in_transaction and destination:
        confidence = _reuse_confidence(destination, context.repository_blocks)
        if confidence is not None:
            return replace(
                span,
                classification=Classification.AI_REUSED,
                confidence=confidence,
            )
        return replace(
            span,
            classification=Classification.AI_SKILL,
            confidence=1.0,
        )

    if not context.in_transaction and context.observer_healthy:
        return replace(span, classification=Classification.MANUAL_CANDIDATE, confidence=1.0)

    return replace(span, classification=Classification.UNKNOWN, confidence=0.0)


def _added_subspan(span: PatchSpan, start: int, end: int) -> PatchSpan:
    return replace(
        span,
        new_start=span.new_start + start,
        new_end=span.new_start + end,
        new_lines=span.new_lines[start:end],
    )


def _split_added_prompt_overlap(
    span: PatchSpan,
    ranges: tuple[tuple[int, int], ...],
    context: ClassificationContext,
) -> list[PatchSpan]:
    result: list[PatchSpan] = []
    position = 0
    for start, end in ranges:
        if position < start:
            result.append(
                _classify_without_prompt(
                    _added_subspan(span, position, start),
                    context,
                )
            )
        result.append(
            replace(
                _added_subspan(span, start, end),
                classification=Classification.USER_SUPPLIED,
                confidence=1.0,
            )
        )
        position = end

    if position < len(span.new_lines):
        result.append(
            _classify_without_prompt(
                _added_subspan(span, position, len(span.new_lines)),
                context,
            )
        )
    return result


def _classify_span(
    span: PatchSpan,
    context: ClassificationContext,
) -> list[PatchSpan]:
    destination = _metric_lines(span.new_lines)
    prompt_ranges = context.prompt_evidence.matching_ranges(span.new_lines)

    if prompt_ranges == ((0, len(span.new_lines)),):
        return [
            replace(
                span,
                classification=Classification.USER_SUPPLIED,
                confidence=1.0,
            )
        ]

    removed = _removed_source(span, context)
    if removed is not None:
        source, action = removed
        return [
            replace(
                span,
                classification=source.origin,
                action=action,
                confidence=1.0,
            )
        ]

    effective_span = (
        replace(span, action=ActionKind.ADDED)
        if span.action is ActionKind.MOVED
        else span
    )
    if prompt_ranges:
        if effective_span.action is ActionKind.ADDED:
            return _split_added_prompt_overlap(effective_span, prompt_ranges, context)
        return [
            replace(
                effective_span,
                classification=Classification.UNKNOWN,
                confidence=0.0,
            )
        ]

    return [_classify_without_prompt(effective_span, context)]


def classify_spans(
    spans: Iterable[PatchSpan],
    context: ClassificationContext,
) -> list[PatchSpan]:
    """Classify spans according to the approved, uncertainty-preserving order."""

    return [classified for span in spans for classified in _classify_span(span, context)]
