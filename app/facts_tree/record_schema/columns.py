"""Appendix D8 v1: frozen top-level wire columns, without value rules.

Declarations follow entity_fact, source_evidence, link_placement,
governance_event and the four broker schemas. Unix-second timestamps are
INTEGER on the wire. Arbitrary JSON and mixed-type unions stay in payload_json.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sql_type: str
    required: bool
    searchable: bool = False


_COMMON = (
    ColumnSpec("repo_id", "TEXT", True),
    ColumnSpec("company_id", "TEXT", True),
    ColumnSpec("record_type", "TEXT", True),
    ColumnSpec("schema_version", "INTEGER", True),
    ColumnSpec("format_version", "INTEGER", True),
)


def _columns(text="", integer="", nullable_text="", *, searchable=""):
    searchable = searchable.split()
    return (_COMMON
            + tuple(ColumnSpec(name, "TEXT", True, name in searchable) for name in text.split())
            + tuple(ColumnSpec(name, "INTEGER", True) for name in integer.split())
            + tuple(ColumnSpec(name, "TEXT", False, name in searchable) for name in nullable_text.split()))


SCHEMA_COLUMNS: Mapping[str, tuple[ColumnSpec, ...]] = MappingProxyType({
    "consumed_candidate": _columns(
        "candidate_uid payload_digest operation_id", "candidate_revision"),
    "entity_current": _columns("entity_uid status", "revision"),
    "entity_revision": _columns(
        "entity_uid entity_kind raw_name canonical_name status reason",
        "revision event_time", searchable="raw_name canonical_name"),
    "event": _columns("event_id operation_id base_commit event_type", "ordinal"),
    "evidence": _columns(
        "evidence_id usage", nullable_text=(
            "original_hash mapped_fingerprint source_loc coordinate quote_digest "
            "legacy_normalized_fingerprint legacy_fingerprint_algorithm read_audit_ref"), searchable="usage"),
    "fact_current": _columns("fact_uid status", "revision"),
    "fact_revision": _columns(
        "fact_uid statement_raw value_type statement_kind polarity revision_kind reason",
        "revision event_time", "value_raw conversion_rule_version conversion_input_digest",
        searchable="statement_raw value_raw reason"),
    "input_candidate": _columns(
        "candidate_uid payload_digest candidate_rule_version adapter_rule_version "
        "validation_rule_version coverage_manifest_version source_id version_id "
        "mapped_fingerprint payload_ref envelope_ref", "candidate_revision"),
    "issue_event": _columns(
        "event_id issue_uid issue_type status", "ordinal event_time", "basis", searchable="basis"),
    "link_revision": _columns("link_id status reason", "revision event_time", searchable="reason"),
    "operation": _columns(
        "operation_id request_digest effect_digest request_id base_commit coverage_id "
        "coverage_digest coverage_validation_version unit_id rule_version model_version",
        "server_time"),
    "placement_current": _columns("fact_uid", "revision"),
    "placement_revision": _columns(
        "fact_uid pack_path_id template_pack manifest_sha256 trunk_code leaf_code "
        "action group_key order_key reason",
        "revision pack_version binding_revision event_time"),
    "proposal_event": _columns(
        "event_id proposal_uid proposal_type status", "ordinal event_time", "basis", searchable="basis"),
    "source_lifecycle": _columns(
        "source_id status reason", "revision event_time", "source_description_ref", searchable="reason"),
    "source_version": _columns(
        "source_id version_id kind display_name", "seq captured_at",
        "origin_zone origin_key content_fingerprint fingerprint_algorithm file_hash "
        "source_description_ref adapter_rule_version mapping_rule_version", searchable="display_name"),
})


SEARCHABLE_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    family: tuple(column.name for column in columns if column.searchable)
    for family, columns in SCHEMA_COLUMNS.items()
})
