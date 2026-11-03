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


def exhausted(owner, state, counter, setting, default):
    if active_budget_override(owner, state):
        return False
    limit = owner.number(setting, default, 0, 100000)
    return bool(limit and usage_for(state).get(counter, 0) >= limit)


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
