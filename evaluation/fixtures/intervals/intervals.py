def merge_intervals(intervals):
    """Merge overlapping or touching closed intervals."""
    if not intervals:
        return []

    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start < current[1]:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return [tuple(interval) for interval in merged]

