"""Small, bounded lifecycle records for evidence, failures and appointments."""

import hashlib
import json
import math
import re
import time
import uuid


def record_evidence(state, source, text):
    """Record source-tagged observations without treating model reports as facts.

    Args:
        state: Per-target persistent state.
        source: Explicit producer category.
        text: Bounded observation or report.
    """
    state["evidence"] = (
        state.get("evidence", [])
        + [
            {
                "id": uuid.uuid4().hex[:12],
                "at": time.time(),
                "source": source,
                "text": str(text)[:2000],
            }
        ]
    )[-30:]


def note_failure(state, key, text):
    """Track repeated failure signatures, with a bounded cooldown rather than replay.

    Args:
        state: Per-target state.
        key: Stable tool-and-arguments or turn identifier.
        text: Real failure description.

    Returns:
        Updated failure record.
    """
    signature = hashlib.sha256(
        (key + "\0" + re.sub(r"\b\d{10,}\b", "<timestamp>", str(text)[:500])).encode()
    ).hexdigest()
    failures = state.setdefault("stalls", {})
    prior = failures.get(key, {})
    count = prior.get("count", 0) + 1 if prior.get("signature") == signature else 1
    record = {
        "signature": signature,
        "count": count,
        "reason": str(text)[:400],
        "at": time.time(),
        "until": time.time() + 900 if count >= 3 else 0,
    }
    failures[key] = record
    state["stalls"] = dict(list(failures.items())[-16:])
    return record


def tool_key(name, arguments):
    return (
        name
        + ":"
        + hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
    )


def appointment_updates(state, updates, user_text):
    """Validate evidence-backed user appointments and return an updated copy.

    Args:
        state: Existing target state.
        updates: At most three proposed changes.
        user_text: Actual latest user wording supporting the change.

    Returns:
        Bounded appointment list, without mutating the input state.

    Raises:
        ValueError: A supplied appointment field is invalid.
    """
    if not isinstance(updates, list) or len(updates) > 3:
        raise ValueError("Invalid appointment updates")
    items = [dict(item) for item in state.get("appointments", [])]
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("Invalid appointment")
        evidence = update.get("evidence", "")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or evidence not in user_text
        ):
            continue
        identifier = update.get("id", "")
        old = next((item for item in items if item["id"] == identifier), None)
        status = update.get("status", "pending")
        if status not in {"pending", "resolved", "cancelled"}:
            raise ValueError("Invalid appointment status")
        if old:
            result = update.get("result", "")
            if not isinstance(result, str):
                raise ValueError("Invalid appointment result")
            old.update(
                status=status,
                result=result[:500],
                evidence=evidence[:300],
                updated_at=time.time(),
            )
            continue
        if identifier or status != "pending":
            continue
        due, title = update.get("due_at"), update.get("title", "")
        if (
            isinstance(due, bool)
            or not isinstance(due, (int, float))
            or not math.isfinite(due)
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise ValueError("Invalid appointment deadline or title")
        if not time.time() - 3600 <= due <= time.time() + 90 * 86400:
            continue
        if any(
            item["title"] == title[:100] and abs(item["due_at"] - due) < 1800
            for item in items
        ):
            continue
        if len(items) >= 20:
            retired = next(
                (item for item in items if item["status"] != "pending"), None
            )
            if retired is None:
                continue
            items.remove(retired)
        items.append(
            {
                "id": uuid.uuid4().hex[:12],
                "title": title[:100],
                "due_at": due,
                "status": "pending",
                "result": "",
                "evidence": evidence[:300],
                "created_at": time.time(),
                "reminded_at": 0,
                "followed_up_at": 0,
            }
        )
    return items


def due_appointments(state, now=None):
    """Return reminder opportunities without consuming them before actual dispatch.

    Args:
        state: Target appointment state.
        now: Optional clock value for deterministic verification.

    Returns:
        Bounded reminder opportunities tied to exact appointment IDs.
    """
    now = time.time() if now is None else now
    due = []
    for item in state.get("appointments", []):
        if item["status"] != "pending":
            continue
        delta = item["due_at"] - now
        kind = (
            "pre"
            if 0 <= delta <= 3600 and not item.get("reminded_at")
            else "post"
            if -86400 <= delta <= -3600 and not item.get("followed_up_at")
            else ""
        )
        if kind:
            due.append(
                {
                    "id": item["id"],
                    "kind": kind,
                    "title": item["title"],
                    "due_at": item["due_at"],
                }
            )
    return due[:3]
