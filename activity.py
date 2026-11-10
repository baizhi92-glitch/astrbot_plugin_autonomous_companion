"""用户活跃时间：按本地整点统计她的发言分布，随时间衰减。

只记时间，不记内容。统计按工作日与周末分开，样本不足时不下结论。
"""

import math
import time

HOURS = 24
DEFAULT_HALF_LIFE_DAYS = 30.0
# 至少这么多次发言才下结论，样本不足只承认“看不出来”
MIN_SAMPLES = 6
# 一小时占比超过平均值的这么多倍才算“常在线”
ACTIVE_RATIO = 1.6
# 低于平均值的这个比例算“几乎不在”
QUIET_RATIO = 0.25
# 工作日与周末的重心差超过这么多小时才值得一提
SHIFT_HOURS = 1.0
# 两边都要有这么集中才谈得上“早晚”
FOCUS_MIN = 0.6


def blank() -> dict:
    """Build an empty activity record.

    Returns:
        A record with zeroed weekday and weekend histograms.
    """
    return {
        "weekday": [0.0] * HOURS,
        "weekend": [0.0] * HOURS,
        "samples": 0,
        "updated_at": 0.0,
    }


def _bins(raw) -> list[float]:
    """Normalize a stored histogram to 24 non-negative floats.

    Args:
        raw: Stored value, possibly missing or malformed.

    Returns:
        A list of 24 floats.
    """
    bins = [0.0] * HOURS
    if isinstance(raw, list):
        for index, value in enumerate(raw[:HOURS]):
            try:
                bins[index] = max(float(value), 0.0)
            except (TypeError, ValueError):
                bins[index] = 0.0
    return bins


def decay_factor(elapsed_days: float, half_life_days: float) -> float:
    """Weight of older samples after ``elapsed_days``.

    Args:
        elapsed_days: Days since the samples were recorded.
        half_life_days: Days after which a sample counts half as much.

    Returns:
        A factor in (0, 1].
    """
    if elapsed_days <= 0:
        return 1.0
    half_life = max(float(half_life_days or DEFAULT_HALF_LIFE_DAYS), 0.5)
    return 0.5 ** (elapsed_days / half_life)


def record(state: dict, at: float | None = None, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> dict:
    """Add one inbound message to the histogram, decaying older samples first.

    Args:
        state: Per-conversation state, updated in place.
        at: Timestamp of the message, defaults to now.
        half_life_days: Days after which a sample counts half as much.

    Returns:
        The updated activity record.
    """
    stamp_at = time.time() if at is None else float(at)
    entry = state.get("activity")
    if not isinstance(entry, dict):
        entry = blank()
    weekday = _bins(entry.get("weekday"))
    weekend = _bins(entry.get("weekend"))
    updated_at = entry.get("updated_at") or 0.0
    if updated_at:
        factor = decay_factor((stamp_at - updated_at) / 86400.0, half_life_days)
        if factor < 1.0:
            weekday = [value * factor for value in weekday]
            weekend = [value * factor for value in weekend]
    local = time.localtime(stamp_at)
    target = weekday if local.tm_wday < 5 else weekend
    target[local.tm_hour] += 1.0
    samples = int(entry.get("samples") or 0) + 1
    state["activity"] = {
        "weekday": weekday,
        "weekend": weekend,
        "samples": min(samples, 999999),
        "updated_at": stamp_at,
    }
    return state["activity"]


def _decayed_hours(entry: dict, half_life_days: float, now: float) -> tuple[list[float], list[float]]:
    """Return both histograms decayed to the current moment without mutating storage.

    Args:
        entry: Stored activity record.
        half_life_days: Days after which a sample counts half as much.
        now: Reference timestamp.

    Returns:
        Weekday and weekend histograms decayed to ``now``.
    """
    weekday = _bins(entry.get("weekday"))
    weekend = _bins(entry.get("weekend"))
    updated_at = entry.get("updated_at") or 0.0
    if updated_at and now > updated_at:
        factor = decay_factor((now - updated_at) / 86400.0, half_life_days)
        weekday = [value * factor for value in weekday]
        weekend = [value * factor for value in weekend]
    return weekday, weekend


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Merge adjacent hour flags into ranges, joining a run that crosses midnight.

    Args:
        flags: 24 booleans.

    Returns:
        List of inclusive ``(start, end)`` hour pairs.
    """
    hours = [index for index, flag in enumerate(flags) if flag]
    if not hours:
        return []
    if len(hours) == HOURS:
        return [(0, HOURS - 1)]
    runs: list[tuple[int, int]] = []
    start = previous = hours[0]
    for hour in hours[1:]:
        if hour == previous + 1:
            previous = hour
            continue
        runs.append((start, previous))
        start = previous = hour
    runs.append((start, previous))
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == HOURS - 1:
        first = runs.pop(0)
        last = runs.pop()
        runs.append((last[0], first[1]))
    return runs


def _label(start: int, end: int) -> str:
    """Render an hour range as ``08:00–09:00`` style text.

    Args:
        start: First hour of the range.
        end: Last hour of the range, inclusive.

    Returns:
        A readable range label.
    """
    end_hour = (end + 1) % HOURS
    return f"{start:02d}:00–{end_hour:02d}:00"


def _mean_hour(bins: list[float]) -> tuple[float, float] | None:
    """Circular mean hour and how concentrated the histogram is.

    Args:
        bins: 24 weighted hours.

    Returns:
        ``(mean hour, concentration)`` or ``None`` when there is no weight. The
        concentration is the resultant length in ``0..1``; a spread-out or
        two-peaked day scores low, which is how a fake weekend shift is avoided.
    """
    total = sum(bins)
    if total <= 0:
        return None
    sin_sum = sum(value * math.sin(hour / HOURS * 2 * math.pi) for hour, value in enumerate(bins))
    cos_sum = sum(value * math.cos(hour / HOURS * 2 * math.pi) for hour, value in enumerate(bins))
    angle = math.atan2(sin_sum, cos_sum) % (2 * math.pi)
    concentration = math.hypot(sin_sum, cos_sum) / total
    return angle / (2 * math.pi) * HOURS, concentration


def _run_length(start: int, end: int) -> int:
    """How many hours a merged range covers.

    Args:
        start: First hour.
        end: Last hour, inclusive.

    Returns:
        Hour count.
    """
    return (end - start) % HOURS + 1


def _run_weight(run: tuple[int, int], weights: list[float]) -> float:
    """Total weight of a merged range.

    Args:
        run: Inclusive ``(start, end)`` hour pair.
        weights: 24 per-hour weights.

    Returns:
        The summed weight of the range.
    """
    start, end = run
    span = weights[start : end + 1] if end >= start else weights[start:] + weights[: end + 1]
    return sum(span)


def _rank(runs: list[tuple[int, int]], weights: list[float], limit: int, by_length: bool = False) -> list[tuple[int, int]]:
    """Keep the most telling ranges, then put them back in clock order.

    Args:
        runs: Merged ranges.
        weights: 24 per-hour weights.
        limit: How many ranges to keep.
        by_length: Rank by duration instead of weight.

    Returns:
        At most ``limit`` ranges ordered by start hour.
    """
    def score(run):
        return _run_length(*run) if by_length else _run_weight(run, weights)

    picked = sorted(runs, key=score, reverse=True)[:limit]
    return sorted(picked, key=lambda run: run[0])


def summary(state: dict, half_life_days: float = DEFAULT_HALF_LIFE_DAYS, now: float | None = None) -> dict:
    """Summarize when the user is usually around.

    Args:
        state: Per-conversation state.
        half_life_days: Days after which a sample counts half as much.
        now: Reference timestamp, defaults to now.

    Returns:
        A summary with per-hour weights, active and quiet ranges, and a sample count.
    """
    entry = state.get("activity")
    if not isinstance(entry, dict):
        entry = blank()
    current = time.time() if now is None else float(now)
    weekday, weekend = _decayed_hours(entry, half_life_days, current)
    hours = [weekday[hour] + weekend[hour] for hour in range(HOURS)]
    samples = int(entry.get("samples") or 0)
    total = sum(hours)
    result = {
        "samples": samples,
        "enough": samples >= MIN_SAMPLES and total > 0,
        "hours": hours,
        "total": total,
        "active": [],
        "quiet": [],
        "peak": "",
        "weekend_shift": "",
        "weekday_total": sum(weekday),
        "weekend_total": sum(weekend),
        "updated_at": entry.get("updated_at") or 0.0,
    }
    if not result["enough"]:
        return result
    average = total / HOURS
    active_flags = [value >= average * ACTIVE_RATIO for value in hours]
    quiet_flags = [value <= average * QUIET_RATIO for value in hours]
    # 只在时段里挑最值得说的几段，再按时间顺序摆回去
    result["active"] = [
        _label(start, end) for start, end in _rank(_runs(active_flags), hours, 4)
    ]
    result["quiet"] = [
        _label(start, end)
        for start, end in _rank(_runs(quiet_flags), hours, 2, by_length=True)
    ]
    peak_hour = max(range(HOURS), key=lambda hour: hours[hour])
    result["peak"] = f"{peak_hour:02d}:00"
    weekday_stat = _mean_hour(weekday)
    weekend_stat = _mean_hour(weekend)
    if weekday_stat and weekend_stat:
        weekday_mean, weekday_focus = weekday_stat
        weekend_mean, weekend_focus = weekend_stat
        shift = (weekend_mean - weekday_mean + HOURS / 2) % HOURS - HOURS / 2
        # 双峰或过于分散时，圆均值会算出没意义的偏移，宁可不提
        if weekday_focus >= FOCUS_MIN and weekend_focus >= FOCUS_MIN and abs(shift) >= SHIFT_HOURS:
            direction = "晚" if shift > 0 else "早"
            result["weekend_shift"] = f"周末比工作日{direction}约 {abs(round(shift * 2) / 2):.1f} 小时"
    return result


def clear(state: dict) -> None:
    """Drop the histogram but keep a manual note and the sample counter reset.

    Args:
        state: Per-conversation state, updated in place.
    """
    entry = state.get("activity")
    note = entry.get("note") if isinstance(entry, dict) else ""
    fresh = blank()
    if note:
        fresh["note"] = note
    state["activity"] = fresh
