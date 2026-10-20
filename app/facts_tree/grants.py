"""Structural line-set authority; no content decisions or range repair."""


def range_is_granted(lines, start, end):
    return (type(start) is int and type(end) is int and start <= end
            and end-start+1 <= len(lines)
            and all(str(i) in lines for i in range(start,end+1)))
