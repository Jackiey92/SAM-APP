"""Action-scoped mapped-MD employee output descriptions (no business inference)."""


def response_contract(action):
    if action == 'classify':
        return dict(action=action,required=['classification'],
            example=dict(classification=dict(description='Employee describes current granted material.')),
            forbidden=['ranges','facts','assignments','retain_context_windows','retention_reason'],
            boundary='Only classify this window; context is read-only supporting material.')
    if action == 'attribute':
        return dict(action=action,required=['ranges'],
            example=dict(ranges=[dict(entity='<source entity>',start_line=1,end_line=1)],outcome='attributed'),
            empty_example=dict(ranges=[],outcome='no_attribution',reason='Employee explains lack of attributable entity.'),
            forbidden=['classification','facts','assignments','retain_context_windows','retention_reason'],
            boundary='Absolute integer lines in current window only; no inferred entity; empty needs nonempty reason.')
    if action == 'extract':
        return dict(action=action,required=['facts','complete','remaining_source_locs'],
            fact_required=['entity','attribute','period','value','unit','value_type','quote','source_loc','attribution_index','module_key'],
            field_evidence=dict(required=['field','grant_id','company_id','file_hash','mapped_fingerprint','source_loc','start_line','end_line','quote'],
                fields=['period','unit','entity'],max_fields=3,
                boundary='Exact literal field evidence in current/displayed grants; main quote/value remain current attributed lines.'),
            example=dict(facts=[],complete=True,remaining_source_locs=[]),
            incomplete_example=dict(facts=[],complete=False,remaining_source_locs=['<current source_loc>']),
            forbidden=['classification','assignments','retain_context_windows','retention_reason','outcome'],
            boundary='Facts only. Qualifier-only window may have no facts; later independent plan decides original grant retention. Never return a plan receipt here.')
    if action == 'plan':
        from ..facts_tree.md_reader import allocation_contract
        return allocation_contract()
    if action == 'leaf':
        from ..facts_tree.reading_context import block_support_contract
        return dict(action=action,field_support=block_support_contract(),required=['leaf_code','base_version','base_sha','blocks','conflicts','note'],
            block_required=['block_id','text','evidence'],
            evidence_schema=dict(type='object',properties=dict(grant_id=dict(type='string'),
                fact_ids=dict(type='array',items=dict(type='integer',minimum=1)),
                start_line=dict(type='integer'),end_line=dict(type='integer'))),
            base_version_type='integer',
            evidence_required=['ref','grant_id','file_hash','original_name','source_loc','start_line','end_line','fact_ids'],
            conflict_required=['old_block_id','new_block','reason'],
            required_applies_to=("the top-level 'required' list applies to a normal submission only; "
                'no_content and unresolved are alternative receipts governed by their own required fields, '
                'and must not include blocks or base version fields'),
            no_content=dict(required=['leaf_code','outcome','reason'],outcome='no_content',
                boundary='Nonempty employee reason; no content changes, placeholder or assignments.'),
            unresolved=dict(required=['leaf_code','outcome','reason'],outcome='unresolved',
                boundary=("Employee nonempty explanation when comparison cannot be decided with the "
                          'authorized evidence; the target is recorded unresolved with no content changes, '
                          'fabricated facts, automatic conflicts or authority beyond assigned grants.')),
            forbidden=['assignments','retain_context_windows','retention_reason','context_windows'],
            boundary='Compare and submit this target only; preserve current.blocks exactly, use only assigned grants for additions. Plan selects memory, leaf does not.')
    raise ValueError('unknown mapped MD action')


def response_contract_v4(action):
    """Protocol V4 reading-phase contracts. V1 has no leaf action at all."""
    if action == 'classify':
        return dict(action=action, required=['classification'],
            example=dict(classification=dict(description='Employee describes current granted material.')),
            forbidden=['ranges', 'facts', 'creates', 'revisions', 'assignments', 'target_leaf_code',
                       'retain_context_windows', 'retention_reason'],
            boundary='Only classify this window; context grants are read-only supporting material. No leaf decisions.')
    if action == 'attribute':
        return dict(action=action, required=['ranges'],
            example=dict(ranges=[dict(entity='<source entity>', start_line=1, end_line=1)], outcome='attributed'),
            empty_example=dict(ranges=[], outcome='no_attribution', reason='Employee explains lack of attributable entity.'),
            request_context_alternative=dict(
                receipt=dict(outcome='request_context', request_context_windows=['<grant_id from read_grant_index>'],
                             reason='nonempty reason, max 2000 characters'),
                mutex='a request_context response contains exactly those three keys and no ranges/classification/facts',
                inputs='read_grant_index is coordinates (grant_id/source_loc/ranges) of earlier truly-read windows, not text; '
                       'existing_notes is the quote-free compact notebook; both are read-only orientation',
                result='the granted original windows return as context_tool_results on the next same-phase request, '
                       'billed against the attribute phase budget',
                bounds='at most two distinct earlier-read grants per window; temporary redisplay only, never a retain_context_windows entry'),
            forbidden=['classification', 'facts', 'creates', 'revisions', 'assignments', 'target_leaf_code',
                       'retain_context_windows', 'retention_reason'],
            boundary='Absolute integer lines in current window only; no inferred entity; empty needs nonempty reason.')
    if action == 'extract':
        return dict(action=action, required=['facts', 'complete', 'remaining_source_locs'],
            fact_required=['entity', 'attribute', 'period', 'value', 'unit', 'value_type', 'quote',
                           'source_loc', 'attribution_index'],
            field_evidence=dict(required=['field', 'grant_id', 'company_id', 'file_hash',
                'mapped_fingerprint', 'source_loc', 'start_line', 'end_line', 'quote'],
                fields=['period', 'unit', 'entity'], max_fields=3,
                boundary='Exact literal field evidence in current/displayed grants (including granted revisit windows); '
                         'main quote/value remain current attributed lines. Distinguish unknown period / empty unit from null: '
                         'never invent dates or numbers; request_context the earlier window that has the literal instead.'),
            example=dict(facts=[], complete=True, remaining_source_locs=[]),
            incomplete_example=dict(facts=[], complete=False, remaining_source_locs=['<current source_loc>']),
            request_context_alternative=dict(
                receipt=dict(outcome='request_context', request_context_windows=['<grant_id from read_grant_index>'],
                             reason='nonempty reason, max 2000 characters'),
                mutex='a request_context response contains exactly those three keys and no facts/complete/remaining_source_locs',
                inputs='read_grant_index is coordinates of earlier truly-read windows, not text authorization; '
                       'existing_notes is the quote-free compact notebook with earlier periods/units; both are read-only',
                result='the granted original windows return as context_tool_results on the next extract request, '
                       'billed against the extract phase budget; a newly displayed grant counts as tool progress',
                bounds='at most two distinct earlier-read grants per window; temporary redisplay only, never a retain_context_windows entry; '
                       'repeating the same IDs without a new grant is a bounded stall (max two)'),
            no_progress='a normal extract delivery must add a new valid candidate or advance coverage; two consecutive '
                        'deliveries that do neither stop the window; a genuinely fact-free title window closes with facts=[]/complete=true',
            forbidden=['creates', 'revisions', 'assignments', 'target_leaf_code',
                       'retain_context_windows', 'retention_reason', 'non-null module_key'],
            boundary=('V4 reading phase: facts become host-verified window candidates for the separate note receipt; '
                      'extract never selects a trunk/leaf or writes notes; omit module_key (null is tolerated only '
                      'while old V4 workers are in flight). The publish phase alone assigns each fact group to one '
                      'top1 leaf, whose trunk determines module_key. Qualifier-only window may have no facts. '
                      "Normal fact deliveries carry no 'outcome' field; outcome is used only by the request_context alternative."))
    if action == 'plan':
        from .notes import note_receipt_contract
        return note_receipt_contract()
    if action == 'review':
        from .notes import note_review_contract
        return note_review_contract()
    if action == 'publish':
        # V2-1 post-review publication: top1 leaf assignment + append-only
        # compare_submit. The contract text lives next to the publisher so the
        # host never maintains two divergent copies.
        from ..facts_tree.notes_publish import publish_contract
        return publish_contract()
    raise ValueError('protocol V4 has no leaf action; publication uses the publish action only')
