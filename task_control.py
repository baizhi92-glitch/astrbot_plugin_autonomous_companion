"""Conversation-scoped task handles for model-driven cancellation."""

import hashlib
import time

from .cancellations import remember_cancellation


def pending_tasks(state):
    entries = {}

    def add(text, kind, key):
        if not isinstance(text, str) or not text.strip():
            return
        identifier = hashlib.sha256(
            (kind + "\0" + key + "\0" + text).encode()
        ).hexdigest()[:24]
        entries[identifier] = {"id": identifier, "kind": kind, "key": key, "task": text}

    for key in ("manual_plan", "future_task", "next_task"):
        add(state.get(key), "plan", key)
    for key, item in state.get("execution_checkpoints", {}).items():
        if item.get("pending"):
            add(item.get("task"), "checkpoint", key)
    legacy = state.get("execution_checkpoint", {})
    if legacy.get("pending"):
        add(legacy.get("task"), "legacy", legacy.get("task_id", ""))
    for item in state.get("projects", []):
        if item.get("status") == "active":
            add(
                item.get("title", "") + "：" + item.get("goal", ""),
                "project",
                item["id"],
            )
    for item in state.get("long_tasks", []):
        if item.get("status") not in {"completed", "cancelled"}:
            add(item["goal"], "long_task", item["id"])
    return list(entries.values())


def cancel_tasks(state, identifiers):
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or len(identifiers) > 30
        or any(not isinstance(i, str) for i in identifiers)
    ):
        raise ValueError("请选择1～30个实际任务ID")
    pending = {item["id"]: item for item in pending_tasks(state)}
    if any(identifier not in pending for identifier in identifiers):
        raise ValueError("任务已变化或不存在，请先重新查询")
    selected = [pending[i] for i in identifiers]
    for item in selected:
        if item["kind"] == "long_task":
            from .long_tasks import control_task

            control_task(state, item["key"], "cancel")
    texts = {item["task"] for item in selected}
    projects = {item["key"] for item in selected if item["kind"] == "project"}
    remember_cancellation(state, list(texts))
    for key in ("manual_plan", "future_task", "next_task"):
        if state.get(key) in texts or (
            projects and state.get("manual_project_id") in projects
        ):
            state[key] = ""
    for checkpoint in [
        state.get("execution_checkpoint", {}),
        *state.get("execution_checkpoints", {}).values(),
    ]:
        if checkpoint.get("task") in texts or checkpoint.get("project_id") in projects:
            checkpoint["pending"] = False
            remember_cancellation(state, [checkpoint.get("task", "")])
    for project in state.get("projects", []):
        if project["id"] in projects:
            project.update(status="cancelled", updated_at=time.time())
    if not state.get("manual_plan"):
        state.update(manual_project_id="", manual_plan_expires_at=0)
    if not state.get("next_task"):
        state.update(window_end=0, window_admitted=False)
    state["plan_status"] = "已按对话中的取消要求停止所选任务"
    return selected
