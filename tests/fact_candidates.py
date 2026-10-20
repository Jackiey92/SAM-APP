"""Scripted employee decisions and explicit ledger seeds, never production rules."""
import json

from app.memory.company_facts import ExtractedFact


def fact_candidate(file_hash, metric, value, *, company_id="acme", critical=False,
                   period="unspecified", entity=None, unit=None, source_span="fixture"):
    return ExtractedFact(
        company_id=company_id, metric=metric, value=str(value), period=period,
        value_type="string", unit=unit, source_file=file_hash, source_page=1,
        source_span=source_span, confidence=1.0, critical=critical, entity=entity,
    )


def manifest_candidates(manifest, *, company_id="acme", critical=False):
    """The test explicitly supplies label/value rows as employee decisions."""
    table = manifest["pages"][0]["tables"][0]
    rows = [table["headers"], *table["rows"]]
    return [fact_candidate(manifest["file_hash"], *row, company_id=company_id,
                           critical=critical, source_span=f"fixture-row-{index}")
            for index, row in enumerate(rows) if len(row) == 2]


class FixtureFinanceEmployee:
    def __init__(self, *decisions):
        self.decisions = decisions
        self.calls = []

    def run(self, *, text, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(text)
        source = "".join(payload["source_block"].split())
        return [dict(item, entity=item.get("entity") or payload["attribution"]["subject"])
                for item in self.decisions if "".join(item["quote"].split()) in source]


def decision(metric, value, quote, *, unit=None, period="2024", critical=False, entity=None):
    return dict(metric=metric, value=value, quote=quote, unit=unit, period=period,
                critical=critical, entity=entity, value_type="number")
