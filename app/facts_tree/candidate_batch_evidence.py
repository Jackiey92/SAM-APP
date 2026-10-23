"""Batch composition: structure first, then host evidence binding.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 sections 3.3-3.5 and 7. This module is the single pure entry
point that chains the two existing validators for one fact-candidate/1
extraction batch:

1. Host context gate. company_id, file_hash, mapped_fingerprint and
   current_source_loc must be non-empty strings, grants must be a list
   of grant dicts, attributions must be a dict or None, and qualifiers
   must be None or a dict mapping each candidate local_id (a non-empty
   string) to that candidate's own evidence_ref -> credential dict.
   Any violation returns host_context_invalid errors immediately: no
   schema or evidence validation runs against untrusted host inputs.
2. Structure. candidate_schema.validate_batch(payload) runs next. Any
   structural error is returned unchanged and the evidence validator
   is never called for the batch.
3. Binding. Only a structurally valid batch reaches this layer:

   - complete=false: the schema already guarantees exactly one
     remaining_source_locs entry; this layer requires it to equal the
     host's current source_loc exactly (no normalization), else
     remaining_source_loc_mismatch. complete=true keeps the schema's
     empty-array rule; this layer adds nothing to it.
   - every candidate is handed to
     candidate_evidence.validate_candidate_evidence with its error
     paths rooted at "$.items[i]", the same host grants, attributions
     and the host's trusted current_source_loc, plus only its own
     qualifier scope: per P01 section 3.5 an evidence_ref is unique
     inside one candidate only, so the batch host qualifiers are a
     nested local_id -> evidence_ref -> credential mapping and this
     layer selects qualifiers[candidate.local_id] before delegating.
     Two candidates may therefore reuse the same evidence_ref for
     different windows without colliding, and one candidate can never
     borrow another candidate's credential. Evidence cited by
     field_bindings.statement must come from the current attributed
     read window, while non-statement qualifier fields may still cite
     a host-displayed re-read window of the same file and mapping
     version.

A pass here means exactly what the two delegated layers prove together
— the batch has a legal fact-candidate/1 shape, every candidate's
evidence matches the caller-provided host read-window and attribution
records, and an incomplete batch points at the host's current
source_loc. It does not prove extraction completeness (the employee's
complete flag is not window-coverage verification, P01 section 4), an
authenticated actor, a policy id/revision, a delegation chain, a
revocation state or an authorization credential, and it says nothing
about persistence, receipts or entry into the tree. No runtime entry,
candidate store, formal facts Git, Commit Broker, configuration or
feature gate is touched here.

The function is pure: no disk, no Git, no database, no model, no
network, and no input object (payload, grants, attributions,
qualifiers) is ever mutated. Errors from the two delegated validators
are returned as their original objects; errors raised by this layer
are BatchEvidenceError instances with the same stable
path/code/message/suggestion fields and as_dict().

Rule codes introduced here: host_context_invalid,
remaining_source_loc_mismatch. Every other code comes unchanged from
candidate_schema or candidate_evidence; their semantics are delegated,
never copied or relaxed.
"""
from dataclasses import dataclass

from app.facts_tree import candidate_evidence
from app.facts_tree import candidate_schema


@dataclass(frozen=True)
class BatchEvidenceError:
    """One composition-layer violation: path, rule code and fix hint."""
    path: str
    code: str
    message: str
    suggestion: str

    def as_dict(self):
        """Return the error as a plain JSON-compatible dict."""
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def validate_candidate_batch_evidence(payload, *, company_id, file_hash,
                                      mapped_fingerprint,
                                      current_source_loc, grants,
                                      attributions, qualifiers=None):
    """Validate one fact-candidate/1 batch against the host context.

    Runs the three layers described in the module docstring in order:
    host context gate, candidate_schema.validate_batch, then the exact
    remaining_source_locs[0] vs current_source_loc comparison for
    complete=false and the per-candidate evidence binding. qualifiers
    is the nested local_id -> evidence_ref -> credential mapping; each
    candidate receives only its own inner map (or None when the host
    recorded no credentials for it). Returns a flat list mixing
    CandidateSchemaError, EvidenceBindingError and BatchEvidenceError
    objects; an empty list means the batch passed all three layers,
    within the boundary stated in the module docstring.
    """
    errors = _validate_host_context(
        company_id=company_id, file_hash=file_hash,
        mapped_fingerprint=mapped_fingerprint,
        current_source_loc=current_source_loc, grants=grants,
        attributions=attributions, qualifiers=qualifiers)
    if errors:
        return errors

    errors = candidate_schema.validate_batch(payload)
    if errors:
        return errors

    combined = []
    if not payload["complete"]:
        # validate_batch guarantees exactly one non-empty string entry
        # here; P01 section 3.3 requires it to be the host's current
        # source_loc, compared exactly and never normalized.
        remaining = payload["remaining_source_locs"][0]
        if remaining != current_source_loc:
            combined.append(BatchEvidenceError(
                "$.remaining_source_locs[0]",
                "remaining_source_loc_mismatch",
                "complete=false requires remaining_source_locs[0] to be "
                "exactly the host's current source_loc %r, got %r"
                % (current_source_loc, remaining),
                "list the current host source_loc of the unfinished "
                "window; never carry another window's or a guessed "
                "location"))
    for position, candidate in enumerate(payload["items"]):
        # P01 section 3.5: an evidence_ref is unique only inside its
        # own candidate, so the batch-level qualifiers are scoped by
        # local_id; hand each candidate only its own inner map. A
        # candidate without an entry simply has no trusted credentials.
        scoped_qualifiers = None
        if qualifiers is not None:
            scoped_qualifiers = qualifiers.get(candidate["local_id"])
        combined.extend(candidate_evidence.validate_candidate_evidence(
            candidate, company_id=company_id, file_hash=file_hash,
            mapped_fingerprint=mapped_fingerprint,
            current_source_loc=current_source_loc, grants=grants,
            attributions=attributions, qualifiers=scoped_qualifiers,
            path="$.items[%d]" % position))
    return combined


def _validate_host_context(*, company_id, file_hash, mapped_fingerprint,
                           current_source_loc, grants, attributions,
                           qualifiers):
    """Fail closed on host inputs the binding layer silently relies on.

    Only container shapes are checked here; the internal rules of a
    grant, an attribution range or a qualifier credential stay with
    candidate_evidence, which owns those semantics. Qualifier scope
    keys are not compared with the batch local_ids at this point: the
    batch has not passed schema validation yet, and a scope that no
    candidate claims is simply never selected.
    """
    errors = []

    def host_error(name, message, suggestion):
        errors.append(BatchEvidenceError(
            "$.host.%s" % name, "host_context_invalid", message,
            suggestion))

    for name, value in (("company_id", company_id),
                        ("file_hash", file_hash),
                        ("mapped_fingerprint", mapped_fingerprint),
                        ("current_source_loc", current_source_loc)):
        if not isinstance(value, str) or not value:
            host_error(
                name,
                "host %s must be a non-empty string, got %s"
                % (name, type(value).__name__),
                "the host injects its own trusted value; never derive "
                "it from the candidate batch")
    if not isinstance(grants, list):
        host_error(
            "grants",
            "host grants must be a list of rebuilt grant dicts, got %s"
            % type(grants).__name__,
            "pass the host-rebuilt read grants as a list; an empty "
            "list is legal and simply grants nothing")
    elif any(not isinstance(grant, dict) for grant in grants):
        host_error(
            "grants",
            "every host grant must be a rebuilt grant dict",
            "pass only MDWindows.grant()-shaped dicts; never repair "
            "them from candidate data")
    if attributions is not None and not isinstance(attributions, dict):
        host_error(
            "attributions",
            "host attributions must be a dict mapping read_grant_id to "
            "its persisted attribution list, got %s"
            % type(attributions).__name__,
            "pass the persisted attribution lists keyed by their own "
            "read_grant_id, or None")
    if qualifiers is not None:
        if not isinstance(qualifiers, dict):
            host_error(
                "qualifiers",
                "host qualifiers must be a dict mapping candidate "
                "local_id to that candidate's own evidence_ref -> "
                "credential dict, got %s" % type(qualifiers).__name__,
                "pass the trusted host qualifier credentials scoped "
                "by candidate local_id, or None")
        elif any(not isinstance(key, str) or not key
                 or not isinstance(value, dict)
                 for key, value in qualifiers.items()):
            host_error(
                "qualifiers",
                "host qualifiers must map each candidate local_id "
                "(non-empty string) to that candidate's own "
                "evidence_ref -> credential dict",
                "scope the trusted host qualifier credentials by the "
                "candidate local_id they belong to; a flat "
                "evidence_ref map cannot express per-candidate scope")
    return errors
