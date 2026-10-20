"""Deterministic inline-source checks. No IO, conversions or entity inference."""
from __future__ import annotations

import re
import unicodedata

MARK = re.compile(r"\{([fl]):([^{}]+)\}")
DIGITS = r"[0-9０-９]+(?:[ \t]+[0-9０-９]+)*"
NUMBER = re.compile(rf"[+＋\-－]?[ \t]*(?:{DIGITS}(?:[ \t]*[,，][ \t]*[0-9０-９]{{3}})*(?:[ \t]*[.．][ \t]*{DIGITS})?|[.．][ \t]*{DIGITS})")
PERIOD = re.compile(r"[12１２][0-9０-９]{3}(?:[QＱ][1-4１-４]|年?第?[一二三四1-4１-４]季度|[-/－／][0-9０-９]{1,2}(?:[-/－／][0-9０-９]{1,2})?)")
TOKEN = re.compile(rf"(?P<period>{PERIOD.pattern})|(?P<number>{NUMBER.pattern})")
UNIT = re.compile(r"\s*(?:[%％]|[万萬亿億兆千百]?(?:元|美元|人民币|人民幣|股|人|家|个|個|倍|吨|噸|件)|[万萬亿億兆]|年(?:度)?|季度|月|日)?")


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).translate(str.maketrans("萬億幣個噸圓錢輛隻張戶單筆臺", "万亿币个吨圆钱辆只张户单笔台"))


def number(text):
    raw = normalize(text).replace(',', '')
    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)', raw):
        return None
    negative = raw.startswith('-')
    integer, _, fraction = raw.lstrip('+-').partition('.')
    integer = integer.lstrip('0') or '0'
    fraction = fraction.rstrip('0')
    value = integer + ('.' + fraction if fraction else '')
    return ('-' if negative and value != '0' else '') + value


def strip_marks(text):
    return MARK.sub('', text)


def tokens(text):
    """Keep original offsets; marks and Markdown link destinations aren't prose."""
    masked = list(text)
    for pattern in (MARK, re.compile(r"\]\([^\n)]*\)")):
        for m in pattern.finditer(text):
            start = m.start() + (2 if pattern is not MARK else 0)
            for i in range(start, m.end()):
                masked[i] = '\0'
    clean = ''.join(masked)
    for m in TOKEN.finditer(clean):
        unit = UNIT.match(text, m.end())
        end = unit.end()
        # Any explicitly delimited literal unit is supported, not a business
        # unit dictionary. Ambiguous prose between number and mark mismatches.
        literal = re.match(r"[^0-9０-９{}；;。\n|,，:：()\[\]]*?(?=\{[fl]:)", text[m.end():])
        if literal:
            end = m.end() + literal.end()
        mark = MARK.match(text, end)
        period_token = normalize(m.group()) if m.group('period') else None
        unit_text = normalize(text[m.end():end])
        normalized_number = period_token or number(m.group())
        if not period_token and (unit_text in {'年', '年度', '季度', '月', '日'}
                or (not unit_text and re.fullmatch(r'[12][0-9]{3}', normalized_number or ''))):
            period_token = normalize(text[m.start():end]).removesuffix('年度').removesuffix('年')
        yield {"number": normalized_number, "period_token": period_token, "unit": normalize(text[m.end():end]),
               "start": m.start(), "end": end, "text": text[m.start():end],
               "kind": mark.group(1) if mark else None,
               "ids": mark.group(2).split(',') if mark else []}


def period_matches(declaration, period):
    return re.search(r"(?<![0-9])" + re.escape(declaration) + r"(?![0-9])", period) is not None


def validate(conclusion, sources, *, kind):
    """Sources are scoped L2 cards (f) or verified L1 token lists (l).

    All declared sources must match; no union fallback. Period declarations
    are scoped to their punctuation-delimited clause, never inferred by meaning.
    """
    verified, violations = [], []
    found = list(tokens(conclusion))
    attached = {t['end'] for t in found if t['ids']}
    for mark in MARK.finditer(conclusion):
        if mark.start() not in attached:
            violations.append({'start': mark.start(), 'end': mark.end(), 'text': mark.group(),
                               'ids': mark.group(2).split(','), 'reason': 'invalid-source'})
    for token in found:
        code = None
        ids = token['ids']
        if not ids:
            code = 'missing-source'
        elif token['kind'] != kind or any(ref not in sources for ref in ids):
            code = 'invalid-source'
        else:
            for ref in ids:
                if kind == 'l':
                    candidates = sources[ref]
                    if not any(t['number'] == token['number'] for t in candidates):
                        code = 'number-not-in-source'
                    elif not any(t['number'] == token['number'] and t['unit'] == token['unit'] for t in candidates):
                        code = 'unit-mismatch'
                else:
                    card = sources[ref]
                    period = normalize(card.get('period'))
                    if token['period_token']:
                        if not period_matches(token['period_token'], period):
                            code = 'period-mismatch'
                    elif token['number'] != number(card.get('value')):
                        code = 'number-not-in-source'
                    elif token['unit'] != normalize(card.get('unit')):
                        code = 'unit-mismatch'
                if code:
                    break
        if not code:
            # A declared period in the same clause must agree with every explicitly
            # bound source, including when the year itself binds another card.
            left = max((conclusion.rfind(p, 0, token['start']) for p in '；;。\n'), default=-1) + 1
            right = min([pos for p in '；;。\n' if (pos := conclusion.find(p, token['end'])) >= 0] or [len(conclusion)])
            declarations = [t['period_token'] for t in found
                            if left <= t['start'] < right and t['period_token']]
            for ref in ids:
                periods = [normalize(sources[ref].get('period'))] if kind == 'f' else [t.get('period', '') for t in sources[ref] if t['number'] == token['number'] and t['unit'] == token['unit']]
                if any(not any(period_matches(declaration, p) for p in periods) for declaration in declarations):
                    code = 'period-mismatch'
        if code:
            violations.append({**token, 'reason': code})
        else:
            period = normalize(sources[ids[0]].get('period')) if kind == 'f' else next(t.get('period', '') for t in sources[ids[0]] if t['number'] == token['number'] and t['unit'] == token['unit'])
            verified.append({**token, 'period': period})
    return verified, violations


def rejection(layer, violations):
    return {'status': 'rejected', 'layer': layer, 'unresolved': [
        {'status': 'unresolved', 'source': v.get('source', {'file_hash': '', 'span': f"{v['start']}:{v['end']}"}),
         'candidate': v, 'reason': v['reason'], 'blocking': 'numeric source review required'}
        for v in violations]}
