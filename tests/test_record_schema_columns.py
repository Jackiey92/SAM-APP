"""Independent wire-shape reconciliation for the D8 column declarations."""
from dataclasses import FrozenInstanceError
import re

import pytest

from app.facts_tree.record_schema import SCHEMA_COLUMNS, SCHEMA_FIELDS
from app.facts_tree.record_schema.columns import ColumnSpec
from tests.facts_tree_full_fixture import full_tree


# Reviewed against the actual spec dictionaries; nullable scalar keys are
# deliberately absent here. Mixed-type unions cannot have a SQLite scalar type.
COMPOSITE = {
    "consumed_candidate": "disposition_result",
    "entity_current": "",
    "entity_revision": "aliases predecessor_ref lineage disambiguation_issue_ref actor",
    "event": "changed_refs before after invalidation_domains affected_refs",
    "evidence": "source_version_ref attribution_index quote_excerpt_ref",
    "fact_current": "",
    "fact_revision": (
        "entity_ref canonical_value value_unit qualifiers primary_evidence_refs "
        "field_evidence unresolved qualification_basis_refs confirmation_basis_refs "
        "predecessor_ref valid_from valid_to actor"),
    "input_candidate": "whitelisted_fields envelope_summary",
    "issue_event": (
        "predecessor_event_ref affected_refs actor candidate_refs fact_refs "
        "conflict_evidence_refs disposition_result"),
    "link_revision": "predecessor_ref link evidence_refs actor superseded_by",
    "operation": "candidate_refs disposition_result formal_fact_refs actor approval_use_refs event_ids",
    "placement_current": "",
    "placement_revision": "fact_ref predecessor_ref actor migration_ref approval_use_refs",
    "proposal_event": (
        "predecessor_event_ref affected_refs actor input_impact execution_operation_ref disposition_result"),
    "source_lifecycle": "predecessor_ref version_scope actor approval_use_refs",
    "source_version": "locator mapped_object_ref prior_version_ref actor",
}
NULLABLE = {
    "evidence": (
        "original_hash mapped_fingerprint source_loc coordinate quote_digest "
        "legacy_normalized_fingerprint legacy_fingerprint_algorithm read_audit_ref"),
    "fact_revision": "value_raw conversion_rule_version conversion_input_digest",
    "issue_event": "basis", "proposal_event": "basis",
    "source_lifecycle": "source_description_ref",
    "source_version": (
        "origin_zone origin_key content_fingerprint fingerprint_algorithm file_hash "
        "source_description_ref adapter_rule_version mapping_rule_version"),
}


def test_family_set_and_frozen_exports():
    assert set(SCHEMA_COLUMNS) == set(SCHEMA_FIELDS) == set(COMPOSITE)
    assert len(SCHEMA_COLUMNS) == 16
    with pytest.raises(TypeError):
        SCHEMA_COLUMNS["new"] = ()
    with pytest.raises(FrozenInstanceError):
        SCHEMA_COLUMNS["fact_current"][0].required = False


@pytest.mark.parametrize("family", sorted(COMPOSITE))
def test_exact_scalar_fields_and_requiredness(family):
    columns = SCHEMA_COLUMNS[family]
    assert type(columns) is tuple
    names = [column.name for column in columns]
    assert len(names) == len(set(names))
    assert set(names) == SCHEMA_FIELDS[family] - set(COMPOSITE[family].split())
    common = {"repo_id": "TEXT", "company_id": "TEXT", "record_type": "TEXT",
              "schema_version": "INTEGER", "format_version": "INTEGER"}
    nullable = set(NULLABLE.get(family, "").split())
    for column in columns:
        assert type(column) is ColumnSpec
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column.name)
        assert column.sql_type in {"TEXT", "INTEGER", "REAL"}
        assert column.required is (column.name not in nullable)
        if column.name in common:
            assert column.sql_type == common[column.name]
            assert column.required
    assert common.keys() <= set(names)


def test_real_wire_values_have_declared_types():
    for payload in full_tree().values():
        for column in SCHEMA_COLUMNS[payload["record_type"]]:
            value = payload[column.name]
            if value is None:
                assert not column.required
            else:
                assert type(value) is {"TEXT": str, "INTEGER": int, "REAL": float}[column.sql_type]
    assert not any(c.sql_type == "REAL" for columns in SCHEMA_COLUMNS.values() for c in columns)


def test_searchable_columns_exact_and_read_only():
    from app.facts_tree.record_schema import SEARCHABLE_COLUMNS
    expected = {
        "entity_revision": ("raw_name", "canonical_name"),
        "fact_revision": ("statement_raw", "reason", "value_raw"),
        "evidence": ("usage",), "link_revision": ("reason",),
        "issue_event": ("basis",), "proposal_event": ("basis",),
        "source_lifecycle": ("reason",), "source_version": ("display_name",),
    }
    assert {f: names for f, names in SEARCHABLE_COLUMNS.items() if names} == expected
    assert sum(map(len, SEARCHABLE_COLUMNS.values())) == 11
    for family, columns in SCHEMA_COLUMNS.items():
        assert set(SEARCHABLE_COLUMNS[family]) <= SCHEMA_FIELDS[family]
        assert all(c.sql_type == "TEXT" for c in columns if c.searchable)
    with pytest.raises(TypeError):
        SEARCHABLE_COLUMNS["event"] = ()
    assert ColumnSpec("old", "TEXT", True).searchable is False
