"""Bounded read authority and literal field evidence, never period inference."""
import json
from .grants import range_is_granted
from .leaf_store import LeafRejected

PROTOCOL = 3
MAX_CONTEXT_BYTES = 128000
MAX_READING_REQUEST_BYTES = 256000
UNKNOWN_PERIODS = ('', 'unknown', 'unspecified', '未知')


def compact(value):
    return ''.join(value.split())


FIELD_LITERAL_PREFIX = 'field literal/quote absent from declared read evidence'


def _bounded_fragment(value, limit):
    """Employee-submitted text only; never applied to source lines directly."""
    try:
        fragment = value if isinstance(value, str) else f'<{type(value).__name__}>'
        fragment = compact(fragment)
    except Exception:
        return '<unrepresentable>'
    return fragment[:limit]


def _field_literal_error(field, value, verified_quote):
    """Diagnostic for the existing literal rejection. Echo only a quote that has
    already been verified inside the granted lines; never a forged quote or any
    other grant text. No host-corrected value is ever suggested."""
    try:
        name = field if field in ('period', 'unit', 'entity') else 'field'
        if verified_quote is None:
            detail = (f'[{name}] declared quote is not verified in the actually granted scope, '
                      'so it is not echoed and no other grant or reread is provided')
            submitted = _bounded_fragment(value, 80)
            if submitted:
                detail += f'; submitted value="{submitted}"'
            return (FIELD_LITERAL_PREFIX + '; ' + detail)[:1000]
        if not isinstance(value, str) or not value.strip():
            detail = (f'[{name}] field value must be a nonempty string found verbatim in the cited evidence; '
                      f'submitted type={type(value).__name__}')
            return (FIELD_LITERAL_PREFIX + '; ' + detail)[:1000]
        submitted = _bounded_fragment(value, 80)
        quote_fragment = _bounded_fragment(verified_quote, 240)
        detail = (f'[{name}] submitted value="{submitted}" does not occur character-for-character '
                  f'in the verified evidence quote="{quote_fragment}"; the value must be copied verbatim '
                  'from submitted evidence without reformatting dates or units; resubmit yourself, '
                  'no host-corrected value is provided')
        return (FIELD_LITERAL_PREFIX + '; ' + detail)[:1000]
    except Exception:
        # A diagnostic must never mask the rejection or leak unauthorized text.
        return FIELD_LITERAL_PREFIX


def bounded_context(grants):
    if len(grants)>4: raise LeafRejected('reading context exceeds four grants')
    if len(json.dumps(grants,ensure_ascii=False).encode())>MAX_CONTEXT_BYTES:
        raise LeafRejected('reading context exceeds byte bound; no truncated context granted')
    return grants


def field_evidence(item, current, contexts, company):
    """Verify employee-chosen field witnesses without concatenating source text."""
    evidence=item.get('field_evidence',[])
    if not isinstance(evidence,list) or len(evidence)>3: raise ValueError('field_evidence must contain at most three fields')
    allowed={g['grant_id']:g for g in [current]+contexts}
    seen=set();result=[]
    for e in evidence:
        if not isinstance(e,dict) or e.get('field') not in ('period','unit','entity') or e['field'] in seen:
            raise ValueError('invalid or duplicate field evidence')
        seen.add(e['field']);g=allowed.get(e.get('grant_id')) if isinstance(e.get('grant_id'),str) else None
        if g is None: raise ValueError('field evidence outside read grants')
        for key,expected in dict(company_id=company,file_hash=current['file_hash'],mapped_fingerprint=current['mapped_fingerprint'],source_loc=g['source_loc']).items():
            if e.get(key)!=expected: raise ValueError('field evidence scope/fingerprint differs from grant')
        start,end=e.get('start_line'),e.get('end_line')
        if not range_is_granted(g['lines'],start,end): raise ValueError('field evidence outside granted lines')
        quote=e.get('quote');value=item.get(e['field'])
        text='\n'.join(g['lines'][str(i)] for i in range(start,end+1))
        quote_verified=isinstance(quote,str) and bool(quote.strip()) and compact(quote) in compact(text)
        value_verified=(isinstance(value,str) and bool(value.strip()) and quote_verified
            and compact(value) in compact(quote))
        if not (quote_verified and value_verified):
            raise ValueError(_field_literal_error(e.get('field'),value,quote if quote_verified else None))
        result.append(dict(field=e['field'],company_id=company,grant_id=g['grant_id'],file_hash=g['file_hash'],
            mapped_fingerprint=g['mapped_fingerprint'],original_name=g['original_name'],source_loc=g['source_loc'],
            ranges=[[start,end]],quote=quote))
    # Unknown is explicit absence, not permission to infer a qualifier. New
    # qualifiers outside the primary quote require an employee-declared witness.
    for field in ('period','unit'):
        value=item[field]
        if (field=='period' and value in UNKNOWN_PERIODS) or (field=='unit' and value==''): continue
        if compact(value) not in compact(item['quote']) and field not in seen:
            raise ValueError(f'{field} outside primary quote requires field_evidence')
    return result


def fields_in_grants(fact, span, grants, *, strict=False):
    evidence=span.get('field_evidence',[])
    if not isinstance(evidence,list): return False
    proved=set()
    for e in evidence:
        if not isinstance(e,dict) or e.get('field') not in ('period','unit','entity'): return False
        found=False
        for g in grants:
            if (e.get('company_id')!=fact['company_id'] or e.get('file_hash')!=g['file_hash']
                    or e.get('mapped_fingerprint')!=g['mapped_fingerprint'] or e.get('source_loc')!=g['source_loc']
                    or e.get('grant_id')!=g['grant_id'] or e.get('original_name')!=g['original_name']): continue
            ranges=e.get('ranges')
            if not isinstance(ranges,list) or not ranges: continue
            if not all(isinstance(r,list) and len(r)==2 and range_is_granted(g['lines'],r[0],r[1]) for r in ranges): continue
            text='\n'.join(g['lines'][str(i)] for a,b in ranges for i in range(a,b+1))
            quote=e.get('quote');value=fact[e['field']]
            if isinstance(quote,str) and quote.strip() and compact(quote) in compact(text) and isinstance(value,str) and compact(value) in compact(quote):
                found=True;break
        if not found:return False
        proved.add(e['field'])
    if strict:
        for field in ('period','unit'):
            value=fact[field] or ''
            if (field=='period' and value in UNKNOWN_PERIODS) or (field=='unit' and value==''):continue
            if compact(value) not in compact(span['quote']) and field not in proved:return False
    return True


def field_support_diagnostic(fact, span, supports, assigned, *, strict=False):
    """Explain only declared witnesses verified against the actual assigned grants."""
    missing=[]
    evidence=span.get('field_evidence',[])
    if not isinstance(evidence,list): return 'invalid field evidence metadata'
    for e in evidence[:3]:
        single={**span,'field_evidence':[e]}
        if fields_in_grants(fact,single,supports): continue
        field=e.get('field') if isinstance(e,dict) else None
        if field not in ('period','unit','entity'): continue
        if fields_in_grants(fact,single,assigned):
            missing.append(f"{field}: grant_id={e['grant_id']}, ranges={e['ranges'][:2]}")
        else:
            missing.append(f'{field}: no verified support in assigned grants')
    if strict:
        declared={e.get('field') for e in evidence if isinstance(e,dict)}
        for field in ('period','unit'):
            value=fact[field] or ''
            if (field=='period' and value in UNKNOWN_PERIODS) or (field=='unit' and value==''):continue
            if compact(value) not in compact(span['quote']) and field not in declared:
                missing.append(f'{field}: no declared authorized field witness')
    return ('missing same-block field support ['+'; '.join(missing)+']; add fresh evidence/ref in this new block and cite it in its text; old-block refs are not local evidence') if missing else 'field evidence failed verification; do not replace primary evidence or borrow old-block refs'


def block_support_contract():
    return dict(
        types=dict(grant_id='quoted string, copy exact supplied ID even if numeric-looking',
            fact_ids='JSON integer array, e.g. [1], never [\"1\"]; use actual integers from this request facts, never copy illustrative ID 1',
            start_line='JSON integer; use actual authorized line, not illustrative 1',end_line='JSON integer',base_version='JSON integer'),
        rule='Each new block citing a fact must include that fact field_evidence as its own local evidence and text refs. Old blocks remain verbatim but their refs cannot supply this new block. Do not remove applicable fact IDs to evade verification.',
        example=dict(block_id='<new distinct block ID>',
            text='<verbatim period/unit qualifier>, <source value>[^new-value][^new-scope]',
            evidence=[dict(ref='new-value',grant_id='<assigned primary grant>',file_hash='<assigned hash>',
                original_name='<declared original>',source_loc='<assigned primary coordinate>',
                start_line=1,end_line=1,fact_ids=[1]),
                dict(ref='new-scope',grant_id='<assigned field witness grant>',file_hash='<assigned hash>',
                original_name='<declared original>',source_loc='<assigned witness coordinate>',
                start_line=1,end_line=1,fact_ids=[])]),
        boundary='Only this fact field_evidence and assigned grants authorize support. Same original may have fresh distinct refs in different blocks; no new reading rights or inferred qualifiers.')
