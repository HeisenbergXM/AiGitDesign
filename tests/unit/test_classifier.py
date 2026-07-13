from aigit.classifier import ClassificationContext, RepositoryBlock, classify_spans
from aigit.domain import ActionKind, Classification, PatchSpan
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


def test_added_mixed_prompt_overlap_splits_without_losing_lines() -> None:
    result = classify_spans(
        (PatchSpan.added("mixed.py", MODULE_LINES),),
        _context(prompt_blocks=(MODULE_LINES[:2],)),
    )

    assert len(result) == 2
    supplied, generated = result
    assert supplied.new_lines == MODULE_LINES[:2]
    assert supplied.new_start == 0
    assert supplied.new_end == 2
    assert supplied.classification is Classification.USER_SUPPLIED
    assert generated.new_lines == MODULE_LINES[2:]
    assert generated.new_start == 2
    assert generated.new_end == 3
    assert generated.classification is Classification.AI_SKILL
    assert supplied.new_lines + generated.new_lines == MODULE_LINES


def test_destination_subset_of_larger_prompt_block_is_user_supplied() -> None:
    prompt_block = (
        "setup = True",
        *MODULE_LINES[:2],
        "cleanup = True",
    )

    result = classify_spans(
        (PatchSpan.added("subset.py", MODULE_LINES[:2]),),
        _context(prompt_blocks=(prompt_block,)),
    )

    assert len(result) == 1
    assert result[0].new_lines == MODULE_LINES[:2]
    assert result[0].classification is Classification.USER_SUPPLIED


def test_unmappable_mixed_prompt_overlap_in_replacement_is_unknown() -> None:
    replacement = PatchSpan(
        path="replacement.py",
        old_start=4,
        old_end=6,
        new_start=8,
        new_end=11,
        old_lines=("old_first = 1", "old_second = 2"),
        new_lines=MODULE_LINES,
        classification=Classification.UNKNOWN,
        action=ActionKind.REPLACED,
        confidence=0.0,
    )

    result = classify_spans(
        (replacement,),
        _context(prompt_blocks=(MODULE_LINES[:2],)),
    )

    assert len(result) == 1
    assert result[0].action is ActionKind.REPLACED
    assert result[0].new_start == 8
    assert result[0].new_end == 11
    assert result[0].new_lines == MODULE_LINES
    assert result[0].classification is Classification.UNKNOWN


def test_retained_source_relocation_outside_transaction_is_manual_addition() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.relocated("a.py", "copy.py", MODULE_LINES),),
        _context(
            in_transaction=False,
            observer_healthy=True,
            repository_blocks=(source,),
        ),
    )

    assert result[0].action is ActionKind.ADDED
    assert result[0].classification is Classification.MANUAL_CANDIDATE


def test_retained_source_relocation_during_observer_gap_is_unknown_addition() -> None:
    source = RepositoryBlock("a.py", MODULE_LINES, Classification.MANUAL_CANDIDATE)

    result = classify_spans(
        (PatchSpan.relocated("a.py", "copy.py", MODULE_LINES),),
        _context(
            in_transaction=False,
            observer_healthy=False,
            repository_blocks=(source,),
        ),
    )

    assert result[0].action is ActionKind.ADDED
    assert result[0].classification is Classification.UNKNOWN


def test_prompt_evidence_exposes_no_raw_prompt_code() -> None:
    prompt_lines = ("secret_value = 41", "return secret_value + 1")

    evidence = build_prompt_evidence((prompt_lines,), key=KEY)
    representation = repr(evidence)

    assert evidence.counts == (2,)
    assert len(evidence.fingerprints) == 1
    assert len(evidence.fingerprints[0]) == 64
    assert set(evidence.__slots__) == {
        "fingerprints",
        "counts",
        "_key",
        "_line_fingerprints",
    }
    assert "_key" not in representation
    assert KEY.hex() not in representation
    assert all(line not in representation for line in prompt_lines)
    assert not hasattr(evidence, "code_blocks")
    assert not hasattr(evidence, "lines")


def test_prompt_evidence_returns_maximal_contiguous_candidate_ranges() -> None:
    evidence = build_prompt_evidence(
        (MODULE_LINES[:2], ("separate = True",)),
        key=KEY,
    )

    ranges = evidence.matching_ranges(
        (*MODULE_LINES[:2], "novel = True", "separate = True")
    )

    assert ranges == ((0, 2), (3, 4))
