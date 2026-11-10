"""Per-target daily accounting and bounded deferred outputs."""

import time
import uuid
from datetime import datetime, timedelta


def usage_for(state):
    """Return today's local-time counters, rolling over on the next access.

    Args:
        state: Persistent per-target state.

    Returns:
        Today's mutable counters.
    """
    day = datetime.now().astimezone().date().isoformat()
    if state.get("usage", {}).get("day") != day:
        state["usage"] = {
            "day": day,
            "checks": 0,
            "rounds": 0,
            "tools": 0,
            "messages": 0,
            "failures": 0,
            "previews": 0,
            "chat_plans": 0,
        }
    return state["usage"]


def next_day():
    return (
        (datetime.now().astimezone() + timedelta(days=1))
        .replace(hour=0, minute=0, second=5, microsecond=0)
        .timestamp()
    )


def active_budget_override(owner, state):
    """Allow an admitted active window to finish without budget interruptions.

    Args:
        owner: Plugin runtime and configuration.
        state: Target state with an admitted action window.

    Returns:
        Whether this window currently bypasses daily and tool-count budgets.
    """
    return bool(
        owner.config.get("ignore_budget_while_active", True)
        and state.get("window_admitted", False)
        and state.get("rounds", 0) > 0
        and state.get("window_end", 0) > time.time()
    )


def night_bounds(owner):
    """Return the configured night window as local whole hours.

    Args:
        owner: Plugin runtime and configuration.

    Returns:
        Tuple of start and end hour; the window is left-inclusive, right-exclusive
        and may wrap past midnight.
    """
    start = owner.number("night_window_start_hour", 23, 0, 23)
    end = owner.number("night_window_end_hour", 8, 0, 23)
    return start, end


def night_pool_key(owner, now=None):
    """Return the identifier of the night pool currently in effect.

    A night that wraps past midnight stays one pool: hours after the start hour
    use today's date, hours before the end hour use yesterday's date, so the
    counter does not reset at 00:00 in the middle of a night.

    Args:
        owner: Plugin runtime and configuration.
        now: Optional aware local datetime for deterministic verification.

    Returns:
        Night pool identifier, or an empty string during the day.
    """
    if not owner.config.get("split_night_budget", True):
        return ""
    now = now or datetime.now().astimezone()
    start, end = night_bounds(owner)
    if start == end:
        return ""
    if start > end:
        if now.hour >= start:
            return now.date().isoformat()
        if now.hour < end:
            return (now.date() - timedelta(days=1)).isoformat()
        return ""
    if start <= now.hour < end:
        return now.date().isoformat()
    return ""


def night_end_timestamp(owner, now=None):
    """Return when the current or next night window ends, as an epoch value."""
    now = now or datetime.now().astimezone()
    _, end = night_bounds(owner)
    candidate = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def pool_usage(owner, state):
    """Return the counters of the pool that currently applies.

    Args:
        owner: Plugin runtime and configuration.
        state: Persistent per-target state.

    Returns:
        Today's counters during the day, or the current night pool at night.
    """
    key = night_pool_key(owner)
    if not key:
        return usage_for(state)
    pools = state.setdefault("night_usage", {})
    if pools.get("pool") != key:
        state["night_usage"] = pools = {
            "pool": key,
            "checks": 0,
            "rounds": 0,
            "tools": 0,
            "messages": 0,
            "failures": 0,
            "previews": 0,
        }
    return pools


def count(owner, state, counter, amount=1):
    """Count an occurrence in both today's totals and the active pool.

    Args:
        owner: Plugin runtime and configuration.
        state: Persistent per-target state.
        counter: Counter name such as tools, rounds, checks or messages.
        amount: Increment size.

    Returns:
        The new today total for that counter.
    """
    usage = usage_for(state)
    usage[counter] = usage.get(counter, 0) + amount
    pool = pool_usage(owner, state)
    if pool is not usage:
        pool[counter] = pool.get(counter, 0) + amount
    return usage[counter]


def pool_limit(owner, setting, default):
    """Return the effective limit for the active pool.

    Args:
        owner: Plugin runtime and configuration.
        setting: Daily limit configuration key.
        default: Fallback when the key is missing.

    Returns:
        The nightly share of the daily limit at night, otherwise the daily limit.
    """
    limit = owner.number(setting, default, 0, 100000)
    if not limit or not night_pool_key(owner):
        return limit
    percent = owner.number("night_budget_percent", 40, 0, 200)
    return max(1, limit * percent // 100)


def pool_snapshot(owner, state):
    """Return both pool usages and limits for pages and the morning report."""
    pool = pool_usage(owner, state)
    return {
        "pool": "night" if night_pool_key(owner) else "day",
        "checks": pool.get("checks", 0),
        "rounds": pool.get("rounds", 0),
        "tools": pool.get("tools", 0),
        "messages": pool.get("messages", 0),
        "limit_checks": pool_limit(owner, "daily_check_limit", 144),
        "limit_rounds": pool_limit(owner, "daily_round_limit", 96),
        "limit_tools": pool_limit(owner, "daily_tool_limit", 300),
        "limit_messages": pool_limit(owner, "daily_message_limit", 200),
        "today": dict(usage_for(state)),
    }


def exhausted(owner, state, counter, setting, default):
    if active_budget_override(owner, state):
        return False
    limit = pool_limit(owner, setting, default)
    return bool(limit and pool_usage(owner, state).get(counter, 0) >= limit)


def retain_outputs(state, outputs):
    """Validate deferred text outputs before adding them to a bounded outbox.

    Args:
        state: Per-target state.
        outputs: Model-authored completed outputs with optional reference strings.

    Raises:
        ValueError: An output is malformed or exceeds the batch size.
    """
    if not isinstance(outputs, list) or len(outputs) > 3:
        raise ValueError("Invalid deferred outputs")
    validated = []
    for output in outputs:
        if not isinstance(output, dict) or any(
            not isinstance(output.get(key, ""), str)
            for key in ("title", "content", "artifact", "reason")
        ):
            raise ValueError("Invalid deferred output")
        content = output.get("content", "").strip()[:8000]
        if not content:
            raise ValueError("Deferred output requires shareable text")
        validated.append(
            {
                "id": uuid.uuid4().hex[:12],
                "title": output.get("title", "成果")[:120],
                "content": content,
                "artifact": output.get("artifact", "")[:1000],
                "reason": output.get("reason", "等待合适时机分享")[:500],
                "status": "pending",
                "created_at": time.time(),
            }
        )
    box = list(state.get("outbox", []))
    for output in validated:
        if any(
            item["content"] == output["content"]
            and item["status"] in {"pending", "sending", "sent", "uncertain"}
            for item in box
        ):
            continue
        if len(box) >= 20:
            retired = next(
                (item for item in box if item["status"] in {"sent", "discarded"}), None
            )
            if retired is None:
                continue
            box.remove(retired)
        box.append(output)
    state["outbox"] = box
