import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import long_tasks


def test_lifecycle_and_restart():
    state = {}
    job = long_tasks.create_task(state, "Report", "Write a report")
    assert long_tasks.due_task(state) is job
    long_tasks.control_task(state, job["id"], "pause")
    assert long_tasks.due_task(state) is None
    long_tasks.control_task(state, job["id"], "resume")
    job["status"] = "running"
    long_tasks.recover_tasks(state)
    assert job["status"] == "waiting"
    long_tasks.control_task(state, job["id"], "cancel")
    long_tasks.recover_tasks(state)
    assert job["status"] == "cancelled"
    with pytest.raises(ValueError):
        long_tasks.control_task(state, job["id"], "resume")


@pytest.mark.asyncio
async def test_isolated_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(long_tasks.StarTools, "get_data_dir", lambda _: tmp_path)
    state = {}
    hooks = object()
    event = SimpleNamespace(state=state, action_receipts=[], message_str="自主心跳")
    owner = SimpleNamespace(
        states={"umo": state},
        save=AsyncMock(),
        check_current=AsyncMock(),
        notification_hooks=hooks,
        prepare=AsyncMock(
            return_value=(
                event,
                {},
                "provider",
                [{"role": "user", "content": "private chat"}],
                "memory",
            )
        ),
        tools=lambda *_: SimpleNamespace(tools=[]),
        number=lambda key, default, *args: default,
        context=SimpleNamespace(
            tool_loop_agent=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text='{"status":"completed","result":"report","summary":"done"}'
                )
            )
        ),
    )
    job = long_tasks.create_task(state, "Report", "Write report")
    await long_tasks.run_task(owner, "umo", job)
    assert job["status"] == "completed"
    assert Path(job["workspace"]).is_dir()
    kwargs = owner.context.tool_loop_agent.call_args.kwargs
    assert kwargs["contexts"] == []
    # 长期任务必须把通知器接进工具 Agent，否则下游学习插件采集不到证据；
    # 任务文本用任务目标，而不是占位的“自主心跳”。
    assert kwargs["agent_hooks"] is hooks
    assert event.message_str == "Write report"


def test_semantic_cancel_includes_long_task():
    from data.plugins.astrbot_plugin_autonomous_companion.task_control import pending_tasks, cancel_tasks
    state = {}
    job = long_tasks.create_task(state, "Report", "Write report")
    handle = next(t for t in pending_tasks(state) if t["kind"] == "long_task")
    cancel_tasks(state, [handle["id"]])
    assert job["status"] == "cancelled"
    assert long_tasks.due_task(state) is None


@pytest.mark.asyncio
async def test_queue_web_api_preserves_target_isolation():
    from test_autonomous import make_plugin, module
    owner, state, event = make_plugin()
    api = module.CompanionPageAPI(owner)
    await api.perform({"umo": event.unified_msg_origin, "action": "long_task", "operation": "create", "title": "Report", "goal": "Write report"})
    job = state["long_tasks"][0]
    with pytest.raises(ValueError):
        await api.perform({"umo": "other:FriendMessage:999", "action": "long_task", "operation": "cancel", "id": job["id"]})
    assert job["status"] == "queued"
