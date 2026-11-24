import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.fixture
def executor(tmp_path, monkeypatch):
    """Provide a per-run event and locally controllable executor."""
    monkeypatch.setattr(long_tasks.StarTools, "get_data_dir", lambda _: tmp_path)
    state = {}

    async def prepare(_):
        return (
            SimpleNamespace(state=state, action_receipts=[], message_str="自主心跳"),
            {},
            "provider",
            [{"role": "user", "content": "private chat"}],
            "memory",
        )

    return SimpleNamespace(
        states={"umo": state},
        config={},
        save=AsyncMock(),
        check_current=AsyncMock(),
        notification_hooks=object(),
        prepare=AsyncMock(side_effect=prepare),
        tools=lambda *_: SimpleNamespace(tools=[]),
        number=lambda key, default, *args: default,
        logger=Mock(),
        context=SimpleNamespace(
            tool_loop_agent=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text='{"status":"completed","result":"report","summary":"done"}'
                )
            )
        ),
    )


@pytest.mark.asyncio
async def test_isolated_executor(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    outcome = await long_tasks.run_task(executor, "umo", job)
    assert outcome == job["updated_at"]
    assert job["status"] == "completed"
    assert Path(job["workspace"]).is_dir()
    kwargs = executor.context.tool_loop_agent.call_args.kwargs
    assert kwargs["contexts"] == []
    assert kwargs["agent_hooks"] is executor.notification_hooks
    assert kwargs["event"].message_str == "Write report"
    assert not kwargs["event"].long_task_is_current()
    snapshot = deepcopy(job)
    assert await long_tasks.run_task(executor, "umo", job) is None
    for operation in ("pause", "resume", "cancel"):
        with pytest.raises(ValueError):
            long_tasks.control_task(state, job["id"], operation)
    assert job == snapshot
    long_tasks.control_task(state, job["id"], "delete")
    assert state["long_tasks"] == []
    assert Path(job["workspace"]).is_dir()


def test_terminal_evidence_does_not_fill_active_capacity_or_auto_replay():
    state = {}
    for index in range(31):
        job = long_tasks.create_task(state, "Report", f"Goal {index}")
        if index % 2:
            long_tasks.control_task(state, job["id"], "cancel")
        else:
            job.update(status="completed", result=f"Report {index}")
    assert len(state["long_tasks"]) == 31
    assert state["long_tasks"][0]["result"] == "Report 0"
    with pytest.raises(ValueError):
        long_tasks.create_task(state, "Other report", "  GOAL  0  ")
    with pytest.raises(ValueError):
        long_tasks.create_task(state, "Other report", "Goal 1")
    long_tasks.control_task(state, job["id"], "delete")
    with pytest.raises(ValueError):
        long_tasks.create_task(state, "Other report", "Goal 30")
    job = long_tasks.create_task(state, "Other report", "Goal 30", explicit=True)
    with pytest.raises(ValueError):
        long_tasks.create_task(state, "Other report", job["goal"], explicit=True)
    for index in range(29):
        long_tasks.create_task(state, "New", f"New {index}")
    with pytest.raises(ValueError):
        long_tasks.create_task(state, "Extra", "Another goal")


@pytest.mark.parametrize("operation", ["cancel", "delete", "pause"])
@pytest.mark.parametrize("late_failure", [False, True])
@pytest.mark.asyncio
async def test_late_worker_cannot_overwrite_controls(executor, operation, late_failure):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    fresh_response = SimpleNamespace(
        completion_text='{"status":"completed","result":"fresh result"}'
    )

    async def agent(event, **_):
        assert event.long_task_is_current()
        if started.is_set():
            assert event.action_receipts == [{"tool": "work", "status": "verified"}]
            return fresh_response
        receipt = {"tool": "work", "status": "verified"}
        event.action_receipts.append(receipt)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        assert not event.long_task_is_current()
        receipt["status"] = "failed"
        event.action_receipts.append({"tool": "late", "status": "verified"})
        if late_failure:
            raise RuntimeError("late failure")
        return SimpleNamespace(
            completion_text='{"status":"completed","result":"stale"}'
        )

    executor.context.tool_loop_agent.side_effect = agent
    worker = asyncio.create_task(long_tasks.run_task(executor, "umo", job))
    try:
        await asyncio.wait_for(started.wait(), 3)
        unrelated = long_tasks.create_task(state, "Other", "Other goal")
        long_tasks.control_task(state, unrelated["id"], "cancel")
        assert not cancelled.is_set()
        assert await long_tasks.run_task(executor, "umo", job) is None
        long_tasks.control_task(state, job["id"], operation)
        await asyncio.wait_for(cancelled.wait(), 3)
        if operation == "pause":
            assert job["status"] == "paused"
            paused = deepcopy(job)
            with pytest.raises(ValueError):
                long_tasks.control_task(state, job["id"], "pause")
            assert job == paused
            long_tasks.control_task(state, job["id"], "resume")
            assert await long_tasks.run_task(executor, "umo", job) == job["updated_at"]
            assert job["result"] == "fresh result"
        elif operation == "delete":
            assert all(item is not job for item in state["long_tasks"])
            assert await long_tasks.run_task(executor, "umo", job) is None
        else:
            assert job["status"] == "cancelled"
            assert job in state["long_tasks"]
        snapshot = deepcopy(state)
        job_snapshot = deepcopy(job)
        save_count = executor.save.await_count
    finally:
        release.set()
        outcome = await asyncio.wait_for(worker, 3)
    assert outcome is None
    assert state == snapshot
    assert job == job_snapshot
    assert executor.save.await_count == save_count
    executor.logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_delete_during_final_save_suppresses_scheduler_outcome(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    saving = asyncio.Event()
    release = asyncio.Event()

    async def save():
        if job["status"] == "completed":
            saving.set()
            await release.wait()

    executor.save.side_effect = save
    worker = asyncio.create_task(long_tasks.run_task(executor, "umo", job))
    try:
        await asyncio.wait_for(saving.wait(), 3)
        assert job["result"] == "report"
        long_tasks.control_task(state, job["id"], "delete")
        assert not state["long_tasks"]
    finally:
        release.set()
        outcome = await asyncio.wait_for(worker, 3)
    assert outcome is None
    assert not state["long_tasks"]


@pytest.mark.asyncio
async def test_external_interruption_keeps_attempt_evidence(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    started = asyncio.Event()

    async def agent(event, **_):
        event.action_receipts.append({"tool": "work", "status": "attempted"})
        started.set()
        await asyncio.Future()

    executor.context.tool_loop_agent.side_effect = agent
    worker = asyncio.create_task(long_tasks.run_task(executor, "umo", job))
    await asyncio.wait_for(started.wait(), 3)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, 3)
    assert job["status"] == "waiting"
    assert job["receipts"] == [{"tool": "work", "status": "attempted"}]
    assert long_tasks.due_task(state) is None
    executor.save.assert_awaited()


@pytest.mark.asyncio
async def test_review_resume_keeps_progress_and_resets_only_stage_budget(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    job.update(steps=11, result="Earlier work", failures=2)
    executor.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text='{"status":"continue","summary":"made progress"}'
    )
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "waiting"
    assert job["steps"] == 12
    previous = deepcopy(job)
    long_tasks.control_task(state, job["id"], "resume")
    for key in ("steps", "failures", "attempts", "result", "log", "receipts"):
        assert job[key] == previous[key]
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "queued"
    assert job["steps"] == 13
    assert job["steps_since_review"] == 1
    assert job["result"] == "Earlier work"


@pytest.mark.asyncio
async def test_completion_resolves_only_matching_older_receipt(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Write report")
    job["receipts"] = [
        {"tool": "work", "status": "failed", "call_key": "same"},
        {"tool": "work", "status": "verified", "call_key": "other"},
    ]
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "waiting"
    assert job["result"] == "report"
    long_tasks.control_task(state, job["id"], "resume")
    job["receipts"].append({"tool": "work", "status": "verified", "call_key": "same"})
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "completed"
    assert [r["status"] for r in job["receipts"]] == ["failed", "verified", "verified"]


@pytest.mark.asyncio
async def test_round_and_tool_counters_are_per_stage(executor, monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import daily

    monkeypatch.setattr(daily, "night_pool_key", lambda owner: "test-night")
    state = executor.states["umo"]
    state["tool_calls"] = 24
    job = long_tasks.create_task(state, "Report", "Write report")

    async def agent(**_):
        assert state["tool_calls"] == 0
        state["tool_calls"] += 5
        return SimpleNamespace(completion_text='{"status":"continue"}')

    executor.context.tool_loop_agent.side_effect = agent
    for _ in range(2):
        job["next_due"] = 0
        await long_tasks.run_task(executor, "umo", job)
    assert job["steps"] == 2
    assert state["usage"]["rounds"] == state["night_usage"]["rounds"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal", "tool"),
    [
        ("把报告保存到文件", "autonomous_write_file"),
        ("Run the script", "autonomous_run_command"),
        ("Save the report", "autonomous_write_file"),
    ],
)
async def test_action_goal_waits_for_receipt(executor, goal, tool):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", goal)
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "waiting"

    long_tasks.control_task(state, job["id"], "resume")
    job["receipts"].append({"tool": tool, "status": "success"})
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "completed"


@pytest.mark.asyncio
async def test_text_only_instructions_do_not_require_execution(executor):
    state = executor.states["umo"]
    job = long_tasks.create_task(state, "Report", "Explain how to run the script")
    await long_tasks.run_task(executor, "umo", job)
    assert job["status"] == "completed"


def test_semantic_cancel_includes_long_task():
    from data.plugins.astrbot_plugin_autonomous_companion.task_control import (
        cancel_tasks,
        pending_tasks,
    )

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
    await api.perform(
        {
            "umo": event.unified_msg_origin,
            "action": "long_task",
            "operation": "create",
            "title": "Report",
            "goal": "Write report",
        }
    )
    job = state["long_tasks"][0]
    with pytest.raises(ValueError):
        await api.perform(
            {
                "umo": "other:FriendMessage:999",
                "action": "long_task",
                "operation": "cancel",
                "id": job["id"],
            }
        )
    assert job["status"] == "queued"
