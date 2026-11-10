"""Durable per-task execution checkpoints."""

import copy
import time
import uuid

# 一个待办断点被自动续跑的次数上限。断点没有时间过期机制，只要 pending 就会被
# 下一轮选中；同一件事反复"再证明一次"能挂几个小时（2026-11-10 发歌那次就是）。
MAX_CHECKPOINT_RESUMES = 4


def save_checkpoint(event):
    task = getattr(event, "checkpoint_task", "")
    if task:
        previous = event.state.get("execution_checkpoints", {}).get(
            getattr(event, "checkpoint_id", ""), {}
        )
        event.state["execution_checkpoint"] = {
            "task": task,
            "task_id": getattr(event, "checkpoint_id", ""),
            "cid": event.cid,
            "updated_at": time.time(),
            "contract": copy.deepcopy(getattr(event, "execution_contract", {})),
            "receipts": copy.deepcopy(getattr(event, "action_receipts", [])[-40:]),
            "media_deliveries": getattr(event, "media_deliveries", 0),
            "pending": getattr(event, "checkpoint_pending", True),
            "project_id": getattr(event, "project_id", ""),
            "next_attempt_at": previous.get("next_attempt_at", 0),
            "resume_count": previous.get("resume_count", 0),
        }
        checkpoints = event.state.setdefault("execution_checkpoints", {})
        identifier = getattr(event, "checkpoint_id", "")
        if identifier:
            checkpoints[identifier] = copy.deepcopy(event.state["execution_checkpoint"])
            event.state["execution_checkpoints"] = dict(list(checkpoints.items())[-20:])


def resume_checkpoint(event, task):
    saved = event.state.get("execution_checkpoint", {})
    candidates = [
        item
        for item in event.state.get("execution_checkpoints", {}).values()
        if item.get("pending")
        and item.get("task") == task
        and item.get("cid") == event.cid
    ]
    if candidates:
        saved = max(candidates, key=lambda item: item.get("updated_at", 0))
    event.checkpoint_task = task
    if (
        not saved.get("pending", True)
        or saved.get("task") != task
        or saved.get("cid") != event.cid
    ):
        event.checkpoint_id = uuid.uuid4().hex
        return False
    event.checkpoint_id = saved.get("task_id") or uuid.uuid4().hex
    event.action_receipts = copy.deepcopy(saved.get("receipts", []))
    event.media_deliveries = saved.get("media_deliveries", 0)
    event.execution_contract = copy.deepcopy(saved.get("contract", {}))
    return bool(event.execution_contract)


def next_checkpoint(state, cid, now=None):
    """Pick the least recently updated eligible task, not the last task forever.

    Args:
        state: Per-target task index.
        cid: Current conversation identity.
        now: Optional clock.

    Returns:
        An eligible pending checkpoint or None.
    """
    now = time.time() if now is None else now
    index = dict(state.get("execution_checkpoints", {}))
    legacy = state.get("execution_checkpoint", {})
    if legacy.get("task") and not index:
        index[legacy.get("task_id", "legacy")] = legacy
    candidates = [
        item
        for item in index.values()
        if item.get("pending")
        and item.get("cid") == cid
        and int(item.get("resume_count", 0)) < MAX_CHECKPOINT_RESUMES
        and item.get("next_attempt_at", 0) <= now
        and (
            not item.get("project_id")
            or any(
                project.get("id") == item["project_id"]
                and project.get("status") == "active"
                and project.get("retry_after", 0) <= now
                for project in state.get("projects", [])
            )
        )
    ]
    return (
        min(candidates, key=lambda item: item.get("updated_at", 0))
        if candidates
        else None
    )


def count_checkpoint_resume(state, task: str) -> int:
    """Count one automatic resume against a pending checkpoint.

    Args:
        state: Per-target task index.
        task: Task text identifying the resumed checkpoint.

    Returns:
        The highest resume count recorded for that task.
    """
    count = 0
    legacy = state.get("execution_checkpoint", {})
    if isinstance(legacy, dict) and legacy.get("task") == task:
        legacy["resume_count"] = int(legacy.get("resume_count", 0)) + 1
        count = legacy["resume_count"]
    for item in (state.get("execution_checkpoints") or {}).values():
        if item.get("task") == task:
            item["resume_count"] = int(item.get("resume_count", 0)) + 1
            count = max(count, item["resume_count"])
    return count


def checkpoint_context(event):
    return {
        "task_id": getattr(event, "checkpoint_id", ""),
        "task": getattr(event, "checkpoint_task", ""),
        "contract": getattr(event, "execution_contract", {}),
        "receipts": [
            {
                "tool": r["tool"],
                "status": r["status"],
                "call_key": r.get("call_key", "legacy_unknown"),
                "result": r.get("result", "")[:4000],
            }
            for r in getattr(event, "action_receipts", [])[-20:]
        ],
        "artifacts": [
            item
            for item in event.state.get("task_artifacts", [])
            if item.get("task_id") == getattr(event, "checkpoint_id", None)
        ],
    }
