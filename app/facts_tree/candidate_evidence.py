"""Host-side binding check: candidate evidence vs real read grants.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 sections 3.2, 3.5 and 7. candidate_schema.py proves a
fact-candidate/1 batch has a legal shape; this module performs the next
program layer for one already-structurally-valid candidate: every
evidence item is re-bound against the host's own trusted inputs — the
current request company_id, the registered file_hash, the exact
mapped_fingerprint, the host's trusted current source_loc, the
host-rebuilt read grants and the persisted attribution ranges
recorded for those same grants.

Host shapes mirrored here (see app/facts_tree/md_reader.MDWindows.grant
and app/document_read/service.py window preparation):

- A rebuilt grant carries grant_id, company_id, file_hash,
  mapped_fingerprint, source_loc, ranges and lines. ``ranges`` is a
  non-empty list of [start_line, end_line] integer pairs, never empty,
  never duplicated and never overlapping; ``lines`` maps the 1-based
  line number as a canonical positive-integer decimal string
  (``str(int(key)) == key``, so ``"03"`` is malformed) to the original
  line text, which must be a string. The union of the ranges must be
  exactly the set of line keys: ranges/lines misalignment is rejected,
  and cited coordinates must fall inside both, never only inside the
  lines dict. Range/line agreement is verified arithmetically against
  the actual granted line count; no unbounded ``set(range(...))`` or
  span traversal is ever materialized from host- or employee-supplied
  endpoints, so a corrupt huge endpoint fails closed as a structural
  error instead of exhausting memory or CPU.
- The persisted attribution receipt of a grant (``prepared['ranges']``
  in the host service) is a list of dicts
  ``{'entity': str, 'start_line': int, 'end_line': int}``.
  ``attribution_index`` indexes exactly that list; the selected entry
  and its whole line interval must lie inside the same read grant.

What a pass here means, exactly per P01 section 3.5/7: the evidence
coordinates and quote match an actual host-supplied read window of this
company/file/mapping version, the declared lines are a subset of the
granted lines and ranges, the quote is continuous original text inside
the declared authorized lines, and any attribution_index resolves
inside that same grant's own persisted attribution list with the
evidence coordinates contained in the selected range. On top of that,
every evidence item cited by field_bindings.statement binds a grant
whose source_loc is exactly the host's trusted current source_loc:
the primary statement evidence must come from the current attributed
read window, while non-statement qualifier fields may still cite a
host-displayed re-read window of the same file and mapping version.
The current source_loc is a host input only; it is never derived
from candidate data, and a missing or non-string value fails closed
instead of skipping the rule.

What a pass here does NOT mean (the boundary is deliberate):

- MDWindows.grant() grant_id values are per-task read-window coordinate
  identifiers, not policy-authorization proofs. This module proves the
  evidence matches the caller-provided host read-window/attribution
  records; it does not and cannot prove an authenticated actor, a
  policy id/revision, a delegation chain, a revocation state or an
  authorization credential. The current codebase has no trusted source
  of those identity/authorization values, so none are fabricated here
  and no formal host envelope is constructed.
- attribution_index may be null only for evidence that
  field_bindings.statement does not list, that at least one
  non-statement field binding cites, and that the caller binds to a
  trusted host structural classification credential with
  qualifier_kind header|footnote plus the same evidence_ref,
  read_grant_id and exact start/end coordinates (the header/footnote
  qualifiers of P01 section 3.5). Without that credential a stable
  missing_qualifier_kind error is returned; a credential that binds
  different coordinates is qualifier_binding_mismatch. The current
  Dossier map does NOT provide such trusted structural labels, so
  today there is no integration source for them; this module never
  infers labels, never opens a runtime entry, and a pass with a
  caller-supplied credential still only proves the binding and the
  authorized range, not an end-to-end header/footnote pipeline.
- Quote comparison uses only the whitespace normalization defined
  here: for matching, every Unicode whitespace character is deleted
  and the remaining characters are compared one by one (the same
  explicit rule as app/facts_tree/md_publish.py and
  app/document_read/service.py: ``''.join(text.split())``). The
  candidate's original quote is never rewritten. Negations, digits and
  punctuation are never relaxed. A repeated quote is disambiguated
  solely by the declared exact line range; the first occurrence is
  never picked by default.

The function is pure: no disk, no Git, no database, no model, no
network, and no input object is ever mutated. Errors are structured
with a "$"-rooted field path, a stable rule code and a correction
suggestion, mirroring candidate_schema.CandidateSchemaError.

Rule codes used: type_object, type_string, type_integer, type_boolean,
bool_as_int, unknown_grant_id, duplicate_grant_id, grant_malformed,
grant_range_invalid, grant_lines_mismatch, company_mismatch,
file_mismatch, mapping_mismatch, source_loc_mismatch, line_not_granted,
quote_mismatch, quote_line_range_mismatch, quote_ambiguous,
missing_attribution_index, missing_attribution_ranges,
attribution_index_out_of_range, attribution_range_invalid,
attribution_range_outside_grant, attribution_range_exceeded,
null_attribution_unbound, missing_qualifier_kind,
qualifier_binding_mismatch, current_source_loc_invalid,
statement_not_current_window.
"""
from dataclasses import dataclass

# Keys a rebuilt MDWindows.grant() record must carry for binding.
_GRANT_REQUIRED_KEYS = (
    "grant_id", "company_id", "mapped_fingerprint", "file_hash",
    "source_loc", "ranges", "lines",
)

_QUALIFIER_KINDS = ("header", "footnote")


@dataclass(frozen=True)
class EvidenceBindingError:
    """One binding violation: field path, rule code and fix hint."""
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


def normalize_whitespace(text):
    """The only quote normalization allowed by P01 section 3.5.

    For matching only, every Unicode whitespace character is deleted
    (``''.join(text.split())``); the remaining characters — digits,
    negations, punctuation included — are preserved verbatim and
    compared one by one. The candidate's original quote is never
    rewritten.
    """
    return "".join(text.split())


def validate_candidate_evidence(candidate, *, company_id, file_hash,
                                mapped_fingerprint, current_source_loc,
                                grants, attributions,
                                qualifiers=None, path="$"):
    """Bind one structurally valid candidate's evidence to host inputs.

    Parameters:

    - candidate: one candidate object that already passed
      candidate_schema.validate_candidate(). This module still rechecks
      the exact integer/line types it relies on, fail closed.
    - company_id / file_hash / mapped_fingerprint: the host's trusted
      values for the current request; employee-echoed values in the
      candidate are never consulted.
    - current_source_loc: the host's trusted source_loc of the current
      read window for this request. Every evidence item cited by
      field_bindings.statement must bind a grant with exactly this
      source_loc (P01 section 3.5); re-read windows of the same file
      and mapping version remain usable for non-statement qualifier
      fields only. Never derived from the candidate; a missing,
      non-string or empty value fails closed with
      current_source_loc_invalid instead of skipping the rule.
    - grants: the host-rebuilt read grants for this task, as a list of
      MDWindows.grant()-shaped dicts (grant_id, company_id, file_hash,
      mapped_fingerprint, source_loc, ranges, lines). Index entries or
      guessed grants are not acceptable inputs.
    - attributions: mapping of read_grant_id to that same grant's
      persisted attribution list, each entry a dict
      {'entity': str, 'start_line': int, 'end_line': int} as persisted
      by the host (app/document_read/service.py prepared['ranges']).
      Attribution indexes are interpreted per evidence item's own
      read_grant_id only; borrowing across grants is impossible here.
    - qualifiers: optional mapping of evidence_ref to a trusted host
      structural classification credential dict with qualifier_kind
      ("header" or "footnote"), read_grant_id, start_line and end_line
      exactly matching the evidence item. Required for any evidence
      whose attribution_index is null; the current Dossier map has no
      trusted source of these labels and none is inferred here.
    - path: root used in error paths (defaults to "$").

    Returns a list of EvidenceBindingError; an empty list means every
    evidence item matches the host-provided window/attribution records,
    within the boundary stated in the module docstring.
    """
    if not isinstance(current_source_loc, str) or not current_source_loc:
        # The statement/current-window rule cannot be evaluated without
        # the host's trusted value; fail closed, never skip it.
        return [EvidenceBindingError(
            "%s.host.current_source_loc" % path,
            "current_source_loc_invalid",
            "host current_source_loc must be a non-empty string, got %s"
            % type(current_source_loc).__name__,
            "the host injects its own trusted current source_loc; "
            "never derive it from candidate data or leave it out")]
    errors = []
    grants_by_id, duplicate_ids = _index_grants(grants)

    evidence = candidate.get("evidence")
    bindings = candidate.get("field_bindings")
    if not isinstance(evidence, list) or not isinstance(bindings, dict):
        # Structural validity is candidate_schema's job; without the
        # evidence array and field bindings there is nothing to bind.
        return errors

    statement_refs = _referenced_refs(bindings, "statement")
    non_statement_refs = set()
    for key, value in bindings.items():
        if key == "statement":
            continue
        non_statement_refs.update(_referenced_refs(bindings, key))

    for position, item in enumerate(evidence):
        item_path = "%s.evidence[%d]" % (path, position)
        if not isinstance(item, dict):
            continue  # structural error already reported upstream
        _bind_one(item, item_path, errors, grants_by_id, duplicate_ids,
                  attributions, qualifiers or {}, company_id, file_hash,
                  mapped_fingerprint, current_source_loc,
                  statement_refs, non_statement_refs)
    return errors


def _referenced_refs(bindings, key):
    value = bindings.get(key)
    if not isinstance(value, list):
        return set()
    return {ref for ref in value if isinstance(ref, str)}


def _index_grants(grants):
    """Map grant_id -> grant; collect ids declared more than once."""
    by_id = {}
    duplicates = set()
    for grant in grants or ():
        if not isinstance(grant, dict):
            continue
        grant_id = grant.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            continue
        if grant_id in by_id:
            duplicates.add(grant_id)
        else:
            by_id[grant_id] = grant
    return by_id, duplicates


def _err(errors, path, code, message, suggestion):
    errors.append(EvidenceBindingError(path, code, message, suggestion))


def _check_grant_windows(grant, grant_id, grant_id_path, errors):
    """Validate grant ranges/lines; return sorted ranges or None.

    ranges must be a non-empty list of [start, end] integer pairs with
    no duplicates and no overlaps, and their union must be exactly the
    set of line keys; ranges/lines misalignment fails closed. All
    agreement checks are arithmetic (sorted endpoints, span counts and
    per-line membership against the sorted ranges), so an unrestricted
    huge endpoint is rejected without ever materializing the span.
    """
    ranges = grant.get("ranges")
    lines = grant.get("lines")
    if not isinstance(ranges, list) or not ranges:
        _err(errors, grant_id_path, "grant_range_invalid",
             "grant %r has no read ranges; an empty window grants "
             "nothing" % grant_id,
             "the host must rebuild the read window with its real "
             "authorized ranges; never invent them")
        return None
    entries = []
    for entry in ranges:
        if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                or isinstance(entry[0], bool) or isinstance(entry[1], bool)
                or type(entry[0]) is not int or type(entry[1]) is not int
                or entry[0] < 1 or entry[0] > entry[1]):
            _err(errors, grant_id_path, "grant_range_invalid",
                 "grant %r carries an illegal range entry %r"
                 % (grant_id, entry),
                 "the host must supply intact [start_line, end_line] "
                 "integer pairs; never repair them from candidate data")
            return None
        entries.append((entry[0], entry[1]))
    entries.sort()
    for previous, current in zip(entries, entries[1:]):
        if current[0] <= previous[1]:
            _err(errors, grant_id_path, "grant_range_invalid",
                 "grant %r carries duplicate or overlapping ranges"
                 % grant_id,
                 "the host must rebuild the read window; overlapping "
                 "authorization records are ambiguous")
            return None
    line_numbers = set()
    for key, text in lines.items():
        if (not isinstance(key, str) or not key.isascii()
                or not key.isdigit() or str(int(key)) != key):
            _err(errors, grant_id_path, "grant_malformed",
                 "grant %r has a non-canonical line key %r; keys must "
                 "be plain positive-integer decimal strings"
                 % (grant_id, key),
                 "the host must supply complete rebuilt read grants; "
                 "never repair them from candidate data")
            return None
        if not isinstance(text, str):
            _err(errors, grant_id_path, "grant_malformed",
                 "grant %r line %r does not hold string text"
                 % (grant_id, key),
                 "the host must supply the original line text; never "
                 "repair it from candidate data")
            return None
        line_numbers.add(int(key))
    covered_count = sum(hi - lo + 1 for lo, hi in entries)
    if (covered_count != len(line_numbers)
            or any(not _line_in_ranges(entries, number)
                   for number in line_numbers)):
        _err(errors, grant_id_path, "grant_lines_mismatch",
             "grant %r ranges cover %d lines but the lines dict holds "
             "%d; ranges and lines disagree"
             % (grant_id, covered_count, len(line_numbers)),
             "the host must rebuild the read window so the ranges and "
             "the granted line text describe the same lines")
        return None
    return entries


def _line_in_ranges(sorted_ranges, number):
    """True when number lies inside one of the sorted disjoint ranges."""
    lo, hi = 0, len(sorted_ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        start, end = sorted_ranges[mid]
        if number < start:
            hi = mid
        elif number > end:
            lo = mid + 1
        else:
            return True
    return False


def _bind_one(item, item_path, errors, grants_by_id, duplicate_ids,
              attributions, qualifiers, company_id, file_hash,
              mapped_fingerprint, current_source_loc, statement_refs,
              non_statement_refs):
    def child(key):
        return "%s.%s" % (item_path, key)

    grant_id = item.get("read_grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        return  # structural layer reports this
    grant_id_path = child("read_grant_id")
    if grant_id in duplicate_ids:
        _err(errors, grant_id_path, "duplicate_grant_id",
             "read_grant_id %r is declared by more than one host grant; "
             "the record is ambiguous" % grant_id,
             "the host must rebuild the read window records; never pick "
             "one of the duplicates")
        return
    grant = grants_by_id.get(grant_id)
    if grant is None:
        _err(errors, grant_id_path, "unknown_grant_id",
             "read_grant_id %r does not match any host-provided read "
             "grant" % grant_id,
             "cite a read window the host actually displayed in this "
             "request; an index entry or a guessed id is not a grant")
        return
    missing = [key for key in _GRANT_REQUIRED_KEYS if key not in grant]
    if missing or not isinstance(grant.get("lines"), dict):
        _err(errors, grant_id_path, "grant_malformed",
             "the host grant %r is missing required keys %s"
             % (grant_id, missing),
             "the host must supply complete rebuilt read grants; never "
             "repair them from candidate data")
        return

    if grant.get("company_id") != company_id:
        _err(errors, grant_id_path, "company_mismatch",
             "grant %r belongs to a different company than the current "
             "request" % grant_id,
             "the host injects company_id; evidence can never cross "
             "company scope")
    if grant.get("file_hash") != file_hash:
        _err(errors, grant_id_path, "file_mismatch",
             "grant %r is for a different file_hash than the registered "
             "source of this request" % grant_id,
             "cite only windows of the currently registered source "
             "version")
    if grant.get("mapped_fingerprint") != mapped_fingerprint:
        _err(errors, grant_id_path, "mapping_mismatch",
             "grant %r was built from a different mapped_fingerprint "
             "than the exact mapping version of this request" % grant_id,
             "re-read with the current mapping version; coordinates "
             "from a stale mapping are not valid evidence")
    ref = item.get("evidence_ref")
    if (isinstance(ref, str) and ref in statement_refs
            and grant.get("source_loc") != current_source_loc):
        # P01 section 3.5: primary statement evidence must belong to
        # the current attributed read window; only non-statement
        # qualifier fields may cite a displayed re-read window.
        _err(errors, grant_id_path, "statement_not_current_window",
             "evidence %r is listed in field_bindings.statement but "
             "grant %r is window %r, not the host's current read "
             "window %r"
             % (ref, grant_id, grant.get("source_loc"),
                current_source_loc),
             "bind the primary statement evidence to the host's "
             "current attributed read window; a re-read window of the "
             "same file and mapping version only serves non-statement "
             "qualifier fields")
    if item.get("source_loc") != grant.get("source_loc"):
        _err(errors, child("source_loc"), "source_loc_mismatch",
             "source_loc %r does not match the grant's authorized "
             "source_loc %r" % (item.get("source_loc"),
                                grant.get("source_loc")),
             "use the exact source_loc of the granted window; no "
             "cross-source locating or splicing")

    lines = grant["lines"]
    granted_ranges = _check_grant_windows(grant, grant_id,
                                          grant_id_path, errors)
    if granted_ranges is None:
        return

    start = item.get("start_line")
    end = item.get("end_line")
    start_ok = end_ok = False
    for key in ("start_line", "end_line"):
        value = item.get(key)
        if isinstance(value, bool):
            _err(errors, child(key), "bool_as_int",
                 "%s must be a real integer, never a boolean" % key,
                 "pass a 1-based line number as a JSON integer")
        elif type(value) is int:
            if key == "start_line":
                start_ok = True
            else:
                end_ok = True
        else:
            _err(errors, child(key), "type_integer",
                 "%s must be an integer, got %s"
                 % (key, type(value).__name__),
                 "pass a 1-based line number as a JSON integer")
    coords_ok = start_ok and end_ok and start <= end
    if coords_ok:
        # Fail closed on an unrestricted huge declared span: anything
        # longer than the grant's finite line count cannot be granted,
        # and is rejected without enumerating the span.
        if end - start + 1 > len(lines):
            _err(errors, child("start_line"), "line_not_granted",
                 "the declared range %d-%d spans %d lines, more than "
                 "the %d lines grant %r authorizes"
                 % (start, end, end - start + 1, len(lines), grant_id),
                 "keep the quote inside actually granted lines; never "
                 "span an authorization gap or a line outside the "
                 "window")
            coords_ok = False
        else:
            missing_lines = [
                i for i in range(start, end + 1)
                if not _line_in_ranges(granted_ranges, i)
                or str(i) not in lines]
            if missing_lines:
                _err(errors, child("start_line"), "line_not_granted",
                     "lines %s in the declared range %d-%d are not "
                     "part of grant %r"
                     % (missing_lines, start, end, grant_id),
                     "keep the quote inside actually granted lines; "
                     "never span an authorization gap or a line "
                     "outside the window")
                coords_ok = False

    quote = item.get("quote")
    if coords_ok and isinstance(quote, str) and quote:
        _check_quote(quote, start, end, lines, item_path, errors)

    index_value = item.get("attribution_index")
    index_path = child("attribution_index")
    is_statement = isinstance(ref, str) and ref in statement_refs
    is_field_only = isinstance(ref, str) and ref in non_statement_refs

    if index_value is None:
        if is_statement:
            _err(errors, index_path, "missing_attribution_index",
                 "evidence %r is listed in field_bindings.statement, "
                 "so its attribution_index must be a real integer "
                 "index, not null" % ref,
                 "pass the index into this grant's own persisted "
                 "attribution list")
        elif not is_field_only:
            _err(errors, index_path, "null_attribution_unbound",
                 "attribution_index null is only allowed for evidence "
                 "that at least one non-statement field binding cites "
                 "as a field qualifier",
                 "cite this evidence from a non-statement field "
                 "binding, or give a real integer attribution_index")
        else:
            _check_qualifier(item, ref, grant_id, start, end, start_ok,
                             end_ok, qualifiers, index_path, errors)
        # Whether such evidence is genuinely a header/footnote
        # qualifier is a semantic question the current host mapping
        # cannot prove; only the caller-supplied trusted credential,
        # the binding shape and the authorized range are checked here.
        return
    if isinstance(index_value, bool):
        _err(errors, index_path, "bool_as_int",
             "attribution_index must be a real integer or null, never "
             "a boolean",
             "pass the integer index into this grant's persisted "
             "attribution list")
        return
    if type(index_value) is not int:
        _err(errors, index_path, "type_integer",
             "attribution_index must be an integer or null, got %s"
             % type(index_value).__name__,
             "pass a JSON integer or null")
        return
    if index_value < 0:
        _err(errors, index_path, "integer_range",
             "attribution_index must be zero or a positive integer, "
             "got %d" % index_value,
             "use the index into this grant's persisted attribution "
             "list")
        return

    ranges = attributions.get(grant_id) if attributions else None
    if not isinstance(ranges, list) or not ranges:
        _err(errors, index_path, "missing_attribution_ranges",
             "grant %r has no persisted attribution ranges, so no "
             "attribution_index is valid for it" % grant_id,
             "finish the attribution flow for that read window first; "
             "an index from another grant's list can never be "
             "borrowed")
        return
    if index_value >= len(ranges):
        _err(errors, index_path, "attribution_index_out_of_range",
             "attribution_index %d is outside the persisted "
             "attribution list of grant %r (length %d)"
             % (index_value, grant_id, len(ranges)),
             "index only into this evidence's own read_grant_id "
             "attribution list")
        return
    selected = ranges[index_value]
    if (not isinstance(selected, dict)
            or not isinstance(selected.get("entity"), str)
            or not selected.get("entity", "").strip()
            or isinstance(selected.get("start_line"), bool)
            or isinstance(selected.get("end_line"), bool)
            or type(selected.get("start_line")) is not int
            or type(selected.get("end_line")) is not int
            or selected["start_line"] > selected["end_line"]):
        _err(errors, index_path, "attribution_range_invalid",
             "the persisted attribution entry %d of grant %r is not a "
             "{'entity', 'start_line', 'end_line'} dict"
             % (index_value, grant_id),
             "the host must supply intact persisted attribution "
             "ranges; never repair them")
        return
    span = selected["end_line"] - selected["start_line"] + 1
    if span > len(lines):
        # Same fail-closed guard: a persisted attribution wider than
        # the whole finite grant is outside it by definition.
        _err(errors, index_path, "attribution_range_outside_grant",
             "the persisted attribution entry %d of grant %r spans %d "
             "lines, more than the %d lines the grant authorizes"
             % (index_value, grant_id, span, len(lines)),
             "the persisted attribution must lie inside the same "
             "authorized read window; never widen it")
        return
    outside = [i for i in range(selected["start_line"],
                                selected["end_line"] + 1)
               if not _line_in_ranges(granted_ranges, i)
               or str(i) not in lines]
    if outside:
        _err(errors, index_path, "attribution_range_outside_grant",
             "the persisted attribution entry %d of grant %r spans "
             "lines %s that are outside the current read grant"
             % (index_value, grant_id, outside),
             "the persisted attribution must lie inside the same "
             "authorized read window; never widen it")
        return
    if coords_ok and not (selected["start_line"] <= start
                          and end <= selected["end_line"]):
        _err(errors, index_path, "attribution_range_exceeded",
             "evidence coordinates %d-%d are not contained in the "
             "selected attribution range %d-%d of grant %r"
             % (start, end, selected["start_line"],
                selected["end_line"], grant_id),
             "keep the evidence coordinates inside the attribution "
             "range the index selects")


def _check_qualifier(item, ref, grant_id, start, end, start_ok, end_ok,
                     qualifiers, index_path, errors):
    """Null attribution_index needs a trusted host qualifier credential.

    The credential must carry qualifier_kind header|footnote bound to
    the same evidence_ref, read_grant_id and exact coordinates. The
    current Dossier map provides no such labels, so nothing is inferred
    here; without the credential the error is stable.
    """
    credential = qualifiers.get(ref)
    if (not isinstance(credential, dict)
            or credential.get("qualifier_kind") not in _QUALIFIER_KINDS):
        _err(errors, index_path, "missing_qualifier_kind",
             "attribution_index null for field-qualifier evidence %r "
             "requires a trusted host structural classification "
             "credential with qualifier_kind header|footnote; the "
             "current host mapping provides none and labels are never "
             "inferred" % ref,
             "supply the host credential binding qualifier_kind, "
             "read_grant_id and the exact coordinates, or give a real "
             "integer attribution_index")
        return
    if (credential.get("read_grant_id") != grant_id
            or not start_ok or not end_ok
            or credential.get("start_line") != start
            or credential.get("end_line") != end):
        _err(errors, index_path, "qualifier_binding_mismatch",
             "the qualifier credential for evidence %r does not bind "
             "the same read_grant_id and exact start/end coordinates"
             % ref,
             "the credential must name this evidence's own grant and "
             "precise line range; never reuse a credential from "
             "another window or span")
        return


def _check_quote(quote, start, end, lines, item_path, errors):
    """Verify the quote against the declared authorized lines.

    Only the declared [start, end] lines participate, so a match can
    never cross an unauthorized line. The quote must occur exactly once
    with its first character on start_line and its last on end_line;
    when the same text occurs more than once the declared precise range
    decides, and no first occurrence is ever picked by default.
    """
    needle = normalize_whitespace(quote)
    if not needle:
        _err(errors, "%s.quote" % item_path, "quote_mismatch",
             "the quote normalizes to empty text",
             "quote the actual original passage")
        return
    parts = []
    char_lines = []
    for lineno in range(start, end + 1):
        norm = normalize_whitespace(lines[str(lineno)])
        parts.append(norm)
        char_lines.extend([lineno] * len(norm))
    joined = "".join(parts)

    occurrences = []
    cursor = joined.find(needle)
    while cursor != -1:
        occurrences.append(cursor)
        cursor = joined.find(needle, cursor + 1)
    quote_path = "%s.quote" % item_path
    if not occurrences:
        _err(errors, quote_path, "quote_mismatch",
             "the quote does not appear in the declared authorized "
             "lines %d-%d (only whitespace removal is allowed)"
             % (start, end),
             "copy the continuous original passage; never splice, "
             "paraphrase or alter digits/negations")
        return
    exact = [pos for pos in occurrences
             if char_lines[pos] == start
             and char_lines[pos + len(needle) - 1] == end]
    if not exact:
        _err(errors, quote_path, "quote_line_range_mismatch",
             "the quote appears in lines %d-%d but its actual span "
             "does not start on start_line %d and end on end_line %d"
             % (start, end, start, end),
             "declare the quote's real line range; when the same "
             "passage occurs several times, give the precise lines of "
             "the occurrence meant")
        return
    if len(exact) > 1:
        _err(errors, quote_path, "quote_ambiguous",
             "the quote matches %d times inside the declared range "
             "%d-%d with the same start and end lines"
             % (len(exact), start, end),
             "narrow start_line/end_line so the intended occurrence is "
             "unambiguous; the first match is never assumed")
        return
