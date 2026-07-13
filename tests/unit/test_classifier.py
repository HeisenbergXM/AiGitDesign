from aigit.classifier import ClassificationContext, RepositoryBlock, classify_spans
from aigit.domain import Classification, PatchSpan
from aigit.prompt_evidence import build_prompt_evidence


KEY = b"k" * 32
MODULE_LINES = (
    "def total(items):",
    "    values = list(items)",
    "    return sum(values)",
)

# Each block has 20 language-neutral tokens. The destination changes exactly
# three identifiers, leaving 17/20 matching tokens and a similarity of 0.85.
SIMILARITY_085_SOURCE = tuple(f"v{index} = source{index};" for index in range(5))
SIMILARITY_085_DESTINATION = tuple(
    f"v{index} = {'novel' if index < 3 else 'source'}{index};"
    for index in range(5)
)

# These blocks have 53 tokens each. Eight identifier substitutions leave
# 45/53 matching tokens, or approximately 0.8490566, immediately below 0.85.
SIMILARITY_0849_SOURCE = (
    "v0 = +source0;",
    *(f"v{index} = source{index};" for index in range(1, 13)),
)
SIMILARITY_0849_DESTINATION = (
    "v0 = +novel0;",
    *(
        f"v{index} = {'novel' if index < 8 else 'source'}{index};"
        for index in range(1, 13)
    ),
)


def _context(
    *,
    in_transaction: bool = True,
    observer_healthy: bool = True,
    prompt_blocks: tuple[tuple[str, ...], ...] = (),
    repository_blocks: tuple[RepositoryBlock, ...] = (),
    removed_blocks: tuple[RepositoryBlock, ...] = (),
) -> ClassificationContext:
    return ClassificationContext(
        in_transaction=in_transaction,
        observer_healthy=observer_healthy,
        prompt_evidence=build_prompt_evidence(prompt_blocks, key=KEY),
        repository_blocks=repository_blocks,
        removed_blocks=removed_blocks,
    )


def test_new_exact_copy_is_ai_reused() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.added("b.py", MODULE_LINES),),
        _context(repository_blocks=(source,)),
    )

    assert result[0].classification is Classification.AI_REUSED


def test_two_line_exact_copy_remains_ai_skill() -> None:
    two_lines = MODULE_LINES[:2]
    source = RepositoryBlock("a.py", two_lines, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.added("b.py", two_lines),),
        _context(repository_blocks=(source,)),
    )

    assert result[0].classification is Classification.AI_SKILL


def test_token_similarity_at_0_85_is_ai_reused() -> None:
    source = RepositoryBlock(
        "a.py",
        SIMILARITY_085_SOURCE,
        Classification.MANUAL_CANDIDATE,
    )

    result = classify_spans(
        (PatchSpan.added("b.py", SIMILARITY_085_DESTINATION),),
        _context(repository_blocks=(source,)),
    )

    assert result[0].classification is Classification.AI_REUSED


def test_token_similarity_at_0_849_remains_ai_skill() -> None:
    source = RepositoryBlock(
        "a.py",
        SIMILARITY_0849_SOURCE,
        Classification.MANUAL_CANDIDATE,
    )

    result = classify_spans(
        (PatchSpan.added("b.py", SIMILARITY_0849_DESTINATION),),
        _context(repository_blocks=(source,)),
    )

    assert result[0].classification is Classification.AI_SKILL


def test_true_move_retains_origin() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.relocated("a.py", "moved.py", MODULE_LINES),),
        _context(repository_blocks=(source,), removed_blocks=(source,)),
    )

    assert result[0].action.value == "MOVED"
    assert result[0].classification is Classification.MANUAL_CANDIDATE


def test_retained_source_is_copy_not_move() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.relocated("a.py", "copied.py", MODULE_LINES),),
        _context(repository_blocks=(source,)),
    )

    assert result[0].action.value == "ADDED"
    assert result[0].classification is Classification.AI_REUSED


def test_prompt_patch_overlap_wins() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.added("b.py", MODULE_LINES),),
        _context(prompt_blocks=(MODULE_LINES,), repository_blocks=(source,)),
    )

    assert result[0].classification is Classification.USER_SUPPLIED


def test_outside_transaction_with_healthy_observer_is_manual_candidate() -> None:
    result = classify_spans(
        (PatchSpan.added("manual.py", ("value = 1",)),),
        _context(in_transaction=False, observer_healthy=True),
    )

    assert result[0].classification is Classification.MANUAL_CANDIDATE


def test_outside_transaction_with_unhealthy_observer_is_unknown() -> None:
    result = classify_spans(
        (PatchSpan.added("ambiguous.py", ("value = 1",)),),
        _context(in_transaction=False, observer_healthy=False),
    )

    assert result[0].classification is Classification.UNKNOWN
