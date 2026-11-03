"""Persistent user cancellation boundaries for autonomous planning."""

import re
import time


def related(left, right):
    a = re.sub(r"[\W_]+", "", str(left)).lower()
    b = re.sub(r"[\W_]+", "", str(right)).lower()
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    x = {a[i : i + 2] for i in range(len(a) - 1)}
    y = {b[i : i + 2] for i in range(len(b) - 1)}
    return bool(x and y and 2 * len(x & y) / (len(x) + len(y)) >= 0.9)


def cancelled_match(state, text):
    return next(
        (
            item
            for item in state.get("cancelled_tasks", [])
            if related(item["text"], text)
        ),
        None,
    )


def remember_cancellation(state, texts):
    entries = list(state.get("cancelled_tasks", []))
    for text in texts:
        if (
            isinstance(text, str)
            and text.strip()
            and not any(related(item["text"], text) for item in entries)
        ):
            entries.append({"text": text[:2000], "at": time.time()})
    state["cancelled_tasks"] = entries[-30:]


def clear_cancelled_work(state):
    """Clear only pending state matching an explicitly cancelled task."""
    for key in ("future_task", "next_task", "manual_plan"):
        if cancelled_match(state, state.get(key, "")):
            state[key] = ""
    checkpoint = state.get("execution_checkpoint", {})
    if cancelled_match(state, checkpoint.get("task", "")):
        state.pop("execution_checkpoint", None)
    state["execution_checkpoints"] = {
        key: value
        for key, value in state.get("execution_checkpoints", {}).items()
        if not cancelled_match(state, value.get("task", ""))
    }
    state["projects"] = [
        p
        for p in state.get("projects", [])
        if not cancelled_match(state, p.get("title", ""))
    ]


def cancel_from_user_text(state, text):
    """Resolve explicit stop-testing language against existing testing tasks only."""
    if not isinstance(text, str) or not re.search(
        r"(?:别|不要|不用|不必|停止|取消|不再|无需).{0,12}(?:复测|重测|测试|测了|再测|测速|检测|ping)|(?:复测|重测|测试|测速|检测).{0,6}(?:停了|停掉|取消|不用了)",
        text,
        re.I,
    ):
        return []
    records = [
        state.get("execution_checkpoint", {}),
        *state.get("execution_checkpoints", {}).values(),
    ]
    candidates = [
        state.get(key, "") for key in ("manual_plan", "future_task", "next_task")
    ]
    candidates += [record.get("task", "") for record in records]
    candidates += [
        p.get("title", "") + " " + p.get("goal", "")
        for p in state.get("projects", [])
        if p.get("status") == "active"
    ]
    tasks = list(
        dict.fromkeys(
            task
            for task in candidates
            if re.search(r"复测|重测|测试|测速|延迟|抖动|ping", task, re.I)
        )
    )
    # Explicit addresses narrow the cancellation; do not cancel an unrelated test.
    addresses = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    if addresses:
        tasks = [
            task for task in tasks if any(address in task for address in addresses)
        ]
    if not tasks:
        return []
    remember_cancellation(state, tasks)
    clear_cancelled_work(state)
    for project in state.get("projects", []):
        if any(
            related(task, project.get("title", "") + " " + project.get("goal", ""))
            for task in tasks
        ):
            project["status"] = "cancelled"
    state.update(
        window_end=0,
        next_task="",
        plan_status="已按用户要求取消测试任务",
        continuation_status="用户明确要求停止测试，旧断点不再恢复",
    )
    if not state.get("manual_plan"):
        state.update(manual_project_id="", manual_plan_expires_at=0)
    return tasks
