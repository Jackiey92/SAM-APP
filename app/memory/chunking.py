"""Structural Markdown chunking, without business classification or summaries."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Chunk:
    text: str
    source_span: tuple[int, int]


def _boundaries(text: str) -> list[int]:
    protected = [(m.start(), m.end()) for m in re.finditer(
        r"\[[^\]]*\]\([^)]*\)|https?://\S+", text)]
    tokens = re.finditer(r"\s+|[\u3400-\u9fff]|[^\s\u3400-\u9fff]+", text)
    return [m.end() for m in tokens
            if not any(a < m.end() < b for a, b in protected)]


def _bisect(text: str, start: int, limit: int) -> list[Chunk]:
    if len(text) <= limit:
        return [Chunk(text, (start, start + len(text)))]
    # Links, Latin runs and numerals are indivisible; CJK is estimated 1:1.
    boundaries = [n for n in _boundaries(text) if n < len(text)]
    if not boundaries:
        return [Chunk(text, (start, start + len(text)))]
    cut = min(boundaries, key=lambda n: abs(n - len(text) // 2))
    return _bisect(text[:cut], start, limit) + _bisect(text[cut:], start + cut, limit)


def split_markdown(text: str, *, fact: bool = False, limit: int = 700,
                   overlap: int = 100) -> list[Chunk]:
    """Keep table rows atomic and carry header context; span excludes copied header.

    Overlong indivisible links/numbers/table rows remain intact, even beyond budget.
    Adjacent plain-text chunks repeat complete trailing lines up to overlap.
    """
    if not isinstance(text, str) or limit < 200 or not 0 <= overlap < limit:
        raise ValueError("invalid chunking input/budget")
    if not text:
        return []
    if fact:
        return [Chunk(text, (0, len(text)))]
    lines = text.splitlines(keepends=True)
    groups: list[Chunk] = []
    pending, offset, begin = "", 0, 0
    header, copied_header = "", ""
    in_table = False

    def emit(end):
        if in_table:
            groups.append(Chunk(copied_header + pending, (begin, end)))
        else:
            pieces = _bisect(pending, begin, limit)
            safe = _boundaries(pending)
            for i, piece in enumerate(pieces):
                start, end = piece.source_span
                if i and overlap:
                    candidates = [begin + n for n in safe if start - overlap <= begin + n < start]
                    if candidates:
                        start = min(candidates)
                groups.append(Chunk(pending[start - begin:end - begin], (start, end)))

    for line in lines:
        table = line.lstrip().startswith("|")
        if pending and (line.startswith("#") or len(copied_header + pending) + len(line) > limit
                        or table != in_table):
            emit(offset)
            suffix = ""
            if not table and not in_table and not line.startswith("#"):
                for old in reversed(pending.splitlines(keepends=True)):
                    if len(suffix) + len(old) > overlap:
                        break
                    suffix = old + suffix
            copied_header = header if table and in_table else ""
            pending, begin = suffix, offset - len(suffix)
        if table and not in_table:
            header = line
        elif table and re.fullmatch(r"[|:\-\s]+", line):
            header += line
        if not pending:
            begin = offset
        pending += line
        offset += len(line)
        in_table = table
    if pending:
        emit(offset)
    return groups
