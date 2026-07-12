from aigit.domain import Classification, Event, ProvenanceStatus


def test_public_classification_values_are_stable() -> None:
    assert {item.value for item in Classification} == {
        "AI_SKILL", "AI_REUSED", "AI_DERIVED", "MANUAL_CANDIDATE",
        "USER_SUPPLIED", "UNKNOWN", "LEGACY_UNKNOWN",
    }
    assert ProvenanceStatus.LOCAL_ONLY.value == "local-only"


def test_event_rejects_empty_identity() -> None:
    try:
        Event.new("", "session-1", "heartbeat", {})
    except ValueError as exc:
        assert str(exc) == "repo_id must not be empty"
    else:
        raise AssertionError("empty repo_id was accepted")
