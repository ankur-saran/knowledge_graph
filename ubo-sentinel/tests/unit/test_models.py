from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ubo_sentinel.models import (
    AuditEvent,
    DataGap,
    Decision,
    EffectiveExposure,
    Entity,
    EvidencePath,
    Provenance,
    Relationship,
    ReportingException,
    Sanction,
    canonical_json,
    compute_decision_id,
    entity_id,
    relationship_id,
    sanction_id,
    sha256_hex,
)

SNAPSHOT_IDS = {"fixtures": "abc123def456"}
CREATED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def make_provenance(**overrides):
    fields = {
        "source": "fixtures",
        "source_record_id": "row-1",
        "snapshot_id": "abc123def456",
        "as_of": date(2026, 9, 30),
        "confidence": 1.0,
    }
    return Provenance(**(fields | overrides))


def make_entity(**overrides):
    fields = {
        "id": "lei:5299000EXAMPLE000001",
        "lei": "5299000EXAMPLE000001",
        "registration_authority_id": "RA000001",
        "registration_number": "12345",
        "legal_name": "Acme Trading FZE",
        "aliases": ["Acme FZE", "ACME Trading"],
        "jurisdiction": "AE",
        "status": "ACTIVE",
        "entity_type": "LegalEntity",
        "provenance": make_provenance(),
    }
    return Entity(**(fields | overrides))


def make_relationship(**overrides):
    fields = {
        "id": relationship_id("owner-1", "asset-1", "OWNS", False),
        "subject_id": "owner-1",
        "object_id": "asset-1",
        "rel_type": "OWNS",
        "pct": Decimal("30"),
        "basis": "shareholding",
        "provenance": make_provenance(),
    }
    return Relationship(**(fields | overrides))


def make_sanction(**overrides):
    fields = {
        "id": sanction_id("fixtures", "row-9", "owner-1"),
        "entity_id": "owner-1",
        "designated_entity_id": "owner-1",
        "program": "SDN",
        "list_date": date(2024, 1, 15),
        "list_source": "OFAC",
        "is_active": True,
        "match_type": "DIRECT",
        "match_confidence": 1.0,
        "provenance": make_provenance(source_record_id="row-9"),
    }
    return Sanction(**(fields | overrides))


def make_decision(**overrides):
    fields = {
        "query": "Acme Trading FZE",
        "target_id": "asset-1",
        "recommendation": "ESCALATE",
        "status": "RECOMMENDED",
        "reasons": ["DERIVED_50PCT"],
        "blocked_set": ["owner-1", "asset-1"],
        "paths": [EvidencePath(node_ids=["owner-1", "asset-1"], edge_ids=["edge-1"])],
        "effective_exposure": EffectiveExposure(max_pct=Decimal("60"), sum_pct=Decimal("60")),
        "data_gaps": [
            DataGap(
                code="LAPSED_LEI",
                subject_id="owner-1",
                decision_relevant=True,
                provenance=make_provenance(),
            )
        ],
        "memo_json": {"target": "asset-1"},
        "snapshot_set_id": "fixtures",
        "snapshot_ids": SNAPSHOT_IDS,
        "rule_pack_id": "ofac_50pct_v1",
        "rule_pack_hash": "f" * 64,
        "engine_version": "0.1.0",
        "max_depth": 5,
        "created_at": CREATED_AT,
    } | overrides
    if "decision_id" not in fields:
        fields["decision_id"] = compute_decision_id(
            fields["target_id"],
            fields["query"],
            fields["snapshot_ids"],
            fields["rule_pack_hash"],
            fields["engine_version"],
            fields["max_depth"],
        )
    return Decision(**fields)


def make_audit_event(**overrides):
    fields = {
        "seq": 0,
        "decision_id": make_decision().decision_id,
        "event_type": "RECOMMENDED",
        "actor": "system",
        "role": "analyst",
        "payload": make_decision().canonical_payload(),
        "created_at": CREATED_AT,
        "prev_hash": "0" * 64,
        "hash": "1" * 64,
    }
    return AuditEvent(**(fields | overrides))


# --- round trips -----------------------------------------------------------


@pytest.mark.parametrize(
    "instance",
    [
        make_provenance(),
        make_entity(),
        make_entity(entity_type="Person", lei=None, id="fixtures:p-1"),
        ReportingException(
            entity_id="asset-1",
            category="ULTIMATE_PARENT",
            reason="NATURAL_PERSONS",
            provenance=make_provenance(),
        ),
        make_relationship(),
        make_relationship(
            rel_type="CONSOLIDATED_BY",
            pct=None,
            basis="accounting_consolidation",
            supporting_records=[make_provenance(source="gleif")],
        ),
        make_sanction(),
        EvidencePath(node_ids=["a", "b", "c"], edge_ids=["e1", "e2"]),
        EffectiveExposure(has_unknown=True),
        make_decision(),
        make_decision(target_id=None, recommendation="REVIEW", blocked_set=[], paths=[]),
        make_decision(
            status="OVERRIDDEN",
            reviewed_by="mlro",
            review_reason="Ownership confirmed by filing",
            reviewed_at=CREATED_AT,
        ),
        make_audit_event(),
    ],
    ids=lambda instance: type(instance).__name__,
)
def test_json_round_trip(instance):
    restored = type(instance).model_validate_json(instance.model_dump_json())
    assert restored == instance
    assert canonical_json(restored) == canonical_json(instance)


# --- shared model behaviour ------------------------------------------------


def test_models_reject_unknown_fields():
    with pytest.raises(ValidationError):
        make_provenance(note="x")
    with pytest.raises(ValidationError):
        make_entity(nickname="x")


def test_models_are_frozen():
    with pytest.raises(ValidationError):
        make_entity().legal_name = "Other"


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_between_0_and_1(confidence):
    with pytest.raises(ValidationError):
        make_provenance(confidence=confidence)
    with pytest.raises(ValidationError):
        make_sanction(match_confidence=confidence)


def test_as_of_is_a_date():
    assert make_provenance(as_of="2026-09-30").as_of == date(2026, 9, 30)
    with pytest.raises(ValidationError):
        make_provenance(as_of="yesterday")


# --- entity, relationship, sanction ----------------------------------------


def test_aliases_are_sorted_and_deduplicated():
    assert make_entity(aliases=["b", "a", "b"]).aliases == ["a", "b"]


def test_entity_status_and_type_are_closed_sets():
    with pytest.raises(ValidationError):
        make_entity(status="DORMANT")
    with pytest.raises(ValidationError):
        make_entity(entity_type="Trust")


def test_entity_id_prefers_lei():
    assert entity_id("5299000EXAMPLE000001", "gleif", "r1") == "lei:5299000EXAMPLE000001"
    assert entity_id(None, "opensanctions", "NK-abc") == "opensanctions:NK-abc"


@pytest.mark.parametrize("pct", ["0", "-5", "100.01", "0.00001"])
def test_pct_must_be_above_0_and_at_most_100(pct):
    with pytest.raises(ValidationError):
        make_relationship(pct=Decimal(pct))


def test_pct_is_a_decimal_quantised_to_4_places():
    relationship = make_relationship(pct="33.333333")
    assert isinstance(relationship.pct, Decimal)
    assert str(relationship.pct) == "33.3333"
    assert make_relationship(pct=Decimal("100")).pct == Decimal("100")


def test_decimal_sums_do_not_depend_on_order():
    stakes = [make_relationship(pct=value).pct for value in ("16.7", "16.6", "16.7")]
    assert sum(stakes) == sum(reversed(stakes)) == Decimal("50")


def test_relationship_enums_are_closed_sets():
    with pytest.raises(ValidationError):
        make_relationship(rel_type="FUNDS")
    with pytest.raises(ValidationError):
        make_relationship(basis="rumour")


def test_supporting_records_are_sorted():
    a, b = make_provenance(source="a"), make_provenance(source="b")
    assert make_relationship(supporting_records=[b, a]).supporting_records == [a, b]


def test_relationship_and_sanction_ids_are_deterministic_and_separator_safe():
    assert relationship_id("ab", "c", "OWNS", False) == relationship_id("ab", "c", "OWNS", False)
    assert relationship_id("ab", "c", "OWNS", False) != relationship_id("a", "bc", "OWNS", False)
    assert relationship_id("a", "b", "CONSOLIDATED_BY", False) != relationship_id(
        "a", "b", "CONSOLIDATED_BY", True
    )
    assert sanction_id("s", "r", "e1") != sanction_id("s", "r", "e2")
    assert len(sanction_id("s", "r", "e1")) == 16


# --- evidence ---------------------------------------------------------------


def test_path_needs_one_fewer_edge_than_nodes():
    with pytest.raises(ValidationError):
        EvidencePath(node_ids=["a", "b"], edge_ids=[])
    with pytest.raises(ValidationError):
        EvidencePath(node_ids=[], edge_ids=[])


def test_gap_code_is_a_closed_set():
    with pytest.raises(ValidationError):
        DataGap(
            code="MISSING", subject_id="a", decision_relevant=True, provenance=make_provenance()
        )


# --- canonical JSON ---------------------------------------------------------


def test_canonical_json_is_sorted_compact_utf8():
    assert canonical_json({"b": 1, "a": ["é", None]}) == '{"a":["é",null],"b":1}'.encode()


def test_canonical_json_writes_decimals_as_4_place_strings():
    assert canonical_json({"pct": Decimal("30")}) == b'{"pct":"30.0000"}'
    assert canonical_json(Decimal("30")) == canonical_json(Decimal("30.00"))


def test_canonical_json_rejects_unknown_types():
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


def test_sha256_hex_ignores_key_order():
    assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})


# --- decision ---------------------------------------------------------------


def test_decision_id_is_deterministic_16_hex():
    first, second = make_decision(), make_decision()
    assert first.decision_id == second.decision_id
    assert len(first.decision_id) == 16
    int(first.decision_id, 16)


def test_decision_id_uses_resolved_snapshots_not_the_alias():
    base = make_decision(snapshot_set_id="latest")
    same_data = make_decision(snapshot_set_id="fixtures")
    new_data = make_decision(snapshot_set_id="latest", snapshot_ids={"fixtures": "999999999999"})
    assert base.decision_id == same_data.decision_id
    assert base.decision_id != new_data.decision_id


@pytest.mark.parametrize(
    "change",
    [
        {"target_id": "asset-2"},
        {"rule_pack_hash": "e" * 64},
        {"engine_version": "0.2.0"},
        {"max_depth": 3},
    ],
)
def test_decision_id_changes_with_each_input(change):
    assert make_decision(**change).decision_id != make_decision().decision_id


def test_decision_id_falls_back_to_normalised_query_without_target():
    no_target = {"target_id": None, "recommendation": "REVIEW", "blocked_set": [], "paths": []}
    one = make_decision(query="Nowhere  Ltd", **no_target)
    same = make_decision(query="nowhere ltd", **no_target)
    other = make_decision(query="Elsewhere Ltd", **no_target)
    assert one.decision_id == same.decision_id
    assert one.decision_id != other.decision_id


def test_decision_rejects_an_id_that_does_not_match_its_inputs():
    with pytest.raises(ValidationError, match="decision_id"):
        make_decision(decision_id="0" * 16)


def test_decision_collections_are_sorted():
    decision = make_decision(
        blocked_set=["b", "a"],
        reasons=["EFFECTIVE_EXPOSURE", "DERIVED_50PCT"],
        paths=[
            EvidencePath(node_ids=["b", "t"], edge_ids=["e2"]),
            EvidencePath(node_ids=["a", "t"], edge_ids=["e1"]),
        ],
    )
    assert decision.blocked_set == ["a", "b"]
    assert decision.reasons == ["DERIVED_50PCT", "EFFECTIVE_EXPOSURE"]
    assert [path.node_ids[0] for path in decision.paths] == ["a", "b"]


def test_reason_code_is_a_closed_set():
    with pytest.raises(ValidationError):
        make_decision(reasons=["BECAUSE"])


def test_blocked_and_possibly_blocked_sets_must_not_overlap():
    with pytest.raises(ValidationError, match="overlap"):
        make_decision(blocked_set=["a"], possibly_blocked_set=["a"])


def test_recommended_decision_has_no_reviewer_fields():
    with pytest.raises(ValidationError):
        make_decision(reviewed_by="mlro", reviewed_at=CREATED_AT)


def test_reviewed_decision_needs_a_reviewer():
    with pytest.raises(ValidationError):
        make_decision(status="APPROVED")


def test_override_needs_a_reason():
    with pytest.raises(ValidationError, match="review_reason"):
        make_decision(
            status="OVERRIDDEN", reviewed_by="mlro", reviewed_at=CREATED_AT, review_reason="  "
        )


def test_timestamps_must_be_aware_and_are_stored_in_utc():
    with pytest.raises(ValidationError):
        make_decision(created_at=datetime(2026, 10, 4, 12, 0))
    plus_two = timezone(timedelta(hours=2))
    decision = make_decision(created_at=datetime(2026, 10, 4, 14, 0, tzinfo=plus_two))
    assert decision.created_at == CREATED_AT
    assert decision.created_at.utcoffset() == timedelta(0)


def test_canonical_payload_excludes_non_deterministic_fields():
    payload = make_decision().canonical_payload()
    excluded = {
        "created_at",
        "status",
        "llm_annotations",
        "reviewed_by",
        "review_reason",
        "reviewed_at",
    }
    assert excluded.isdisjoint(payload)
    assert payload["decision_id"] == make_decision().decision_id
    assert payload["effective_exposure"]["max_pct"] == "60.0000"


def test_canonical_payload_is_unchanged_by_time_review_and_llm_text():
    recommended = make_decision()
    reviewed = make_decision(
        status="APPROVED",
        reviewed_by="mlro",
        reviewed_at=CREATED_AT + timedelta(hours=1),
        created_at=CREATED_AT + timedelta(days=1),
        llm_annotations={"memo_prose": "text"},
    )
    assert canonical_json(recommended.canonical_payload()) == canonical_json(
        reviewed.canonical_payload()
    )


def test_canonical_payload_is_unchanged_by_input_order():
    one = make_decision(
        blocked_set=["owner-1", "asset-1"], reasons=["CONTROL_LINK", "DERIVED_50PCT"]
    )
    two = make_decision(
        blocked_set=["asset-1", "owner-1"], reasons=["DERIVED_50PCT", "CONTROL_LINK"]
    )
    assert canonical_json(one.canonical_payload()) == canonical_json(two.canonical_payload())


# --- audit event ------------------------------------------------------------


def test_audit_event_type_and_role_are_closed_sets():
    with pytest.raises(ValidationError):
        make_audit_event(event_type="DELETED")
    with pytest.raises(ValidationError):
        make_audit_event(role="intern")
    with pytest.raises(ValidationError):
        make_audit_event(seq=-1)
