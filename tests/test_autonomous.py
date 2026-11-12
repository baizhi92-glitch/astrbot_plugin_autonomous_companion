import asyncio
import importlib.util
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main", Path(__file__).parents[1] / "main.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_plugin():
    plugin = object.__new__(module.AutonomousCompanion)
    plugin.config = {"targets": ["test:FriendMessage:123"], "enabled": True}
    plugin.config["show_tool_calls"] = False
    plugin.config["ignore_budget_while_active"] = False
    plugin.config["general_execution_contract"] = False
    plugin.config["natural_task_closing"] = False
    # 默认关闭昼夜分池，让既有预算用例与真实时钟无关；分池与早报另有专门用例。
    plugin.config["split_night_budget"] = False
    plugin.config["morning_report_enabled"] = False
    plugin.logger = logging.getLogger("test")
    plugin.stopping = False
    plugin.revisions = {}
    plugin.running = {}
    plugin.chat_tasks = {}
    plugin.busy = {}
    plugin.save = AsyncMock()
    plugin.context = SimpleNamespace(
        get_all_stars=lambda: [],
        get_config=lambda *_: {"provider_settings":{"show_tool_use_status":False}},
        register_web_api=lambda *args: None,
        platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid")),
        llm_generate=AsyncMock(), tool_loop_agent=AsyncMock(), send_message=AsyncMock(return_value=True),
    )
    state = plugin.new_state()
    plugin.states = {"test:FriendMessage:123": state}
    event = SimpleNamespace(
        unified_msg_origin="test:FriendMessage:123", revision=0, cid="cid", state=state,
        send=AsyncMock(), session="test:FriendMessage:123", deliveries=0,
        clear_result=lambda: None,
        plugins_name=None,
        get_sender_id=lambda: "123",
        get_sender_name=lambda: "朋友",
        get_self_id=lambda: "999",
        get_message_str=lambda: "在",
        get_extra=lambda *_: False,
    )
    plugin.prepare = AsyncMock(return_value=(event, {"prompt": "persona"}, "provider", [], "memory"))
    plugin.tools = lambda *_: module.ToolSet()
    return plugin, state, event


@pytest.mark.asyncio
async def test_generated_image_archive_survives_producer_offline(tmp_path, monkeypatch):
    import base64
    from data.plugins.astrbot_plugin_autonomous_companion import library
    monkeypatch.setattr(library.StarTools, "get_data_dir", lambda _: tmp_path)
    raw = b"\x89PNG\r\n\x1a\nimage-bytes"
    reader = AsyncMock(return_value="data:image/png;base64," + base64.b64encode(raw).decode())
    original = library.read_library_image
    monkeypatch.setattr(library, "read_library_image", reader)
    state = {"work_library": [{"id": "image", "kind": "image", "url": "http://127.0.0.1:8188/view?filename=x.png"}]}
    await library.archive_library_images(state, "http://127.0.0.1:8188", {"image"})
    item = state["work_library"][0]
    assert Path(item["path"]).read_bytes() == raw
    assert item["archive_status"] == "saved"
    monkeypatch.setattr(library, "read_library_image", original)
    assert await original(state, "image", "http://offline.invalid") == "data:image/png;base64," + base64.b64encode(raw).decode()


@pytest.mark.asyncio
async def test_semantic_cancel_tool_stops_selected_task_and_preserves_unrelated_work():
    plugin, state, event = make_plugin()
    event.get_extra = lambda key: None
    event.get_message_str = lambda: "网络已经好了，这个以后不用折腾了"
    task = "复测8.8.8.8延迟"
    state["future_task"] = task
    state["execution_checkpoints"] = {"ping": {"task": task, "pending": True}, "story": {"task": "写故事", "pending": True}}
    listed = json.loads(await plugin.autonomous_task_control(event, "list", []))
    selected = next(item for item in listed["tasks"] if item["task"] == task)
    result = json.loads(await plugin.autonomous_task_control(event, "cancel", [selected["id"]], "这个以后不用折腾了"))
    assert result["ok"]
    assert not state["future_task"]
    assert not state["execution_checkpoints"]["ping"]["pending"]
    assert state["execution_checkpoints"]["story"]["pending"]
    assert state["cancelled_tasks"]


@pytest.mark.asyncio
async def test_cancel_tool_requires_current_user_evidence_and_known_handles():
    plugin, state, event = make_plugin()
    event.get_extra = lambda key: None
    event.get_message_str = lambda: "你好"
    state["future_task"] = "task"
    result = json.loads(await plugin.autonomous_task_control(event, "cancel", ["invented"], "别做了"))
    assert not result["ok"] and state["future_task"] == "task"


@pytest.mark.parametrize("text", ["不要再测了", "不用复测了", "别测试了", "停止ping", "测试取消"])
def test_explicit_stop_testing_removes_old_checkpoint(text):
    from data.plugins.astrbot_plugin_autonomous_companion.cancellations import cancel_from_user_text, cancelled_match
    task = "复测到8.8.8.8的延迟与抖动，对比15:03结果"
    state = {"future_task": task, "execution_checkpoint": {"task": task}, "execution_checkpoints": {"test": {"task": task, "pending": True}, "story": {"task": "写小说", "pending": True}}}
    assert cancel_from_user_text(state, text)
    assert not state["future_task"]
    assert "execution_checkpoint" not in state
    assert "test" not in state["execution_checkpoints"]
    assert "story" in state["execution_checkpoints"]
    assert cancelled_match(state, task)


def test_testing_question_does_not_cancel_existing_work():
    from data.plugins.astrbot_plugin_autonomous_companion.cancellations import cancel_from_user_text
    state = {"future_task": "复测8.8.8.8延迟"}
    assert not cancel_from_user_text(state, "测试结果怎么样？")
    assert state["future_task"]


@pytest.mark.asyncio
async def test_library_manual_add_archive_restore_preserves_original(tmp_path):
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    path = tmp_path / "original.txt"
    path.write_text("source", encoding="utf-8")
    state["work_library"] = [{"id": "original", "kind": "text", "title": "Original", "path": str(path), "content_hash": "hash"}]
    await page.perform({"umo": event.unified_msg_origin, "action": "work_archive", "ids": ["original"]})
    assert state["library_archived"]["hash:hash"]
    assert path.read_text(encoding="utf-8") == "source"
    await page.perform({"umo": event.unified_msg_origin, "action": "work_restore", "ids": ["original"]})
    assert not state["library_archived"]
    await page.perform({"umo": event.unified_msg_origin, "action": "work_add_text", "title": "Manual", "content": "body"})
    await page.perform({"umo": event.unified_msg_origin, "action": "work_add_text", "title": "Manual", "content": "body"})
    assert len(state["work_library"]) == 2


@pytest.mark.parametrize("copies", [1, 2, 3])
def test_title_prefixed_local_text_matches_synced_body(tmp_path, copies):
    from data.plugins.astrbot_plugin_autonomous_companion.library import refresh_text_fingerprints
    path = tmp_path / "chapter.txt"
    path.write_text("短篇·第一段\n\n" * copies + "实际正文", encoding="utf-8")
    state = {"work_library": [
        {"id": "local", "title": "短篇·第一段", "kind": "text", "path": str(path)},
        {"id": "remote", "title": "短篇·第一段", "kind": "text", "inline_content": "实际正文"},
    ]}
    refresh_text_fingerprints(state)
    assert state["work_library"][0]["content_hash"] == state["work_library"][1]["content_hash"]


def test_compile_chapters_orders_deduplicates_and_rejects_conflicts(tmp_path, monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import compiled_work
    monkeypatch.setattr(compiled_work.StarTools, "get_data_dir", lambda _: tmp_path)
    state = {"work_library": [
        {"id": "two", "title": "短篇·第二段", "kind": "text", "inline_content": "结尾"},
        {"id": "one", "title": "短篇·第一段", "kind": "text", "inline_content": "开头"},
        {"id": "duplicate", "title": "短篇·第一段", "kind": "text", "inline_content": "短篇·第一段\n\n开头"},
    ]}
    result = compiled_work.compile_chapters(state, "session", "短篇")
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert text.index("开头") < text.index("结尾")
    assert text.count("开头") == 1
    assert compiled_work.compile_chapters(state, "session", "短篇")["id"] == result["id"]
    state["work_library"].append({"id": "conflict", "title": "短篇·第二段", "kind": "text", "inline_content": "另一个结尾"})
    with pytest.raises(ValueError, match="不同版本"):
        compiled_work.compile_chapters(state, "session", "短篇")
    with pytest.raises(ValueError, match="至少"):
        compiled_work.compile_chapters(state, "session", "短篇", ["two", "conflict"][:1])
    state["work_library"].append({"id": "three", "title": "短篇·第三段", "kind": "text", "inline_content": "第三段"})
    with pytest.raises(ValueError, match="缺号"):
        compiled_work.compile_chapters(state, "session", "短篇", ["one", "three"])


@pytest.mark.asyncio
async def test_completed_project_stops_requested_extra_chapter():
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "story", "title": "Station story", "status": "active", "next_step": "write ending"}]
    state["manual_plan"] = "finish Station story"
    state["manual_project_id"] = "story"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({
        "message": "", "summary": "ending written", "continue_after_seconds": 90,
        "next_task": "write another chapter", "future_task": "continue story",
        "project_updates": [{"id": "story", "status": "completed", "next_step": "", "progress": "complete ending"}]
    }))
    await plugin.turn(event.unified_msg_origin)
    assert state["projects"][0]["status"] == "completed"
    assert not state["next_task"] and not state["future_task"] and not state["manual_plan"]


@pytest.mark.asyncio
async def test_next_chat_round_sees_actual_dispatch_even_without_history_or_journal():
    from data.plugins.astrbot_plugin_autonomous_companion.dialogue_context import remember_dispatch
    plugin, state, event = make_plugin()
    remember_dispatch(event, "不催了，明天再看也行。")
    state["journal"] = [{"action": "tool_result", "summary": "noise"}] * 16
    state["manual_plan"] = "连续骚扰对方"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"先接着做点自己的事。","summary":"自然交流"}')
    await plugin.turn(event.unified_msg_origin)
    prompt = plugin.context.tool_loop_agent.call_args.kwargs["prompt"]
    assert "不催了，明天再看也行。" in prompt
    assert "不要马上催看" in prompt


def test_dispatch_context_is_bounded_and_current_conversation_only():
    from data.plugins.astrbot_plugin_autonomous_companion.dialogue_context import remember_dispatch, dispatched_context
    _, state, event = make_plugin()
    for i in range(20):
        remember_dispatch(event, str(i) + "x" * 1000)
    assert len(state["visible_dispatches"]) == 12
    other = dispatched_context(state, "different-cid")
    assert "xxxxxxxx" not in other
    assert len(dispatched_context(state, event.cid)) < 14000


def test_proactive_segments_preserve_paragraph_content_and_code():
    from data.plugins.astrbot_plugin_autonomous_companion.message_segments import split_proactive_text
    text = "睡前看看吧。\n\n" + "车站没有人。" * 80 + "\n\n明天再聊。"
    parts = split_proactive_text(text, 100)
    assert len(parts) > 3
    assert all(len(part) <= 100 for part in parts)
    assert "".join(parts) == text.replace("\n\n", "")
    code = "```python\n" + "print(1)\n" * 100 + "```"
    assert split_proactive_text(code) == [code]


@pytest.mark.asyncio
async def test_proactive_plain_dispatch_splits_without_splitting_media():
    plugin, state, event = make_plugin()
    plugin.config["proactive_segment_interval_ms"] = 0
    plugin.deliver_segment = AsyncMock()
    await plugin.deliver(event, module.MessageChain().message("第一段。\n\n第二段。"))
    assert plugin.deliver_segment.await_count == 2
    assert plugin.deliver_segment.await_args_list[0].args[1].chain[0].text == "第一段。"
    plugin.deliver_segment.reset_mock()
    await plugin.deliver(event, module.MessageChain().message("简短一句。"))
    plugin.deliver_segment.assert_awaited_once()


@pytest.mark.asyncio
async def test_continuous_manual_chat_cannot_finish_after_first_send_or_drift_next_task():
    plugin, state, event = make_plugin()
    state["manual_plan"] = "连续骚扰对方"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({
        "message": "又想到一个想告诉你的事。", "summary": "交流一次", "manual_plan_completed": True,
        "continue_after_seconds": 0, "next_task": "写小说第四段", "future_task": ""
    }))
    await plugin.turn(event.unified_msg_origin)
    assert state["manual_plan"] == "连续骚扰对方"
    assert "继续原手动聊天目标" in state["next_task"]
    assert "第四段" not in state["next_task"]
    assert 80 < state["next_due"] - time.time() <= 90
    assert state["last_turn"]["continuation_scheduled"] is True
    assert state["last_turn"]["manual_task_pending"] is True


@pytest.mark.asyncio
async def test_foreign_project_update_does_not_block_chat_delivery():
    plugin, state, event = make_plugin()
    state["manual_plan"] = "连续骚扰对方"
    state["projects"] = [{"id": "local", "title": "Local story", "status": "active", "progress": "keep"}]
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({
        "message": "刚想到一句话想告诉你。", "summary": "聊天", "continue_after_seconds": 90,
        "next_task": "继续自然交流", "project_updates": [{"id": "external_eefc654c672f7455", "progress": "foreign draft"}]
    }))
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_awaited_once()
    assert state["projects"][0]["progress"] == "keep"
    assert len(state["projects"]) == 1
    assert "external_eefc654c672f7455" in state["last_turn"]["project_update_warning"]
    assert state["last_turn"]["status"] != "failed"


@pytest.mark.parametrize("task", ["连续骚扰对方", "连续找白骚扰", "继续和对方聊聊天"])
@pytest.mark.asyncio
async def test_plain_manual_chat_skips_external_contract_and_old_clarification(task):
    plugin, state, event = make_plugin()
    plugin.config["general_execution_contract"] = True
    state["manual_plan"] = task
    state["execution_checkpoint"] = {"task": task, "cid": "cid", "pending": True, "contract": {"mode": "action", "steps": [{"goal": "澄清", "tools": [], "accept": "observed", "criterion": "用户回应"}]}}
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"刚想到一个想跟你说的事。","summary":"自然聊天","continue_after_seconds":90,"next_task":"补充一个新想法"}')
    await plugin.turn(event.unified_msg_origin)
    plugin.context.llm_generate.assert_not_called()
    assert state["last_turn"]["action"] == "chat"
    assert event.execution_contract == {"mode": "reply", "steps": []}
    event.send.assert_awaited()


@pytest.mark.parametrize("task", ["连续找白骚扰并画一张图", "聊天后保存文件", "查询天气", "给对方发送文件"])
def test_mixed_external_tasks_are_not_plain_conversation(task):
    assert not module.is_plain_conversation(task)


@pytest.mark.asyncio
async def test_quiet_manual_reply_is_blocked_without_unrelated_background_work():
    plugin, state, event = make_plugin()
    plugin.config["general_execution_contract"] = True
    state["manual_plan"] = "连续找白聊聊天"
    state["last_turn"] = {"status": "running"}
    state["companion_status"] = "陪伴插件暂缓：private_companion_quiet_hours"
    event.companion_adapter = SimpleNamespace(quiet=True, prompt="旧项目：车站短篇")
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"mode":"reply","steps":[]}')
    await plugin.execute_turn(event.unified_msg_origin, False, event, {"prompt": "persona"}, "provider", [], "memory")
    plugin.context.tool_loop_agent.assert_not_called()
    assert state["last_turn"]["status"] == "blocked"
    assert "private_companion_quiet_hours" in state["last_turn"]["blocked_reason"]
    assert state["manual_plan"] == "连续找白聊聊天"


@pytest.mark.asyncio
async def test_livingmemory_bridge_selection_and_assistant_recording():
    plugin, state, event = make_plugin()
    living = SimpleNamespace(autonomous_recall=AsyncMock(return_value="scoped memory"), autonomous_record=AsyncMock())
    meta = SimpleNamespace(activated=True, name="LivingMemory", root_dir_name="astrbot_plugin_livingmemory", star_cls=living)
    plugin.context.get_all_stars = lambda: [meta]
    bridge = plugin.memory_bridge(event)
    assert await bridge.compose_context(query="topic", session_context={}, top_k=6, max_chars=5000) == "scoped memory"
    living.autonomous_recall.assert_awaited_once_with(event, query="topic", top_k=6, max_chars=5000)
    await bridge.record_visible_turn(role="assistant", content="sent")
    living.autonomous_record.assert_awaited_once_with(event, content="sent")
    with pytest.raises(ValueError):
        await bridge.record_visible_turn(role="user", content="fake heartbeat")
    event.plugins_name = ["Other"]
    assert plugin.memory_bridge(event) is None
    event.plugins_name = None
    plugin.config["livingmemory_integration"] = False
    assert plugin.memory_bridge(event) is None


@pytest.mark.parametrize("text", ['[]', 'hello', '{"action":"silent"} trailing'])
def test_invalid_control_is_rejected(text):
    with pytest.raises(ValueError):
        module.parse_object(text)


def test_fenced_control_is_accepted():
    assert module.parse_object('```json\n{"action":"silent"}\n```')["action"] == "silent"


@pytest.mark.asyncio
async def test_web_action_uses_real_astrbot_request_json_contract():
    from astrbot.api.web import PluginRequest, bind_request_context
    from starlette.requests import Request

    plugin, state, event = make_plugin()
    payload = json.dumps({"umo":event.unified_msg_origin,"action":"plan","value":"web plan"}).encode()
    async def receive():
        return {"type":"http.request","body":payload,"more_body":False}
    raw = Request({"type":"http","method":"POST","path":"/action","headers":[(b"content-type",b"application/json")],"query_string":b"","scheme":"http","server":("localhost",6185)}, receive)
    with bind_request_context(PluginRequest(raw)):
        result = await module.CompanionPageAPI(plugin).action()
    assert result["status"] == "ok"
    assert state["future_task"] == "web plan"


@pytest.mark.asyncio
async def test_silent_does_not_execute_or_send():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    plugin.context.tool_loop_agent.assert_not_called()
    event.send.assert_not_called()
    assert not state["next_task"]
    assert state["next_due"] > time.time()


@pytest.mark.asyncio
async def test_preview_does_not_start_window_or_execute():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","task":"write"}')
    await plugin.turn(event.unified_msg_origin, preview=True)
    assert state["window_end"] == 0
    assert state["rounds"] == 0
    plugin.context.tool_loop_agent.assert_not_called()


@pytest.mark.asyncio
async def test_continues_without_user_reply_and_keeps_budget():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","task":"write"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({
        "message": "a story", "summary": "wrote a story", "continue_after_seconds": 60, "next_task": "illustrate it"
    }))
    await plugin.turn(event.unified_msg_origin)
    assert state["next_task"] == "illustrate it"
    assert state["rounds"] == 1
    state["tool_calls"] = 3
    await plugin.turn(event.unified_msg_origin)
    assert state["rounds"] == 2
    assert state["tool_calls"] == 3
    assert event.send.await_count == 2
    assert "wrote a story" in plugin.context.llm_generate.call_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_max_rounds_ends_continuation():
    plugin, state, event = make_plugin()
    plugin.config["max_rounds"] = 1
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"tools","task":"search"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"","summary":"searched","continue_after_seconds":30,"next_task":"more"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["next_task"] == ""


@pytest.mark.asyncio
async def test_invalid_action_output_never_leaks_control():
    plugin, _, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"chat"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text="internal control failure")
    await plugin.turn(event.unified_msg_origin)
    assert plugin.context.tool_loop_agent.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "conversation", "disable", "remove"])
async def test_stale_turn_cannot_send(change):
    plugin, _, event = make_plugin()
    if change == "revision":
        plugin.revisions[event.unified_msg_origin] = 1
    elif change == "conversation":
        plugin.context.conversation_manager.get_curr_conversation_id.return_value = "new"
    elif change == "disable":
        plugin.config["enabled"] = False
    else:
        plugin.config["targets"] = []
    with pytest.raises(asyncio.CancelledError):
        await plugin.deliver(event, module.MessageChain().message("stale"))
    plugin.context.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_tool_budget_is_shared_and_does_not_double_forward_direct_result(monkeypatch):
    plugin, state, event = make_plugin()
    event.owner = plugin
    plugin.config["max_tool_calls"] = 1
    executed = []

    async def execute(*args, **kwargs):
        executed.append(True)
        yield None
        yield CallToolResult(content=[TextContent(type="text", text="done")])

    monkeypatch.setattr(module.FunctionToolExecutor, "execute", execute)
    tool = module.FunctionTool(name="test", description="test", parameters={})
    first = module.BudgetTool(tool, event)
    second = module.BudgetTool(tool, event)
    results = [r async for r in first.call(None)]
    assert len(results) == 1
    denied = [r async for r in second.call(None)]
    assert "exhausted" in denied[0].content[0].text
    assert len(executed) == 1
    assert state["tool_calls"] == 1


@pytest.mark.asyncio
async def test_inbound_cancels_running_and_clears_continuation():
    plugin, state, event = make_plugin()
    task = asyncio.create_task(asyncio.sleep(60))
    plugin.running[event.unified_msg_origin] = task
    event.get_extra = lambda _: False
    event.get_sender_id = lambda: "123"
    event.get_sender_name = lambda: "user"
    event.get_self_id = lambda: "bot"
    event.get_message_str = lambda: "hello"
    state["next_task"] = "old task"
    await plugin.observe(event)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not state["next_task"]
    assert state["identity"]["bot_id"] == "bot"


@pytest.mark.asyncio
async def test_dispatch_failure_is_not_recorded_as_visible_message():
    plugin, state, event = make_plugin()
    plugin.context.send_message.return_value = False
    with pytest.raises(RuntimeError):
        await plugin.deliver(event, module.MessageChain().message("hello"))
    assert event.deliveries == 0
    assert state["journal"][-1]["action"] == "dispatch_attempt"


@pytest.mark.asyncio
async def test_dispatch_appends_assistant_only_and_records_memory():
    plugin, _, event = make_plugin()
    event.message_obj = SimpleNamespace(message_id="event")
    event.persona_id = "persona"
    event.get_platform_name = lambda: "test"
    event.get_sender_id = lambda: "123"
    event.get_self_id = lambda: "bot"
    manager = plugin.context.conversation_manager
    manager.get_conversation = AsyncMock(return_value=SimpleNamespace(history='[{"role":"user","content":"old"}]'))
    manager.update_conversation = AsyncMock()
    bridge = SimpleNamespace(record_visible_turn=AsyncMock())
    plugin.memory_bridge = lambda _: bridge
    await plugin.deliver(event, module.MessageChain().message("hello"))
    history = manager.update_conversation.call_args.kwargs["history"]
    assert history == [{"role": "user", "content": "old"}, {"role": "assistant", "content": "hello"}]
    bridge.record_visible_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_wrapper_with_real_astrbot_executor():
    plugin, state, event = make_plugin()
    event.get_result = lambda: None
    event.owner = plugin
    seen = []

    async def handler(actual_event):
        seen.append(actual_event)
        return "tool output"

    tool = module.FunctionTool(name="real", description="test", parameters={}, handler=handler)
    wrapper = module.BudgetTool(tool, event)
    context = SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=5)
    results = [result async for result in module.FunctionToolExecutor.execute(wrapper, context)]
    assert results[0].content[0].text == "tool output"
    assert seen == [event]
    assert state["tool_calls"] == 1


@pytest.mark.asyncio
async def test_prepare_reads_scoped_memory_and_real_platform_identity():
    plugin, state, _ = make_plugin()
    del plugin.prepare
    state["identity"] = {"user_id": "123", "user_name": "user", "bot_id": "bot"}
    platform_meta = module.CronMessageEvent.__init__.__globals__["PlatformMetadata"](
        name="aiocqhttp", id="test", description="test"
    )
    platform = SimpleNamespace(status=module.PlatformStatus.RUNNING,
                               meta=lambda: platform_meta, client_self_id="fallback")
    plugin.context.get_platform_inst = lambda _: platform
    plugin.context.get_config = lambda _: {"admins_id": ["123"]}
    plugin.context.conversation_manager.get_conversation = AsyncMock(return_value=SimpleNamespace(
        persona_id="persona", history='[{"role":"user","content":"story"}]'
    ))
    plugin.context.persona_manager = SimpleNamespace(resolve_selected_persona=AsyncMock(
        return_value=("persona", {"prompt": "actual persona", "tools": None}, None, False)
    ))
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="provider")
    bridge = SimpleNamespace(compose_context=AsyncMock(return_value="remembered fact"))
    plugin.context.get_all_stars = lambda: [SimpleNamespace(
        activated=True, name="MemoryCompanion", root_dir_name="astrbot_plugin_memory_companion",
        star_cls=SimpleNamespace(memory_companion=bridge),
    )]
    event, persona, provider, history, memory = await plugin.prepare("test:FriendMessage:123")
    assert memory == "remembered fact"
    assert event.get_self_id() == "bot"
    assert event.get_sender_id() == "123"
    assert event.get_platform_name() == "aiocqhttp"
    assert event.role == "admin"
    scope = bridge.compose_context.call_args.kwargs["session_context"]
    assert scope["user_id"] == "123" and scope["bot_id"] == "bot"
    assert scope["session_id"] == "test:FriendMessage:123"
    assert history == [{"role": "user", "content": "story"}]
    assert persona["prompt"] == "actual persona"
    assert provider == "provider"


@pytest.mark.asyncio
async def test_restart_does_not_replay_interrupted_action():
    plugin, state, event = make_plugin()
    state.update(inflight=True, next_task="potential side effect", next_due=0)
    state["future_task"] = "future creative project"
    plugin.get_kv_data = AsyncMock(return_value=plugin.states)
    plugin.scheduler = AsyncMock()
    await plugin.initialize()
    assert not state["inflight"]
    assert not state["next_task"]
    assert state["next_due"] > time.time()
    assert state["future_task"] == "future creative project"
    await plugin.terminate()


def test_qq_number_automatically_resolves_without_enable_command():
    plugin, _, _ = make_plugin()
    plugin.config["targets"] = ["123", "123"]
    plugin.context.platform_manager.platform_insts = [SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq_instance", name="aiocqhttp")
    )]
    assert plugin.target_origins() == ["qq_instance:FriendMessage:123"]
    assert plugin.config["enabled"]


def test_multiple_platforms_require_explicit_route_for_numeric_target():
    plugin, _, _ = make_plugin()
    plugin.config["targets"] = ["123"]
    plugin.context.platform_manager.platform_insts = [
        SimpleNamespace(meta=lambda: SimpleNamespace(id="one", name="aiocqhttp")),
        SimpleNamespace(meta=lambda: SimpleNamespace(id="two", name="aiocqhttp")),
    ]
    assert plugin.target_origins() == []
    plugin.config["platform_id"] = "two"
    assert plugin.target_origins() == ["two:FriendMessage:123"]


@pytest.mark.asyncio
async def test_silent_can_remember_plan_for_next_hour():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({
        "action": "silent", "future_task": "write the next chapter"
    }))
    await plugin.turn(event.unified_msg_origin)
    assert state["future_task"] == "write the next chapter"
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    assert "write the next chapter" in plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert state["future_task"] == "write the next chapter"


@pytest.mark.asyncio
async def test_completed_plan_is_cleared():
    plugin, state, event = make_plugin()
    state["future_task"] = "write chapter"
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","task":"write chapter"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"done","summary":"finished","future_task":""}')
    await plugin.turn(event.unified_msg_origin)
    assert state["future_task"] == ""


@pytest.mark.asyncio
async def test_exhausted_window_keeps_unfinished_plan():
    plugin, state, event = make_plugin()
    plugin.config["max_rounds"] = 1
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"","summary":"drafted","continue_after_seconds":30,"next_task":"illustrate chapter"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["next_task"] == ""
    assert state["future_task"] == "illustrate chapter"


@pytest.mark.asyncio
async def test_preview_does_not_change_future_plan():
    plugin, state, event = make_plugin()
    state["future_task"] = "original"
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent","future_task":"changed"}')
    await plugin.turn(event.unified_msg_origin, preview=True)
    assert state["future_task"] == "original"


def test_projects_update_preserves_existing_progress_and_rejects_unknown_id():
    state = {}
    module.apply_plan(state, {"project_updates": [{"title": "Story", "goal": "write", "progress": "chapter one"}]})
    project_id = state["projects"][0]["id"]
    module.apply_plan(state, {"project_updates": [{"id": project_id, "next_step": "chapter two"}]})
    assert state["projects"][0]["progress"] == "chapter one"
    with pytest.raises(ValueError):
        module.apply_plan(state, {"future_task": "changed", "project_updates": [{"id": "missing", "title": "no"}]})
    assert "future_task" not in state


def test_project_history_duplicate_capacity_and_long_progress_are_safe():
    import copy
    state = {}
    module.apply_plan(state, {"project_updates": [{"title": "Story", "progress": "original"}]})
    identifier = state["projects"][0]["id"]
    module.apply_plan(state, {"project_updates": [{"title": "Story", "progress": "revision"}]})
    assert len(state["projects"]) == 1
    assert state["projects"][0]["history"][0]["progress"] == "original"
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match="6000"):
        module.apply_plan(state, {"project_updates": [{"id": identifier, "progress": "x" * 6001}]})
    assert state == before
    for i in range(4):
        module.apply_plan(state, {"project_updates": [{"title": f"Other {i}"}]})
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match="5"):
        module.apply_plan(state, {"future_task": "changed", "project_updates": [{"title": "Overflow"}]})
    assert state == before


def test_project_completion_is_phase_gated_and_rejects_remaining_step():
    state = {"projects": [{"id": "p", "title": "Story", "status": "active", "next_step": "chapter two"}]}
    update = {"id": "p", "status": "completed", "next_step": ""}
    module.apply_plan(state, {"project_updates": [update]}, allow_completion=False)
    assert state["projects"][0]["status"] == "active"
    module.apply_plan(state, {"project_updates": [{**update, "next_step": "still unfinished"}]})
    assert state["projects"][0]["status"] == "active"
    module.apply_plan(state, {"project_updates": [update]})
    assert state["projects"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_decision_cannot_persist_premature_project_progress_or_completion():
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "p", "title": "Story", "status": "active", "progress": "original"}]
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({"action": "silent", "project_updates": [{"id": "p", "status": "completed", "progress": "fabricated", "artifact": "fake.txt"}]}))
    await plugin.turn(event.unified_msg_origin)
    assert state["projects"][0]["status"] == "active"
    assert state["projects"][0]["progress"] == "original"
    assert not state["projects"][0].get("artifact")


@pytest.mark.asyncio
async def test_manual_project_binding_and_editing_paused_progress():
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "p", "title": "Story", "status": "paused", "progress": "original"}]
    page = module.CompanionPageAPI(plugin)
    with pytest.raises(ValueError, match="暂停"):
        await page.perform({"umo": event.unified_msg_origin, "action": "plan", "value": "unrelated wording", "project_id": "p"})
    await page.perform({"umo": event.unified_msg_origin, "action": "project_edit", "id": "p", "progress": "corrected"})
    assert state["projects"][0]["progress"] == "corrected"
    assert state["projects"][0]["history"][0]["progress"] == "original"
    await page.perform({"umo": event.unified_msg_origin, "action": "project", "id": "p", "status": "active"})
    await page.perform({"umo": event.unified_msg_origin, "action": "plan", "value": "unrelated wording", "project_id": "p"})
    assert state["manual_project_id"] == "p"
    await page.perform({"umo": event.unified_msg_origin, "action": "project", "id": "p", "status": "paused"})
    assert not state["manual_plan"]


@pytest.mark.asyncio
async def test_blocked_project_counts_failure_and_schedules_cooldown():
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "p", "title": "Story", "status": "active", "progress": "old"}]
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","task":"continue Story","project_id":"p","notify":false}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"","summary":"blocked","blocked_reason":"dependency unavailable","project_updates":[{"id":"p","status":"completed","next_step":""}]}')
    await plugin.turn(event.unified_msg_origin)
    assert state["projects"][0]["status"] == "active"
    assert state["projects"][0]["failure_count"] == 1
    assert state["projects"][0]["retry_after"] > time.time()


@pytest.mark.asyncio
async def test_checkpoint_streak_yields_to_fresh_decision():
    plugin, state, event = make_plugin()
    state["checkpoint_streak"] = 2
    state["execution_checkpoints"] = {"job": {"task": "old task", "pending": True, "cid": "cid"}}
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent","reason":"fresh choice"}')
    result = await plugin.turn(event.unified_msg_origin)
    assert result["reason"] == "fresh choice"
    assert state["checkpoint_streak"] == 0
    plugin.context.tool_loop_agent.assert_not_called()


@pytest.mark.parametrize("status", ["paused", "completed", "cancelled"])
def test_model_cannot_resume_or_overwrite_inactive_project(status):
    state = {"projects": [{"id": "story", "title": "Story", "status": status, "progress": "saved draft"}]}
    module.apply_plan(state, {"project_updates": [{"id": "story", "status": "active", "progress": "stale rewrite"}]})
    assert state["projects"][0]["status"] == status
    assert state["projects"][0]["progress"] == "saved draft"


@pytest.mark.asyncio
async def test_project_full_lifecycle_with_round_and_persistence():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    await page.perform({"umo": event.unified_msg_origin, "action": "project_add", "title": "Story", "goal": "write chapters"})
    identifier = state["projects"][0]["id"]
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({"action": "create", "task": "continue story", "project_id": identifier, "notify": False}))
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({"message": "", "summary": "draft", "project_updates": [{"id": identifier, "progress": "chapter one", "next_step": "chapter two"}]}))
    await plugin.turn(event.unified_msg_origin)
    assert state["projects"][0]["progress"] == "chapter one"
    assert state["projects"][0]["last_progress_at"] > 0
    # Exercise the actual serialization used by save, without writing user KV data.
    plugin.store_lock = asyncio.Lock()
    plugin.put_kv_data = AsyncMock()
    await module.AutonomousCompanion.save(plugin)
    restored = plugin.put_kv_data.call_args.args[1][event.unified_msg_origin]
    assert restored["projects"][0]["next_step"] == "chapter two"
    await page.perform({"umo": event.unified_msg_origin, "action": "project", "id": identifier, "status": "completed"})
    plugin.context.tool_loop_agent.reset_mock()
    await plugin.turn(event.unified_msg_origin)
    plugin.context.tool_loop_agent.assert_not_called()
    assert state["projects"][0]["status"] == "completed"
    await page.perform({"umo": event.unified_msg_origin, "action": "project_delete", "id": identifier})
    assert not state["projects"]
    await page.perform({"umo": event.unified_msg_origin, "action": "project_add", "title": "Story", "goal": "write chapters"})
    assert len(state["projects"]) == 1
    assert state["projects"][0]["id"] != identifier


@pytest.mark.asyncio
async def test_pause_project_stops_queued_checkpoint_and_preserves_draft():
    from data.plugins.astrbot_plugin_autonomous_companion.checkpoint import next_checkpoint
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "story", "title": "Long story", "goal": "write chapters", "status": "active", "progress": "chapter one"}]
    state["manual_plan"] = "continue Long story"
    state["future_task"] = "continue Long story"
    state["execution_checkpoints"] = {"job": {"task": "chapter two", "project_id": "story", "pending": True, "cid": "cid"}}
    await module.CompanionPageAPI(plugin).perform({"umo": event.unified_msg_origin, "action": "project", "id": "story", "status": "paused"})
    assert next_checkpoint(state, "cid") is None
    assert not state["manual_plan"] and not state["future_task"]
    assert state["projects"][0]["progress"] == "chapter one"
    await module.CompanionPageAPI(plugin).perform({"umo": event.unified_msg_origin, "action": "project", "id": "story", "status": "active"})
    assert state["projects"][0]["progress"] == "chapter one"
    assert next_checkpoint(state, "cid")["task"] == "chapter two"


@pytest.mark.asyncio
async def test_model_can_schedule_later_check_without_sending():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent","next_check_minutes":120}')
    await plugin.turn(event.unified_msg_origin)
    assert 7190 < state["next_due"] - time.time() <= 7200
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_background_work_updates_project_without_message():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","notify":false}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text=json.dumps({
        "message": "should not send", "summary": "drafted", "project_updates": [{"title": "Story", "progress": "actual draft"}]
    }))
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()
    assert state["projects"][0]["progress"] == "actual draft"
    with pytest.raises(RuntimeError):
        await plugin.deliver(event, module.MessageChain().message("tool output"))


@pytest.mark.asyncio
async def test_event_wakes_once_and_is_consumed_only_after_success():
    plugin, state, event = make_plugin()
    assert await plugin.notify_event(event.unified_msg_origin, "image complete", event_id="job-1")
    assert not await plugin.notify_event(event.unified_msg_origin, "image complete", event_id="job-1")
    assert state["next_due"] - time.time() <= 30
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text="invalid")
    with pytest.raises(ValueError):
        await plugin.turn(event.unified_msg_origin)
    assert len(state["events"]) == 1
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    assert not state["events"]
    assert not await plugin.notify_event(event.unified_msg_origin, "image complete", event_id="job-1")


@pytest.mark.asyncio
async def test_event_arriving_during_generation_is_retained():
    plugin, state, event = make_plugin()
    async def generate(**kwargs):
        await plugin.notify_event(event.unified_msg_origin, "new result", event_id="late")
        return SimpleNamespace(completion_text='{"action":"silent"}')
    plugin.context.llm_generate.side_effect = generate
    await plugin.turn(event.unified_msg_origin)
    assert state["events"][0]["id"] == "late"
    assert state["next_due"] - time.time() <= 30


@pytest.mark.asyncio
async def test_chat_planning_stores_real_preferences_and_project(monkeypatch):
    plugin, state, event = make_plugin()
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    event.get_message_str = lambda: "I prefer stories."
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="provider")
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text=json.dumps({
        "future_task": "write a story", "project_updates": [{"title": "Story", "next_step": "draft"}],
        "note_updates": [{"key": "topic", "value": "stories", "kind": "preference",
                          "evidence": "prefer stories"}]
    }))
    await plugin.plan_from_chat(event, "I could write one.", 0)
    assert state["future_task"] == "write a story"
    assert state["notes"]["topic"]["value"] == "stories"
    assert state["notes"]["topic"]["kind"] == "preference"


@pytest.mark.asyncio
async def test_superseded_chat_planning_does_not_write(monkeypatch):
    plugin, state, event = make_plugin()
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    event.get_message_str = lambda: "old message"
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="provider")
    plugin.revisions[event.unified_msg_origin] = 1
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"future_task":"stale"}')
    await plugin.plan_from_chat(event, "old reply", 0)
    assert state["future_task"] == ""


class PageConfig(dict):
    def save_config(self):
        self.saved = True


def page_settings_groups(page: str) -> str:
    """返回 app.js 里设置分组表的源码片段（分组改名不该让测试失效）。"""
    start = page.index("const groups = [")
    return page[start:page.index("const names = {")]


def test_companion_integration_is_in_page_settings_group():
    page = (Path(__file__).parents[1] / "pages" / "我只在乎你" / "app.js").read_text(encoding="utf-8")
    assert '"companion_integration"' in page_settings_groups(page)
    assert 'names.companion_integration="可选适配我会永远陪着你"' in page


def test_self_scheduled_speak_settings_are_exposed_in_page():
    page = (Path(__file__).parents[1] / "pages" / "我只在乎你" / "app.js").read_text(encoding="utf-8")
    groups = page_settings_groups(page)
    for key in (
        "self_scheduled_speak",
        "next_speak_min_minutes",
        "next_speak_max_minutes",
        "speak_wish_ttl_hours",
    ):
        assert '"' + key + '"' in groups
        assert key + ":" in page
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    for key in (
        "self_scheduled_speak",
        "next_speak_min_minutes",
        "next_speak_max_minutes",
        "speak_wish_ttl_hours",
    ):
        assert key in schema


@pytest.mark.asyncio
async def test_page_config_validates_entire_patch_before_save():
    plugin, _, _ = make_plugin()
    plugin.config = PageConfig(plugin.config)
    page = module.CompanionPageAPI(plugin)
    with pytest.raises(ValueError):
        await page.update_config({"enabled": False, "targets": "123"})
    assert plugin.config["enabled"] is True
    await page.update_config({"targets": ["123"], "platform_id": "test"})
    assert plugin.target_origins() == ["test:FriendMessage:123"]
    assert plugin.config.saved


@pytest.mark.asyncio
async def test_page_config_rejects_out_of_range_speak_window():
    plugin, _, _ = make_plugin()
    plugin.config = PageConfig(plugin.config)
    page = module.CompanionPageAPI(plugin)
    with pytest.raises(ValueError):
        await page.update_config({"next_speak_max_minutes": 5000})
    with pytest.raises(ValueError):
        await page.update_config({"next_speak_min_minutes": 600, "next_speak_max_minutes": 60})
    with pytest.raises(ValueError):
        await page.update_config({"self_scheduled_speak": "yes"})
    await page.update_config(
        {"self_scheduled_speak": False, "next_speak_min_minutes": 10, "next_speak_max_minutes": 720}
    )
    assert plugin.config["self_scheduled_speak"] is False
    assert plugin.config["next_speak_max_minutes"] == 720


@pytest.mark.asyncio
async def test_page_can_edit_plan_and_pause_project():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    await page.perform({"umo": event.unified_msg_origin, "action": "plan", "value": "future work"})
    assert state["future_task"] == "future work"
    state["projects"] = [{"id": "one", "title": "Story", "status": "active"}]
    await page.perform({"umo": event.unified_msg_origin, "action": "project", "id": "one", "status": "paused"})
    assert state["projects"][0]["status"] == "paused"
    with pytest.raises(ValueError):
        await page.perform({"umo": "other:FriendMessage:1", "action": "now"})


@pytest.mark.asyncio
async def test_page_does_not_edit_plan_during_execution():
    plugin, _, event = make_plugin()
    plugin.running[event.unified_msg_origin] = object()
    with pytest.raises(ValueError):
        await module.CompanionPageAPI(plugin).perform({"umo": event.unified_msg_origin, "action": "plan", "value": "race"})


@pytest.mark.asyncio
async def test_native_page_discovery_and_routes():
    from astrbot.dashboard.services.plugin_page_service import PluginPageService

    service = PluginPageService(SimpleNamespace())
    service.resolve_plugin_pages_root = AsyncMock(return_value=Path(__file__).parents[1] / "pages")
    pages = await service.discover_plugin_pages(SimpleNamespace())
    assert [page.name for page in pages] == ["我只在乎你"]
    plugin, _, _ = make_plugin()
    plugin.context.register_web_api = AsyncMock()
    page = module.CompanionPageAPI(plugin)
    # Registration is synchronous in AstrBot.
    routes = []
    plugin.context.register_web_api = lambda *args: routes.append(args)
    page.register()
    assert any(route[0] == "/AutonomousCompanion/page/snapshot" for route in routes)
    assert any(route[0] == "/astrbot_plugin_autonomous_companion/page/config" for route in routes)
    snapshot = await page.snapshot()
    assert snapshot["data"]["targets"] == ["test:FriendMessage:123"]


def attach_companion(plugin, *, allowed=True):
    api = SimpleNamespace(
        bridge_lifecycle_status=lambda: {"active": True},
        prepare_proactive_chat=AsyncMock(return_value={"enabled": True, "allowed": allowed, "token": "reservation" if allowed else "", "prompt_fragment": "current mood and schedule", "reason": "busy"}),
        notify_external_activity_started=lambda *args, **kwargs: {},
        notify_external_activity_ended=lambda *args: True,
        cancel_proactive_chat=AsyncMock(return_value=True),
        notify_proactive_chat_sent=AsyncMock(return_value={"recorded": True}),
        record_external_realtime_continuity=lambda *args, **kwargs: {},
    )
    plugin.context.get_all_stars=lambda: [SimpleNamespace(
        activated=True, root_dir_name="astrbot_plugin_private_companion", name="PrivateCompanion",
        star_cls=SimpleNamespace(extension_api=api),
    )]
    return api


@pytest.mark.asyncio
async def test_companion_context_injected_and_reservation_released():
    plugin, state, event = make_plugin()
    api = attach_companion(plugin)
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    assert "current mood and schedule" in plugin.context.llm_generate.call_args.kwargs["prompt"]
    api.cancel_proactive_chat.assert_awaited_once_with(event.unified_msg_origin, token="reservation")
    assert state["companion_status"] == "已连接陪伴插件"


@pytest.mark.asyncio
async def test_companion_explicit_deferral_is_not_bypassed():
    plugin, state, event = make_plugin()
    attach_companion(plugin, allowed=False)
    decision = await plugin.turn(event.unified_msg_origin)
    assert decision["action"] == "silent"
    plugin.context.llm_generate.assert_not_called()
    assert "暂缓" in state["companion_status"]


@pytest.mark.asyncio
async def test_companion_failure_keeps_standalone_usable():
    plugin, state, event = make_plugin()
    api=attach_companion(plugin)
    api.prepare_proactive_chat.side_effect=RuntimeError("unavailable")
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    plugin.context.llm_generate.assert_awaited_once()
    assert "降级" in state["companion_status"]


@pytest.mark.asyncio
async def test_companion_is_optional_and_disabled_plugin_is_ignored():
    plugin, state, event = make_plugin()
    api=attach_companion(plugin)
    metadata=plugin.context.get_all_stars()[0]
    metadata.activated=False
    plugin.context.get_all_stars=lambda: [metadata]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    api.prepare_proactive_chat.assert_not_called()
    assert state["companion_status"] == "独立运行"


@pytest.mark.asyncio
async def test_companion_preview_does_not_reserve_or_notify():
    plugin, _, event = make_plugin()
    api=attach_companion(plugin)
    api.get_realtime_context=lambda *args, **kwargs: {"prompt":"scene"}
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin, preview=True)
    api.prepare_proactive_chat.assert_not_called()
    api.notify_proactive_chat_sent.assert_not_called()


@pytest.mark.asyncio
async def test_companion_records_delivery_and_cleans_up_on_exception():
    plugin, _, event = make_plugin()
    api=attach_companion(plugin)
    adapter=module.CompanionAdapter(plugin,event)
    await adapter.begin()
    adapter.sent.append("actual dispatched text")
    api.notify_proactive_chat_sent.side_effect=RuntimeError("record error")
    await adapter.finish()
    api.notify_proactive_chat_sent.assert_awaited_once()
    api.cancel_proactive_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_quiet_hours_allow_only_background_work():
    plugin, _, event = make_plugin()
    api = attach_companion(plugin, allowed=False)
    api.prepare_proactive_chat.return_value["reason"] = "private_companion_quiet_hours"
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","notify":true}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"do not send","summary":"drafted"}')
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()
    assert event.quiet


@pytest.mark.asyncio
async def test_page_project_add_delete_and_progress_timestamp():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    await page.perform({"umo": event.unified_msg_origin, "action": "project_add", "title": "Story", "goal": "write"})
    project_id = state["projects"][0]["id"]
    module.apply_plan(state, {"project_updates": [{"id": project_id, "progress": "new draft"}]})
    assert state["projects"][0]["last_progress_at"] > 0
    await page.perform({"umo": event.unified_msg_origin, "action": "project_delete", "id": project_id})
    assert state["projects"] == []


@pytest.mark.asyncio
async def test_decision_detail_and_project_failure_tracking():
    plugin, state, event = make_plugin()
    state["projects"] = [{"id": "story", "title": "Story", "status": "active"}]
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create","reason":"continue story","project_id":"story"}')
    plugin.context.tool_loop_agent.side_effect = RuntimeError("tool failed")
    await plugin.run_due(event.unified_msg_origin)
    assert state["last_turn"]["status"] == "failed"
    assert state["projects"][0]["failure_count"] == 1


@pytest.mark.asyncio
async def test_diagnostics_marks_bad_platform_without_model_call():
    plugin, _, _ = make_plugin()
    plugin.context.get_platform_inst = lambda _: None
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="model")
    plugin.context.conversation_manager.get_conversation = AsyncMock(return_value=None)
    report = (await module.CompanionPageAPI(plugin).diagnostics())["data"]
    assert report["targets"][0]["checks"][0]["status"] == "error"
    plugin.context.llm_generate.assert_not_called()


def test_daily_rollover_and_outbox_dedup():
    from data.plugins.astrbot_plugin_autonomous_companion.daily import retain_outputs, usage_for

    state = {"usage": {"day": "2000-01-01", "tools": 90}}
    assert usage_for(state)["tools"] == 0
    retain_outputs(state, [{"title": "Story", "content": "finished story"}])
    retain_outputs(state, [{"title": "Again", "content": "finished story"}])
    assert len(state["outbox"]) == 1
    assert state["outbox"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_pause_all_and_resume_from_page():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    await page.perform({"umo": event.unified_msg_origin, "action": "pause", "mode": "all", "minutes": 60})
    assert state["pause_until"] > time.time()
    await plugin.run_due(event.unified_msg_origin)
    plugin.prepare.assert_not_called()
    await page.perform({"umo": event.unified_msg_origin, "action": "resume"})
    assert state["pause_until"] == 0


@pytest.mark.asyncio
async def test_quiet_output_retained_and_daily_round_limit():
    plugin, state, event = make_plugin()
    state.update(pause_mode="messages", pause_until=time.time()+100)
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"create"}')
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"finished draft","summary":"done"}')
    await plugin.turn(event.unified_msg_origin)
    # Quiet-turn drafts are kept as a pending message, not fake outbox 成果.
    assert state.get("outbox", []) == []
    assert state["pending_message"]["content"] == "finished draft"
    event.send.assert_not_called()
    assert state["usage"]["rounds"] == 1
    plugin.config["daily_round_limit"] = 1
    await plugin.run_due(event.unified_msg_origin)
    assert state["next_due"] > time.time()+60
    assert plugin.context.tool_loop_agent.await_count == 1


@pytest.mark.asyncio
async def test_outbox_cannot_resend_uncertain_or_paused():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    state["outbox"] = [{"id":"one", "status":"uncertain", "content":"hello"}]
    with pytest.raises(ValueError):
        await page.perform({"umo":event.unified_msg_origin,"action":"outbox","id":"one","operation":"send"})
    state["outbox"][0]["status"] = "pending"
    state["pause_until"] = time.time()+60
    with pytest.raises(ValueError):
        await page.perform({"umo":event.unified_msg_origin,"action":"outbox","id":"one","operation":"send"})


@pytest.mark.asyncio
async def test_daily_tool_budget_blocks_execution():
    from data.plugins.astrbot_plugin_autonomous_companion.daily import usage_for

    plugin, state, event = make_plugin()
    plugin.config["daily_tool_limit"] = 1
    usage_for(state)["tools"] = 1
    event.owner = plugin
    tool = module.BudgetTool(module.FunctionTool(name="test", description="test", parameters={}), event)
    results = [result async for result in tool.call(None)]
    assert "Daily tool budget" in results[0].content[0].text
    assert state["tool_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_manual_outbox_dispatch_tracks_success_or_uncertainty(fails):
    plugin, state, event = make_plugin()
    plugin.deliver = AsyncMock(side_effect=RuntimeError("transport") if fails else None)
    state["outbox"] = [{"id": "output", "content": "finished work", "status": "pending"}]
    page = module.CompanionPageAPI(plugin)
    body = {"umo": event.unified_msg_origin, "action": "outbox", "id": "output", "operation": "send"}
    if fails:
        with pytest.raises(RuntimeError):
            await page.perform(body)
        assert state["outbox"][0]["status"] == "uncertain"
    else:
        await page.perform(body)
        assert state["outbox"][0]["status"] == "sent"
    assert event.unified_msg_origin not in plugin.running
    with pytest.raises(ValueError):
        await page.perform(body)


def test_daily_settings_and_outbox_ui_are_connected():
    root = Path(__file__).parents[1] / "pages" / "我只在乎你"
    html = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    library = (root / "library.js").read_text(encoding="utf-8")
    assert './plans.js' in html and './library.js' in html
    assert 'data-outbox-send' in library and 'data-outbox-retry' in library
    assert '"ignore_budget_while_active","daily_check_limit","daily_round_limit","daily_tool_limit","daily_message_limit"' in app


def test_ui_is_split_into_five_single_purpose_views():
    import re
    root = Path(__file__).parents[1] / "pages" / "我只在乎你"
    html = (root / "index.html").read_text(encoding="utf-8")
    assert html.count('role="tab"') == 5
    for name in ("overview", "plans", "works", "activity", "settings"):
        assert f'id="{name}"' in html
    assert "daily-ui.js" not in html
    assert not (root / "daily-ui.js").exists()
    assert './common.js' in html
    for asset in re.findall(r'src="\./([^"]+)"', html) + re.findall(r'href="\./([^"]+)"', html):
        assert (root / asset).is_file(), asset
    app = (root / "app.js").read_text(encoding="utf-8")
    assert "workspace-content" not in app and "renderDailyAndOutbox" not in app
    assert 'id="works-target"' in html and 'id="plans-target"' in html
    assert 'id="usage"' in html and 'id="overview-target"' in html
    assert "function selectedSession" in app


def test_adaptive_rhythm_bounds_and_disable():
    plugin, state, _ = make_plugin()
    plugin.update_rhythm(state, {"rhythm_minutes": 2, "rhythm_reason": "continue creating"})
    assert state["rhythm_minutes"] == 10
    assert 600 <= plugin.interval(state) <= 1200
    plugin.update_rhythm(state, {"rhythm_minutes": 999, "rhythm_reason": "rest"})
    assert state["rhythm_minutes"] == 180
    plugin.config["adaptive_frequency"] = False
    assert 3000 <= plugin.interval(state) <= 4200


def test_topic_cancellation_cannot_reduce_global_contact_frequency():
    plugin,state,_=make_plugin()
    plugin.update_rhythm(state,{"rhythm_minutes":120,"rhythm_reason":"用户说不用一直画新画，减少主动打扰","rhythm_scope":"topic"})
    assert "rhythm_minutes" not in state
    assert "单一主题" in state["rhythm_ignored_reason"]


def test_pinned_notes_survive_updates_and_are_kept_without_a_cap():
    state = {"notes": {"keep": {"value": "old", "kind": "preference", "confirmed": True,
                                "evidence": "old", "pinned": True, "updated_at": 1}}}
    for index in range(15):
        text = f"new preference {index}"
        module.apply_plan(
            state,
            {"note_updates": [{"key": str(index), "value": text, "kind": "preference", "evidence": text}]},
            user_text=text,
        )
    # 不再有条数上限，固定项也不会被挤掉
    assert len(state["notes"]) == 16
    assert state["notes"]["keep"]["pinned"]
    module.apply_plan(
        state,
        {"note_updates": [{"key": "keep", "value": "changed", "kind": "preference", "evidence": "changed"}]},
        user_text="changed",
    )
    assert state["notes"]["keep"]["value"] == "changed"
    assert state["notes"]["keep"]["pinned"]


def test_note_updates_need_user_evidence_and_keep_previous():
    state = {
        "notes": {
            "语气": {"value": "别用感叹号", "kind": "preference", "confirmed": True,
                     "evidence": "别用感叹号", "created_at": 1, "updated_at": 1}
        }
    }
    module.apply_plan(
        state,
        {
            "note_updates": [
                # 原话里没有这句（是助手或推测），整条丢弃
                {"key": "工作", "value": "在设计公司上班", "kind": "fact",
                 "confirmed": True, "evidence": "她应该在设计公司上班"},
                {"key": "语气", "value": "说话别那么正式", "kind": "preference",
                 "evidence": "说话别那么正式"},
                {"key": "宠物", "value": "养了一只叫团子的橘猫", "kind": "fact",
                 "confirmed": True, "evidence": "我家团子"},
            ]
        },
        user_text="说话别那么正式，我家团子又打翻水了",
    )
    assert set(state["notes"]) == {"语气", "宠物"}
    assert state["notes"]["语气"]["previous"] == "别用感叹号"
    assert state["notes"]["宠物"]["kind"] == "fact"
    assert state["notes"]["宠物"]["source"] == "chat"
    assert state["notes"]["宠物"]["confirmed"] is True


def test_note_kind_defaults_to_fact_and_preference_is_always_confirmed():
    state = {}
    module.apply_plan(
        state,
        {
            "note_updates": [
                {"key": "作息", "value": "十一点后别发消息", "kind": "preference",
                 "evidence": "十一点以后别理我"},
                {"key": "工作", "value": "可能在做设计", "evidence": "又要改稿"},
                {"key": "音乐", "value": "喜欢 city pop", "kind": "interest",
                 "confirmed": True, "evidence": "最近在循环"},
            ]
        },
        user_text="十一点以后别理我，又要改稿，最近在循环 city pop",
    )
    assert state["notes"]["作息"]["kind"] == "preference"
    # 偏好只来自用户明确表达，天然是已确认的
    assert state["notes"]["作息"]["confirmed"] is True
    # 没写 kind 时按事实处理，不会擅自变成约束
    assert state["notes"]["工作"]["kind"] == "fact"
    assert state["notes"]["工作"]["confirmed"] is False
    assert state["notes"]["音乐"]["kind"] == "interest"


def test_note_updates_skip_duplicate_values():
    state = {
        "notes": {
            "语气": {"value": "别用感叹号", "kind": "preference", "confirmed": True,
                     "evidence": "别用感叹号", "updated_at": 1}
        }
    }
    module.apply_plan(
        state,
        {"note_updates": [
            {"key": "风格", "value": "别用感叹号", "kind": "fact", "evidence": "别用感叹号"}
        ]},
        user_text="别用感叹号",
    )
    assert set(state["notes"]) == {"语气"}


def test_note_updates_apply_instead_of_silently_dropping_the_batch():
    state = {}
    module.apply_plan(
        state,
        {
            "future_task": "保留这条计划",
            "note_updates": [
                {"key": "k1", "value": "v1", "evidence": "a1"},
                {"key": "k2", "value": "v2", "evidence": "b2"},
                {"key": "k3", "value": "v3", "evidence": "c3"},
                {"key": "k4", "value": "v4", "evidence": "d4"},
            ],
        },
        user_text="a1 b2 c3 d4",
    )
    assert set(state["notes"]) == {"k1", "k2", "k3", "k4"}
    assert state["future_task"] == "保留这条计划"


def test_notes_have_no_cap_and_the_bot_can_rewrite_or_drop():
    state = {
        "notes": {
            f"k{index}": {
                "value": f"v{index}",
                "kind": "fact",
                "confirmed": index % 2 == 0,
                "updated_at": index,
            }
            for index in range(40)
        }
    }
    module.apply_plan(
        state,
        {
            "note_updates": [
                {"key": "k39", "value": "换工作了，现在在教育行业", "kind": "fact",
                 "confirmed": True, "evidence": "我换工作了"},
                {"key": "k0", "value": "", "evidence": "已经没有猫了", "drop": True},
            ]
        },
        user_text="我换工作了，已经没有猫了",
    )
    notes = state["notes"]
    # 不设条数上限：40 条都在，删掉一条就是 39 条
    assert len(notes) == 39
    assert "k0" not in notes
    assert notes["k39"]["value"] == "换工作了，现在在教育行业"
    assert notes["k39"]["previous"] == "v39"
    assert notes["k39"]["confirmed"] is True


def test_note_drop_needs_user_evidence_and_value_is_required():
    state = {
        "notes": {"宠物": {"value": "养猫", "kind": "fact", "confirmed": True, "updated_at": 1}}
    }
    module.apply_plan(
        state,
        {"note_updates": [{"key": "宠物", "evidence": "助手觉得不该记这条", "drop": True}]},
        user_text="今天好累",
    )
    assert "宠物" in state["notes"]
    with pytest.raises(ValueError):
        module.apply_plan(
            state,
            {"note_updates": [{"key": "居住", "evidence": "今天好累"}]},
            user_text="今天好累",
        )


def test_legacy_preferences_and_impressions_fold_into_one_note_list():
    state = {
        "preferences": {
            "作息": {"value": "十一点后别发消息", "evidence": "十一点以后别理我",
                     "updated_at": 5, "pinned": True}
        },
        "impressions": {
            "宠物": {"value": "养了一只叫团子的橘猫", "kind": "fact", "confirmed": True,
                     "evidence": "我家团子", "updated_at": 6},
            "工作": {"value": "可能在做设计", "kind": "fact", "confirmed": False,
                     "evidence": "又要改稿", "updated_at": 7},
        },
    }
    assert module.migrate_user_notes(state) is True
    assert "preferences" not in state and "impressions" not in state
    assert state["notes"]["作息"]["kind"] == "preference"
    assert state["notes"]["作息"]["confirmed"] is True
    assert state["notes"]["作息"]["pinned"] is True
    assert state["notes"]["宠物"]["kind"] == "fact"
    assert state["notes"]["宠物"]["confirmed"] is True
    assert state["notes"]["工作"]["confirmed"] is False
    # 迁移可重复执行，不会翻倍
    assert module.migrate_user_notes(state) is False
    assert len(state["notes"]) == 3
    # 同名不同来源不互相覆盖
    state["preferences"] = {"宠物": {"value": "用户手动改过", "evidence": "用户手动添加"}}
    assert module.migrate_user_notes(state) is True
    assert len(state["notes"]) == 4
    assert state["notes"]["宠物"]["value"] == "养了一只叫团子的橘猫"


def test_user_notes_context_separates_the_three_groups():
    plugin, state, _ = make_plugin()
    now = time.time()
    state["notes"] = {
        "作息": {"value": "十一点后别发消息", "kind": "preference", "confirmed": True,
                 "evidence": "十一点以后别理我", "updated_at": now},
        "宠物": {"value": "养了一只叫团子的橘猫", "kind": "fact", "confirmed": True,
                 "evidence": "我家团子", "updated_at": now},
        "音乐": {"value": "喜欢 city pop", "kind": "interest", "confirmed": True,
                 "evidence": "最近在循环", "updated_at": now},
        "工作": {"value": "可能在做设计", "kind": "fact", "confirmed": False,
                 "evidence": "又要改稿", "updated_at": now},
    }
    block = plugin.user_notes_context(state)
    preference_part, _, rest = block.partition("已确认的事实与爱好")
    known_part, _, guess_part = rest.partition("还没确认")
    assert "作息（偏好）：十一点后别发消息" in preference_part
    assert "宠物（事实）" in known_part and "音乐（爱好）" in known_part
    assert "可能在做设计" in guess_part
    assert "可能在做设计" not in preference_part + known_part
    assert "必须遵守" in block
    assert "用户现在的说法一律优先于记录" in block
    empty = plugin.user_notes_context({})
    assert "暂无记录" in empty and "不要编造" in empty


def test_user_notes_context_flags_only_stale_unpinned_preferences():
    plugin, state, _ = make_plugin()
    old = time.time() - 200 * 86400
    state["notes"] = {
        "作息": {"value": "十一点后别发消息", "kind": "preference", "confirmed": True, "updated_at": old},
        "称呼": {"value": "叫她小满", "kind": "preference", "confirmed": True, "updated_at": old, "pinned": True},
        "宠物": {"value": "养猫", "kind": "fact", "confirmed": True, "updated_at": old},
    }
    block = plugin.user_notes_context(state)
    assert block.count("天未更新，可能过时") == 1


@pytest.mark.asyncio
async def test_decision_prompt_carries_notes_and_keeps_guesses_out_of_facts():
    plugin, state, event = make_plugin()
    now = time.time()
    state["notes"] = {
        "作息": {"value": "十一点后别发消息", "kind": "preference", "confirmed": True,
                 "evidence": "十一点以后别理我", "updated_at": now},
        "宠物": {"value": "养了一只叫团子的橘猫", "kind": "fact", "confirmed": True,
                 "evidence": "我家团子", "updated_at": now},
        "工作": {"value": "可能在做设计", "kind": "fact", "confirmed": False,
                 "evidence": "又要改稿", "updated_at": now},
    }
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"她在忙"}'
    )
    await plugin.turn(event.unified_msg_origin)
    call = plugin.context.llm_generate.call_args.kwargs
    assert "- 作息（偏好）：十一点后别发消息" in call["prompt"]
    assert "- 宠物（事实）：养了一只叫团子的橘猫" in call["prompt"]
    assert "还没确认" in call["prompt"]
    # 原话不外泄，只留整理后的结论
    assert "我家团子" not in call["prompt"]
    assert "偏好是硬约束" in call["system_prompt"]
    assert "只当猜测" in call["system_prompt"]


def test_chat_planning_prompt_defines_one_note_field_with_three_kinds():
    prompt = module.CHAT_PLANNING_PROMPT
    assert "note_updates" in prompt
    assert "preference_updates" not in prompt and "impression_updates" not in prompt
    for kind in ("preference", "fact", "interest"):
        assert kind in prompt
    assert "偏好是" not in prompt or "preference 是" in prompt
    assert "不限条数" in prompt
    assert "人是会变的" in prompt
    assert '"drop": true' in prompt
    assert "身份证号" in prompt
    assert "每轮最多 3 条改动" in prompt


@pytest.mark.asyncio
async def test_note_page_actions_save_edit_pin_confirm_delete():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    umo = event.unified_msg_origin
    await page.perform({"umo": umo, "action": "note_save", "key": "宠物",
                        "value": "养了一只叫团子的橘猫", "kind": "fact"})
    note = state["notes"]["宠物"]
    assert note["kind"] == "fact" and note["confirmed"] is False
    assert note["source"] == "manual" and note["evidence"] == "用户手动添加"
    await page.perform({"umo": umo, "action": "note_confirm", "key": "宠物", "confirmed": True})
    assert state["notes"]["宠物"]["confirmed"] is True
    assert "confirmed_at" in state["notes"]["宠物"]
    await page.perform({"umo": umo, "action": "note_pin", "key": "宠物", "pinned": True})
    assert state["notes"]["宠物"]["pinned"] is True
    await page.perform({"umo": umo, "action": "note_save", "key": "养宠", "replace_key": "宠物",
                        "value": "养了两只猫", "kind": "fact"})
    assert "宠物" not in state["notes"]
    assert state["notes"]["养宠"]["previous"] == "养了一只叫团子的橘猫"
    # 偏好类型自动视为已确认，且不会被降级成猜测
    await page.perform({"umo": umo, "action": "note_save", "key": "作息",
                        "value": "十一点后别发消息", "kind": "preference", "confirmed": False})
    assert state["notes"]["作息"]["kind"] == "preference"
    assert state["notes"]["作息"]["confirmed"] is True
    with pytest.raises(ValueError):
        await page.perform({"umo": umo, "action": "note_save", "key": "空内容", "value": ""})
    with pytest.raises(ValueError):
        await page.perform({"umo": umo, "action": "note_save", "key": "x", "value": "y", "kind": "mood"})
    with pytest.raises(ValueError):
        await page.perform({"umo": umo, "action": "note_confirm", "key": "不存在", "confirmed": True})
    with pytest.raises(ValueError):
        await page.perform({"umo": umo, "action": "note_confirm", "key": "养宠", "confirmed": "yes"})
    with pytest.raises(ValueError):
        await page.perform({"umo": umo, "action": "note_pin", "key": "养宠", "pinned": "yes"})
    state["manual_plan"] = "keep task"
    state["notes"] = {
        f"k{index}": {"value": f"v{index}", "kind": "fact", "confirmed": True} for index in range(24)
    }
    # 没有条数上限：第 25 条照样能加
    await page.perform({"umo": umo, "action": "note_save", "key": "第二十五条", "value": "也记着"})
    assert len(state["notes"]) == 25
    await page.perform({"umo": umo, "action": "note_delete", "key": "k0"})
    assert "k0" not in state["notes"] and len(state["notes"]) == 24
    assert state["manual_plan"] == "keep task"


@pytest.mark.asyncio
async def test_old_preference_and_impression_actions_still_work_as_aliases():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    umo = event.unified_msg_origin
    await page.perform({"umo": umo, "action": "preference_save", "key": "作息", "value": "十一点后别发消息"})
    assert state["notes"]["作息"]["kind"] == "preference"
    assert state["notes"]["作息"]["confirmed"] is True
    await page.perform({"umo": umo, "action": "preference_pin", "key": "作息", "pinned": True})
    assert state["notes"]["作息"]["pinned"] is True
    await page.perform({"umo": umo, "action": "impression_save", "key": "宠物",
                        "value": "养猫", "kind": "interest"})
    assert state["notes"]["宠物"]["kind"] == "interest"
    await page.perform({"umo": umo, "action": "impression_confirm", "key": "宠物", "confirmed": True})
    assert state["notes"]["宠物"]["confirmed"] is True
    await page.perform({"umo": umo, "action": "impression_delete", "key": "宠物"})
    await page.perform({"umo": umo, "action": "preference_delete", "key": "作息"})
    assert not state["notes"]

def _at(weekday, hour):
    """构造某个星期几的整点时间戳（从周一起算，避开具体日历）。"""
    from datetime import datetime, timedelta

    base = datetime(2026, 11, 2, hour, 30)
    return time.mktime((base + timedelta(days=(weekday - base.weekday()) % 7)).timetuple())


def test_activity_records_by_hour_and_keeps_weekends_separate():
    state = {}
    # 半衰期拉到十年，先把衰减放到一边，只看分桶是否正确
    for _ in range(6):
        module.record_activity(state, _at(0, 21), 3650)
    for _ in range(3):
        module.record_activity(state, _at(5, 23), 3650)
    activity = state["activity"]
    assert activity["samples"] == 9
    assert activity["weekday"][21] == pytest.approx(6, rel=0.01)
    assert activity["weekend"][23] == pytest.approx(3, rel=0.01)
    # 工作日的二十三点不会被周末的发言污染
    assert activity["weekday"][23] == pytest.approx(0, abs=0.001)
    info = module.activity_summary(state, 3650)
    assert info["enough"] is True
    assert "21:00–22:00" in info["active"]


def test_activity_says_nothing_before_enough_samples():
    plugin, state, _ = make_plugin()
    for _ in range(3):
        module.record_activity(state, _at(1, 9), 30)
    info = module.activity_summary(state, 30)
    assert info["enough"] is False
    assert info["active"] == []
    block = plugin.activity_context(state)
    assert "看不出规律" in block
    assert "用户常在线：" not in block
    assert "默认不要主动开口" in block


def test_activity_fades_old_samples_but_keeps_the_shape():
    now = time.time()
    bins = [0.0] * 24
    bins[9] = 10.0
    state = {
        "activity": {
            "weekday": bins,
            "weekend": [0.0] * 24,
            "samples": 10,
            "updated_at": now - 60 * 86400,
        }
    }
    short_half = module.activity_summary(state, 30)
    long_half = module.activity_summary(state, 60)
    # 半衰期 30 天，60 天前的样本只剩四分之一
    assert short_half["hours"][9] == pytest.approx(2.5, rel=0.03)
    assert long_half["hours"][9] == pytest.approx(5.0, rel=0.03)
    assert short_half["active"] == ["09:00–10:00"]


def test_activity_merges_a_range_that_crosses_midnight():
    bins = [0.0] * 24
    for hour in (22, 23, 0, 1):
        bins[hour] = 5.0
    state = {
        "activity": {
            "weekday": bins,
            "weekend": [0.0] * 24,
            "samples": 20,
            "updated_at": time.time(),
        }
    }
    info = module.activity_summary(state, 30)
    assert "22:00–02:00" in info["active"]


def test_activity_does_not_invent_a_weekend_shift_from_two_peaks():
    weekday = [0.0] * 24
    for hour in (8, 12, 21):
        weekday[hour] = 6.0
    weekend = [0.0] * 24
    for hour in (23, 0):
        weekend[hour] = 6.0
    state = {
        "activity": {
            "weekday": weekday,
            "weekend": weekend,
            "samples": 30,
            "updated_at": time.time(),
        }
    }
    info = module.activity_summary(state, 30)
    # 一天分成好几簇时，圆均值会算出没意义的偏移，宁可不提
    assert info["weekend_shift"] == ""


def test_activity_reports_the_shift_when_both_sides_are_tight():
    weekday = [0.0] * 24
    weekday[8] = 20.0
    weekend = [0.0] * 24
    weekend[11] = 20.0
    state = {
        "activity": {
            "weekday": weekday,
            "weekend": weekend,
            "samples": 40,
            "updated_at": time.time(),
        }
    }
    assert module.activity_summary(state, 30)["weekend_shift"] == "周末比工作日晚约 3.0 小时"


def test_activity_keeps_the_longest_quiet_stretches_only():
    bins = [0.0] * 24
    for hour in (9, 13, 21):
        bins[hour] = 10.0
    state = {
        "activity": {
            "weekday": bins,
            "weekend": [0.0] * 24,
            "samples": 40,
            "updated_at": time.time(),
        }
    }
    info = module.activity_summary(state, 30)
    assert info["quiet"] == ["14:00–21:00", "22:00–09:00"]


def test_activity_context_quotes_the_user_words_and_keeps_the_rules():
    plugin, state, _ = make_plugin()
    state["activity"] = {
        "weekday": [0.0] * 24,
        "weekend": [0.0] * 24,
        "samples": 0,
        "updated_at": 0.0,
        "note": "我通常凌晨一点才睡，七点起",
    }
    block = plugin.activity_context(state)
    assert "用户自己说的作息：我通常凌晨一点才睡，七点起" in block
    assert "看不出规律" in block
    assert "不要用作息推断她的心情" in block


@pytest.mark.asyncio
async def test_observe_records_activity_for_each_inbound_message():
    plugin, state, event = make_plugin()
    await plugin.observe(event)
    activity = state["activity"]
    assert activity["samples"] == 1
    stamp = time.localtime(activity["updated_at"])
    bins = activity["weekend"] if stamp.tm_wday >= 5 else activity["weekday"]
    assert bins[stamp.tm_hour] == pytest.approx(1.0)
    assert state["last_inbound"] > 0


@pytest.mark.asyncio
async def test_activity_page_actions_save_note_and_reset_statistics():
    plugin, state, event = make_plugin()
    page = module.CompanionPageAPI(plugin)
    umo = event.unified_msg_origin
    for _ in range(4):
        module.record_activity(state, _at(2, 8), 30)
    await page.perform({"umo": umo, "action": "activity_note", "note": "  我一般八点到公司  "})
    assert state["activity"]["note"] == "我一般八点到公司"
    await page.perform({"umo": umo, "action": "activity_reset"})
    assert state["activity"]["samples"] == 0
    assert state["activity"]["weekday"][8] == 0
    # 清空统计不等于抹掉用户说过的作息
    assert state["activity"]["note"] == "我一般八点到公司"


def test_global_frequency_requires_user_evidence_and_expires():
    plugin,state,_=make_plugin()
    payload={"rhythm_minutes":90,"rhythm_reason":"用户希望少点消息","rhythm_scope":"global","rhythm_evidence":"今天少找我一点"}
    plugin.update_rhythm(state,payload)
    assert "rhythm_minutes" not in state
    plugin.update_rhythm(state,payload,user_text="今天少找我一点，我有事")
    assert state["rhythm_minutes"]==90
    assert state["rhythm_expires_at"]>time.time()
    state["rhythm_expires_at"]=time.time()-1
    assert 3000<=plugin.interval(state)<=4200


@pytest.mark.asyncio
async def test_preview_does_not_adjust_rhythm_but_silent_does():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(completion_text='{"action":"silent","rhythm_minutes":90,"rhythm_reason":"wait for fresh ideas"}')
    await plugin.turn(event.unified_msg_origin, preview=True)
    assert "rhythm_minutes" not in state
    await plugin.turn(event.unified_msg_origin)
    assert state["rhythm_minutes"] == 90
    assert 4800 <= state["next_due"] - time.time() <= 6000


def test_rhythm_preserves_one_shot_and_event_priority():
    plugin, state, _ = make_plugin()
    state["next_due"] = time.time() + 600
    plugin.update_rhythm(state, {"rhythm_minutes":90,"rhythm_reason":"slower","next_check_minutes":10})
    assert state["next_due"] - time.time() < 601
    state["events"] = [{"id":"event"}]
    plugin.update_rhythm(state, {"rhythm_minutes":90,"rhythm_reason":"slower"})
    assert state["next_due"] - time.time() <= 30


def test_speak_commitment_bounds_cancel_and_gate():
    plugin, state, _ = make_plugin()
    plugin.update_speak_commitment(state, {"next_speak_minutes": True})
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_ignored"
    plugin.update_speak_commitment(state, {"next_speak_minutes": 2})
    assert 5 * 60 - 1 < state["next_speak_at"] - time.time() <= 5 * 60
    plugin.update_speak_commitment(state, {"next_speak_minutes": 9999})
    assert state["next_speak_at"] - time.time() <= 1440 * 60
    plugin.update_speak_commitment(
        state, {"next_speak_minutes": 45, "next_speak_reason": "晚饭后想聊聊今天的进展"}
    )
    # 新约定排队，不再顶掉最早那条：45 分钟这条要在队列里，真正唤醒仍看最早。
    scheduled = [
        item
        for item in plugin.speak_schedule(state)
        if item["reason"] == "晚饭后想聊聊今天的进展"
    ]
    assert scheduled and 44 * 60 < scheduled[0]["at"] - time.time() <= 45 * 60
    assert 5 * 60 - 1 < state["next_speak_at"] - time.time() <= 5 * 60
    assert state["journal"][-1]["action"] == "speak_scheduled"
    plugin.update_speak_commitment(state, {"next_speak_minutes": 0})
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_cancelled"
    plugin.config["self_scheduled_speak"] = False
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30})
    assert not state.get("next_speak_at")


def test_speak_commitment_accepts_numeric_strings_from_models():
    plugin, state, _ = make_plugin()
    plugin.update_speak_commitment(state, {"next_speak_minutes": "45"})
    assert 44 * 60 < state["next_speak_at"] - time.time() <= 45 * 60
    plugin.update_speak_commitment(state, {"next_speak_minutes": "0"})
    assert not state.get("next_speak_at")
    plugin.update_speak_commitment(state, {"next_speak_minutes": "soon"})
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_ignored"
    plugin.update_speak_commitment(state, {"next_speak_minutes": float("nan")})
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_ignored"


def test_bad_speak_value_never_breaks_a_round_that_already_produced_a_reply():
    plugin, state, _ = make_plugin()
    # 行动路径在发送消息前写入约定；无法识别的值必须只是忽略，不能抛错。
    plugin.update_speak_commitment(state, {"next_speak_minutes": {"bad": 1}})
    assert not state.get("next_speak_at")
    assert state["speak_ignored_reason"]


def test_speak_commitment_waits_for_pause():
    plugin, state, _ = make_plugin()
    state.update(pause_until=time.time() + 3600, pause_mode="all")
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30, "next_speak_reason": "想分享进展"})
    assert state["next_speak_at"] == state["pause_until"]


def test_wake_at_honors_self_scheduled_opening():
    plugin, state, _ = make_plugin()
    state["next_due"] = time.time() + 3600
    state["next_speak_at"] = time.time() + 600
    assert plugin.wake_at(state) == state["next_speak_at"]
    state["next_speak_at"] = time.time() + 7200
    assert plugin.wake_at(state) == state["next_due"]
    state.pop("next_speak_at")
    assert plugin.wake_at(state) == state["next_due"]


def test_wake_at_never_delays_immediate_wake():
    plugin, state, _ = make_plugin()
    # next_due 0 means "wake now" (立即判断 / page "now"); a pending future
    # opening must not postpone it.
    state["next_due"] = 0
    state["next_speak_at"] = time.time() + 60
    assert plugin.wake_at(state) == 0
    state.pop("next_due")
    assert plugin.wake_at(state) == 0
    state["next_speak_at"] = time.time() - 5
    assert plugin.wake_at(state) == 0


def test_disabled_self_scheduled_speak_drops_pending_commitment():
    plugin, state, _ = make_plugin()
    state["next_speak_at"] = time.time() - 1
    state["next_speak_reason"] = "想聊聊"
    plugin.config["self_scheduled_speak"] = False
    assert plugin.consume_speak_commitment(state) == ""
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_dropped"


def test_consume_speak_commitment_pops_due_only():
    plugin, state, _ = make_plugin()
    assert plugin.consume_speak_commitment(state) == ""
    state["next_speak_at"] = time.time() + 600
    assert plugin.consume_speak_commitment(state) == ""
    assert state["next_speak_at"]
    state["next_speak_at"] = time.time() - 1
    state["next_speak_reason"] = "想分享刚写完的一段"
    reason = plugin.consume_speak_commitment(state)
    assert reason == "想分享刚写完的一段"
    assert not state.get("next_speak_at")
    assert state["last_speak_commitment"]["reason"] == "想分享刚写完的一段"
    assert state["journal"][-1]["action"] == "speak_due"


@pytest.mark.asyncio
async def test_speak_due_round_shows_commitment_in_decision_prompt():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"暂时没有新内容"}'
    )
    await plugin.turn(event.unified_msg_origin, speak_due="想分享刚想到的结尾")
    prompt = plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert "你自己约定的开口时间" in prompt
    assert "想分享刚想到的结尾" in prompt


@pytest.mark.asyncio
async def test_pending_speak_commitment_visible_and_decision_can_reschedule():
    plugin, state, event = make_plugin()
    state["next_speak_at"] = time.time() + 1800
    state["next_speak_reason"] = "睡前道晚安"
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"等到约定时间再说","next_speak_minutes":60,"next_speak_reason":"晚安提前一点"}'
    )
    await plugin.turn(event.unified_msg_origin)
    prompt = plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert "已和自己约定" in prompt
    assert "睡前道晚安" in prompt
    # 新的约定排在原有那条后面，两条都在；唤醒时间仍取最早的"睡前道晚安"。
    reasons = [item["reason"] for item in plugin.speak_schedule(state)]
    assert "睡前道晚安" in reasons and "晚安提前一点" in reasons
    rescheduled = next(
        item for item in plugin.speak_schedule(state) if item["reason"] == "晚安提前一点"
    )
    assert 59 * 60 < rescheduled["at"] - time.time() <= 60 * 60
    assert state["next_speak_reason"] == "睡前道晚安"


@pytest.mark.asyncio
async def test_action_result_can_schedule_next_speak():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"create","task":"写一段短文","reason":"有想写的内容"}'
    )
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text='{"message":"跟你说个刚想到的点子","summary":"分享了点子","next_speak_minutes":120,"next_speak_reason":"两小时后说读后感"}'
    )
    await plugin.turn(event.unified_msg_origin)
    assert 119 * 60 < state["next_speak_at"] - time.time() <= 120 * 60
    assert state["next_speak_reason"] == "两小时后说读后感"


@pytest.mark.asyncio
async def test_preview_does_not_store_speak_commitment():
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"preview only","next_speak_minutes":30}'
    )
    await plugin.turn(event.unified_msg_origin, preview=True)
    assert not state.get("next_speak_at")
    await plugin.turn(event.unified_msg_origin)
    assert state["next_speak_at"] > time.time()


@pytest.mark.asyncio
async def test_run_due_defers_and_drops_speak_commitment():
    plugin, state, _ = make_plugin()
    umo = "test:FriendMessage:123"
    state.update(
        pause_until=time.time() + 3600,
        pause_mode="all",
        next_due=time.time() + 3600,
    )
    state["next_speak_at"] = time.time() + 60
    await plugin.run_due(umo)
    assert state["next_speak_at"] == state["pause_until"]
    state.update(pause_until=0, pause_mode="", next_due=time.time() + 60)
    state["usage"] = {"day": time.strftime("%Y-%m-%d"), "checks": 999}
    state["next_speak_at"] = time.time() + 60
    state["next_speak_reason"] = "想聊聊"
    await plugin.run_due(umo)
    assert not state.get("next_speak_at")
    assert state["journal"][-1]["action"] == "speak_dropped"


@pytest.mark.asyncio
async def test_run_due_fires_committed_opening_and_actually_speaks():
    """到点真正唤醒并开口：约定被消耗、提示词带标记、消息真的发出。"""
    plugin, state, event = make_plugin()
    state["next_due"] = time.time() + 3600
    state["next_speak_at"] = time.time() - 1
    state["next_speak_reason"] = "想聊聊今天的小事"
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"chat","task":"说说今天的小事","reason":"到点开口"}'
    )
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text='{"message":"刚才突然想起一件小事，想说说。","summary":"到点说了句话"}'
    )
    await plugin.run_due(event.unified_msg_origin)
    assert not state.get("next_speak_at")
    assert state["last_turn"]["trigger"] == "speak"
    prompt = plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert "已到你自己约定的开口时间" in prompt
    assert "想聊聊今天的小事" in prompt
    texts = [
        "".join(getattr(part, "text", "") for part in call.args[0].chain)
        for call in event.send.await_args_list
    ]
    assert any("刚才突然想起一件小事" in text for text in texts)


@pytest.mark.asyncio
async def test_speak_wish_is_kept_across_rounds_and_shown_to_her():
    """她想说却没说出口的话要跨轮保留，并在下一轮提示词里让她自己看到。"""
    plugin, state, event = make_plugin()
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"还没到时候","speak_wish":"想问问她面试结果，别显得催"}'
    )
    await plugin.turn(event.unified_msg_origin)
    assert state["speak_wish"] == "想问问她面试结果，别显得催"
    assert state["speak_wish_at"] <= time.time()
    assert any(item["action"] == "wish_kept" for item in state["journal"])
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"还是先不说"}'
    )
    await plugin.turn(event.unified_msg_origin)
    prompt = plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert "你心里存着一句还没说出口的话" in prompt
    assert "面试结果" in prompt
    assert state["speak_wish"] == "想问问她面试结果，别显得催"


def test_speak_wish_repeat_counts_shelvings_and_replaces_on_new_note():
    plugin, state, _ = make_plugin()
    plugin.update_speak_wish(state, {"speak_wish": "想聊聊那本书的结尾"})
    first = state["speak_wish_at"]
    # 同一轮内决策与行动都复述同一句，不该重复计数
    plugin.update_speak_wish(state, {"speak_wish": "想聊聊那本书的结尾"})
    assert state["speak_wish_kept"] == 0
    # 到了下一轮还惦记着，才算搁置一轮
    state["speak_wish_kept_at"] = time.time() - 120
    plugin.update_speak_wish(state, {"speak_wish": "想聊聊那本书的结尾"})
    assert state["speak_wish_kept"] == 1
    assert state["speak_wish_at"] == first
    plugin.update_speak_wish(state, {"speak_wish": "换成想问问她今天累不累"})
    assert state["speak_wish"] == "换成想问问她今天累不累"
    assert state["speak_wish_kept"] == 0


def test_speak_wish_can_be_dropped_expired_and_gated():
    plugin, state, _ = make_plugin()
    plugin.update_speak_wish(state, {"speak_wish": "想说的话"})
    plugin.update_speak_wish(state, {"speak_wish_clear": True})
    assert not state.get("speak_wish")
    assert state["journal"][-1]["action"] == "wish_cleared"
    plugin.update_speak_wish(state, {"speak_wish": "还惦记着一句晚安"})
    state["speak_wish_at"] = time.time() - 25 * 3600
    plugin.expire_speak_wish(state)
    assert not state.get("speak_wish")
    assert state["journal"][-1]["action"] == "wish_faded"
    plugin.update_speak_wish(state, {"speak_wish": "新念头"})
    plugin.config["self_scheduled_speak"] = False
    plugin.update_speak_wish(state, {"speak_wish": "关掉后不该记"})
    assert state["speak_wish"] == "新念头"


@pytest.mark.asyncio
async def test_committed_opening_releases_the_held_note():
    """到点真的说了话，心里那句话就算说完了，不能继续挂着提醒她。"""
    plugin, state, event = make_plugin()
    state.update(
        next_due=time.time() + 3600,
        next_speak_at=time.time() - 1,
        speak_wish="想跟她说说今天的小事",
        speak_wish_at=time.time() - 600,
    )
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"chat","task":"说说今天的小事","reason":"到点开口"}'
    )
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(
        completion_text='{"message":"今天有件小事想跟你说。","summary":"说了"}'
    )
    await plugin.run_due(event.unified_msg_origin)
    assert not state.get("speak_wish")
    assert any(item["action"] == "wish_spoken" for item in state["journal"])


@pytest.mark.asyncio
async def test_opening_without_reason_still_counts_as_her_own_commitment():
    """只约了时间没写原因时，这一轮仍是"到点开口"，不能被当成普通唤醒。"""
    plugin, state, event = make_plugin()
    state.update(next_due=time.time() + 3600, next_speak_at=time.time() - 1)
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"暂时没话"}'
    )
    await plugin.run_due(event.unified_msg_origin)
    prompt = plugin.context.llm_generate.call_args.kwargs["prompt"]
    assert "已到你自己约定的开口时间" in prompt
    assert state["last_turn"]["trigger"] == "speak"


@pytest.mark.asyncio
async def test_silent_round_keeps_the_held_note():
    plugin, state, event = make_plugin()
    state.update(
        next_due=time.time() + 3600,
        next_speak_at=time.time() - 1,
        speak_wish="想跟她说说今天的小事",
        speak_wish_at=time.time() - 60,
    )
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text='{"action":"silent","reason":"她好像很忙，先不说"}'
    )
    await plugin.run_due(event.unified_msg_origin)
    assert state["speak_wish"] == "想跟她说说今天的小事"


@pytest.mark.asyncio
async def test_plain_decision_is_repaired_before_execution():
    plugin, _, event = make_plugin()
    plugin.context.llm_generate.side_effect = [
        SimpleNamespace(completion_text="我想继续写昨天的故事。"),
        SimpleNamespace(completion_text='{"action":"create","task":"continue story","reason":"unfinished"}'),
    ]
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"a story","summary":"written"}')
    await plugin.turn(event.unified_msg_origin)
    assert plugin.context.tool_loop_agent.await_count == 1
    assert event.send.await_count == 1


@pytest.mark.asyncio
async def test_completed_task_entry_is_removed_before_now_action():
    plugin, _, event = make_plugin()
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    plugin.running[event.unified_msg_origin] = task
    result = await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"now"})
    assert result["status"] == "ok"
    assert event.unified_msg_origin not in plugin.running


@pytest.mark.asyncio
async def test_manual_plan_survives_automatic_plan_replacement():
    plugin, state, event = make_plugin()
    await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"plan","value":"write my story"})
    module.apply_plan(state, {"future_task":""})
    assert state["future_task"] == "write my story"
    assert state["manual_plan"] == "write my story"


@pytest.mark.asyncio
async def test_manual_task_bypasses_discretionary_silence():
    plugin, state, event = make_plugin()
    state["manual_plan"] = "send a new thought"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"new thought","summary":"sent","manual_plan_completed":true}')
    await plugin.turn(event.unified_msg_origin)
    plugin.context.llm_generate.assert_not_called()
    plugin.context.tool_loop_agent.assert_awaited_once()
    assert not state["manual_plan"]
    event.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_manual_task_is_retained():
    plugin, state, event = make_plugin()
    state["manual_plan"] = "draw a picture"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"","summary":"unavailable","blocked_reason":"image tool disabled","manual_plan_completed":true}')
    await plugin.turn(event.unified_msg_origin)
    assert state["manual_plan"] == "draw a picture"
    assert state["last_turn"]["status"] == "blocked"
    assert "image tool disabled" in state["plan_status"]


@pytest.mark.asyncio
async def test_explicit_continuous_manual_task_schedules_when_model_omits_delay():
    plugin, state, event = make_plugin()
    state["manual_plan"] = "连续主动聊天"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"a new thought","summary":"one message"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["next_task"]
    assert 85 <= state["next_due"] - time.time() <= 90
    await plugin.turn(event.unified_msg_origin)
    assert state["rounds"] == 2
    assert event.send.await_count == 2
    assert "第 3 轮" in state["continuation_status"]


@pytest.mark.asyncio
async def test_tool_notice_sent_without_arguments_or_raw_result():
    plugin, _, event = make_plugin()
    plugin.config["show_tool_calls"] = True
    plugin.context.get_config=lambda *_: {"provider_settings":{"show_tool_use_status":True}}
    plugin.deliver = AsyncMock()
    await plugin.tool_notice(event, "llm_poke_user", "开始调用")
    plugin.deliver.assert_awaited_once()
    event.quiet = True
    await plugin.tool_notice(event, "llm_poke_user", "已返回")
    assert plugin.deliver.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("show_call,show_result,expected",[(False,False,0),(False,True,0),(True,False,1),(True,True,2)])
async def test_native_astrbot_tool_visibility_switch_matrix(show_call,show_result,expected):
    plugin,_,event=make_plugin()
    plugin.context.get_config=lambda *_:{"provider_settings":{"show_tool_use_status":show_call,"show_tool_call_result":show_result}}
    plugin.deliver=AsyncMock()
    await plugin.tool_notice(event,"port_check","开始调用")
    await plugin.tool_notice(event,"port_check","返回结果","x"*200)
    assert plugin.deliver.await_count==expected
    if show_call and show_result:
        chain=plugin.deliver.call_args.args[1]
        text=chain.get_plain_text()
        assert text=="🔨 调用工具: port_check\n📎 返回结果: "+"x"*67+"..."


@pytest.mark.asyncio
async def test_service_rechecked_each_round_and_change_visible(monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import service_check

    plugin, state, _ = make_plugin()
    state["next_task"] = "等待 ComfyUI 启动后补画"
    probe = AsyncMock(side_effect=[
        {"status":"unknown","detail":"connection refused","checked_at":1},
        {"status":"ready","detail":"responded","checked_at":2},
    ])
    monkeypatch.setattr(service_check, "probe_comfyui", probe)
    first = await service_check.refresh_services(plugin,state,[])
    second = await service_check.refresh_services(plugin,state,[])
    assert "unknown" in first and "ready" in second
    assert state["comfyui_observation"]["changed"]
    assert probe.await_count == 2
    assert "不要再催用户开启" in second


@pytest.mark.asyncio
async def test_unrelated_task_does_not_probe(monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import service_check

    plugin,state,_ = make_plugin()
    probe = AsyncMock()
    monkeypatch.setattr(service_check,"probe_comfyui",probe)
    await service_check.refresh_services(plugin,state,[])
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_service_address_is_unknown():
    from data.plugins.astrbot_plugin_autonomous_companion.service_check import probe_comfyui

    result = await probe_comfyui("file:///bad")
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_generic_file_condition_detects_change_without_reading_content(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.conditions import refresh_conditions

    plugin,state,event=make_plugin()
    path=tmp_path / "output.txt"
    plugin.config["condition_file_roots"]=[str(tmp_path)]
    state["conditions"]=[{"id":"output","kind":"file","path":str(path)}]
    await refresh_conditions(plugin,event,{})
    assert state["condition_observations"][0]["detail"]["exists"] is False
    path.write_text("actual output",encoding="utf-8")
    await refresh_conditions(plugin,event,{})
    observation=state["condition_observations"][0]
    assert observation["changed"]
    assert observation["detail"]["exists"]
    assert "actual output" not in json.dumps(observation)


@pytest.mark.asyncio
async def test_unapproved_tool_condition_never_executes():
    from data.plugins.astrbot_plugin_autonomous_companion.conditions import refresh_conditions

    plugin,state,event=make_plugin()
    plugin.tools=lambda *_: pytest.fail("must not resolve unapproved tool")
    state["conditions"]=[{"id":"job","kind":"tool","tool":"generate_image","args":{"job_id":"real-id"}}]
    await refresh_conditions(plugin,event,{})
    assert state["condition_observations"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_file_outside_roots_is_unknown(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.conditions import refresh_conditions

    plugin,state,event=make_plugin()
    state["conditions"]=[{"id":"file","kind":"file","path":str(tmp_path / "missing")}]
    await refresh_conditions(plugin,event,{})
    assert state["condition_observations"][0]["status"] == "unknown"


def test_conditions_are_persisted_and_validation_is_atomic():
    state={}
    module.apply_plan(state,{"conditions":[{"id":"job","kind":"http","url":"http://127.0.0.1:1234/tasks/id"}]})
    assert state["conditions"][0]["id"]=="job"
    with pytest.raises(ValueError):
        module.apply_plan(state,{"future_task":"bad","conditions":[{"kind":"submit"}]})
    assert "future_task" not in state


def test_repeated_failure_cooldown_and_evidence_sources():
    from data.plugins.astrbot_plugin_autonomous_companion.lifecycle import note_failure, record_evidence

    state = {}
    for _ in range(3):
        failure = note_failure(state, "image:job", "service unavailable")
    assert failure["count"] == 3
    assert failure["until"] > time.time()
    different = note_failure(state, "image:job", "different error")
    assert different["count"] == 1
    record_evidence(state,"model_report","saved work")
    record_evidence(state,"work_saved","actual file receipt")
    assert state["evidence"][0]["source"] != state["evidence"][1]["source"]


def test_appointment_evidence_and_resolution_stop_reminders():
    from data.plugins.astrbot_plugin_autonomous_companion.lifecycle import due_appointments

    state = {}
    module.apply_plan(state,{"appointment_updates":[{"title":"面试","due_at":time.time()+1200,"evidence":"明天面试"}]},user_text="我明天面试")
    assert len(due_appointments(state)) == 1
    identifier = state["appointments"][0]["id"]
    module.apply_plan(state,{"appointment_updates":[{"id":identifier,"status":"resolved","result":"通过了","evidence":"面试通过了"}]},user_text="面试通过了")
    assert not due_appointments(state)
    assert state["appointments"][0]["result"] == "通过了"


def test_appointment_does_not_invent_user_evidence():
    state = {}
    module.apply_plan(state,{"appointment_updates":[{"title":"考试","due_at":time.time()+1000,"evidence":"明天考试"}]},user_text="你好")
    assert state["appointments"] == []


@pytest.mark.asyncio
async def test_three_real_tool_failures_block_identical_call():
    from data.plugins.astrbot_plugin_autonomous_companion.lifecycle import note_failure, tool_key

    plugin,state,event = make_plugin()
    event.owner = plugin
    key = tool_key("test",{})
    for _ in range(3):
        note_failure(state,key,"same failure")
    tool = module.BudgetTool(module.FunctionTool(name="test",description="test",parameters={}),event)
    results = [result async for result in tool.call(None)]
    assert results[0].isError
    assert state["tool_calls"] == 0


@pytest.mark.asyncio
async def test_null_optional_action_fields_do_not_break_continuation():
    plugin,state,event = make_plugin()
    state["manual_plan"] = "连续主动聊天"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"hello","summary":null,"next_task":null,"blocked_reason":null,"continue_after_seconds":null}')
    await plugin.turn(event.unified_msg_origin)
    assert state["next_task"]
    assert state["next_due"] > time.time()
    event.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_nontext_next_task_is_validated_before_strip():
    plugin,state,event = make_plugin()
    state["manual_plan"] = "连续主动聊天"
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"hello","next_task":123}')
    with pytest.raises(ValueError, match="next_task"):
        await plugin.turn(event.unified_msg_origin)


@pytest.mark.asyncio
async def test_template_mcp_recovers_only_configured_enabled_service():
    from data.plugins.astrbot_plugin_autonomous_companion.mcp_recovery import recover_template_tools

    plugin,state,_=make_plugin()
    state["comfyui_observation"]={"status":"ready"}
    tools=[]
    async def enable(*args,**kwargs):
        tools.extend(SimpleNamespace(name=name,active=True) for name in ("list_templates","get_template","run_template"))
    manager=SimpleNamespace(get_full_tool_set=lambda:SimpleNamespace(tools=tools),
                            load_mcp_config=lambda:{"mcpServers":{"ComfyUI生图":{"active":True}}},
                            mcp_client_dict={},enable_mcp_server=AsyncMock(side_effect=enable),disable_mcp_server=AsyncMock())
    plugin.context.get_llm_tool_manager=lambda:manager
    assert "工具齐全" in await recover_template_tools(plugin,state)
    await recover_template_tools(plugin,state)
    manager.enable_mcp_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_template_mcp_does_not_enable_disabled_server():
    from data.plugins.astrbot_plugin_autonomous_companion.mcp_recovery import recover_template_tools

    plugin,state,_=make_plugin()
    state["comfyui_observation"]={"status":"ready"}
    manager=SimpleNamespace(get_full_tool_set=lambda:SimpleNamespace(tools=[]),
        load_mcp_config=lambda:{"mcpServers":{"ComfyUI生图":{"active":False}}},enable_mcp_server=AsyncMock())
    plugin.context.get_llm_tool_manager=lambda:manager
    assert "已停用" in await recover_template_tools(plugin,state)
    manager.enable_mcp_server.assert_not_called()


@pytest.mark.asyncio
async def test_any_user_message_prioritizes_chat_without_deleting_task():
    plugin,state,event=make_plugin()
    event.get_extra=lambda _:False
    event.get_sender_name=lambda:"user"
    event.get_self_id=lambda:"bot"
    event.get_message_str=lambda:"先别弄了"
    state.update(manual_plan="连续主动",next_task="continue",window_end=time.time()+900)
    await plugin.observe(event)
    assert state["manual_plan"] == "连续主动"
    assert not state["next_task"]
    assert not state.get("pause_until")
    assert state["next_due"] > time.time()


@pytest.mark.asyncio
async def test_input_status_notice_does_not_cancel_proactive():
    plugin,state,event=make_plugin()
    event.message_obj=SimpleNamespace(raw_message={"post_type":"notice","notice_type":"notify","sub_type":"input_status"})
    state.update(next_task="continue",window_end=time.time()+900)
    await plugin.observe(event)
    assert state["next_task"]=="continue"
    assert not plugin.revisions


@pytest.mark.asyncio
async def test_cancel_temporary_task_clears_resume_state():
    plugin,state,event=make_plugin()
    state.update(manual_plan="temporary",future_task="temporary",next_task="continue",execution_checkpoint={"pending":True},window_end=time.time()+900)
    await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"cancel_plan"})
    assert not state["manual_plan"] and not state["next_task"]
    assert "execution_checkpoint" not in state


@pytest.mark.asyncio
async def test_deleted_project_is_not_recreated_by_old_model_context():
    plugin,state,event=make_plugin()
    state["projects"]=[{"id":"old","title":"旧项目","status":"active"}]
    await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"project_delete","id":"old"})
    module.apply_plan(state,{"project_updates":[{"title":"旧项目","goal":"again"}]})
    assert not state["projects"]


def test_cancelled_task_cannot_reappear_in_future_plan():
    from data.plugins.astrbot_plugin_autonomous_companion.cancellations import remember_cancellation,clear_cancelled_work
    state={"future_task":"服务恢复后给白画张新画","execution_checkpoint":{"task":"给白画张新画"}}
    remember_cancellation(state,["给白画张新画"])
    clear_cancelled_work(state)
    assert not state["future_task"]
    assert "execution_checkpoint" not in state
    module.apply_plan(state,{"future_task":"服务恢复后给白画张新画"})
    assert not state["future_task"]


def test_cancelling_one_drawing_does_not_cancel_all_future_art():
    from data.plugins.astrbot_plugin_autonomous_companion.cancellations import related
    assert not related("给白画张新画","给白画一张不同风景")


def test_missing_tool_weather_query_is_not_automatically_verified():
    from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import execution_verdict
    event=SimpleNamespace(action_receipts=[],execution_contract={"mode":"action","steps":[{"goal":"查询天气","tools":[],"accept":"observed","kind":"external"}]})
    assert execution_verdict(event,"contract")[0]=="unexecuted"


@pytest.mark.asyncio
async def test_outbox_snapshot_preserves_file_when_original_changes(tmp_path,monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import outbox
    monkeypatch.setattr(outbox.StarTools,"get_data_dir",lambda _:tmp_path/"store")
    original=tmp_path/"code.py";original.write_text("original",encoding="utf-8")
    state={"work_library":[{"id":"code","kind":"code","title":"code","path":str(original)}]}
    outbox.enqueue_work(state,"code")
    original.write_text("changed",encoding="utf-8")
    chain=await outbox.output_chain(SimpleNamespace(config={}),state,state["outbox"][0])
    assert Path(chain.chain[0].file).read_text(encoding="utf-8")=="original"


@pytest.mark.asyncio
async def test_long_synced_novel_exports_complete_file(tmp_path,monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import outbox
    monkeypatch.setattr(outbox.StarTools,"get_data_dir",lambda _:tmp_path)
    body="正文"*5000
    state={"work_library":[{"id":"novel","kind":"text","title":"novel","inline_content":body}]}
    outbox.enqueue_work(state,"novel")
    chain=await outbox.output_chain(SimpleNamespace(config={}),state,state["outbox"][0])
    assert Path(chain.chain[0].file).read_text(encoding="utf-8")==body


def test_checkpoint_selection_rotates_between_pending_tasks():
    from data.plugins.astrbot_plugin_autonomous_companion.checkpoint import next_checkpoint
    state={"execution_checkpoints":{"a":{"task":"one","cid":"cid","pending":True,"updated_at":1},"b":{"task":"two","cid":"cid","pending":True,"updated_at":2}}}
    assert next_checkpoint(state,"cid")["task"]=="one"
    state["execution_checkpoints"]["a"]["next_attempt_at"]=time.time()+100
    assert next_checkpoint(state,"cid")["task"]=="two"


@pytest.mark.asyncio
async def test_cancelled_task_cannot_be_saved_as_memo():
    from data.plugins.astrbot_plugin_autonomous_companion.cancellations import remember_cancellation
    plugin,state,event=make_plugin()
    event.owner=plugin
    remember_cancellation(state,["给白画张新画"])
    tool=module.BudgetTool(module.FunctionTool(name="pc_manage_memo",description="memo",parameters={}),event)
    results=[item async for item in tool.call(None,content="恢复后给白画张新画")]
    assert results[0].isError
    assert state["tool_calls"]==0


def test_dialogue_step_does_not_require_fake_tool_receipt():
    from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import execution_verdict
    event=SimpleNamespace(action_receipts=[{"tool":"memo","status":"success"}],execution_contract={"mode":"action","steps":[
        {"goal":"记录","tools":["memo"],"accept":"success"},
        {"goal":"向用户打招呼","tools":[],"accept":"observed","kind":"reply"}]})
    assert execution_verdict(event,"contract")[0]=="verified"


def test_embedded_page_uses_sandbox_compatible_dialogs():
    import re
    root=Path(__file__).parents[1]/"pages"/"我只在乎你"
    assert './dialogs.js' in (root/"index.html").read_text(encoding="utf-8")
    for name in ("app.js","plans.js","library.js"):
        script=(root/name).read_text(encoding="utf-8")
        assert not re.search(r"\b(?:confirm|prompt)\s*\(",script)
    assert 'await pageConfirm("删除此项目及其进展记录？")' in (root/"plans.js").read_text(encoding="utf-8")


def test_library_backfills_real_receipts_and_text_read(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.library import backfill_library,read_library_text
    text=tmp_path/"novel.md"
    text.write_text("实际小说正文",encoding="utf-8")
    state={"journal":[{"action":"tool_result","summary":"safe_write: "+json.dumps({"ok":True,"data":{"ok":True,"file":str(text)}})}],
        "last_turn":{"tool_receipts":[{"tool":"run_template","result":json.dumps({"status":"completed","outputs":{"one":{"type":"image","url":"http://localhost/view?filename=new.png"}}})}]}}
    assert backfill_library(state)==2
    assert backfill_library(state)==0
    work=next(w for w in state["work_library"] if w["kind"]=="text")
    assert read_library_text(state,work["id"])=="实际小说正文"
    with pytest.raises(ValueError):
        read_library_text(state,"not-an-indexed-path")


@pytest.mark.asyncio
async def test_real_library_work_can_be_added_to_outbox(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.library import catalogue_receipt
    plugin,state,event=make_plugin()
    file=tmp_path/"story.txt"
    file.write_text("完整作品",encoding="utf-8")
    catalogue_receipt(state,"safe_write",json.dumps({"ok":True,"file":str(file)}))
    result=await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"work_to_outbox","id":state["work_library"][0]["id"]})
    assert result["status"]=="ok"
    assert state["outbox"][0]["work_id"]==state["work_library"][0]["id"]


@pytest.mark.asyncio
async def test_reference_outbox_sends_code_as_file_and_deduplicates(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.outbox import enqueue_work,output_chain
    from data.plugins.astrbot_plugin_autonomous_companion.library import register_local_work
    plugin,state,_=make_plugin()
    path=tmp_path/"hello.py";path.write_text("print('hello')",encoding="utf-8")
    register_local_work(state,str(path),"safe_write")
    identifier=state["work_library"][0]["id"]
    enqueue_work(state,identifier);enqueue_work(state,identifier)
    assert len(state["outbox"])==1
    chain=await output_chain(plugin,state,state["outbox"][0])
    assert type(chain.chain[0]).__name__=="File"
    assert chain.chain[0].name=="hello.py"


@pytest.mark.asyncio
async def test_reference_outbox_fails_if_source_disappears():
    from data.plugins.astrbot_plugin_autonomous_companion.outbox import output_chain
    plugin,state,_=make_plugin()
    with pytest.raises(ValueError,match="原作品"):
        await output_chain(plugin,state,{"work_id":"missing"})


@pytest.mark.asyncio
async def test_gallery_reads_cached_image_through_indexed_id(tmp_path,monkeypatch):
    import hashlib
    from data.plugins.astrbot_plugin_autonomous_companion import library
    monkeypatch.setattr(library.StarTools,"get_data_dir",lambda _:tmp_path)
    url="http://127.0.0.1:8188/view?filename=picture.png&type=output"
    cache=tmp_path/"gallery"/(hashlib.sha256(url.encode()).hexdigest()+".bin")
    cache.parent.mkdir()
    cache.write_bytes(b"\x89PNG\r\n\x1a\nactual test image bytes")
    state={"work_library":[{"id":"one","kind":"image","url":url}]}
    result=await library.read_library_image(state,"one","http://127.0.0.1:8188")
    assert result.startswith("data:image/png;base64,")
    with pytest.raises(ValueError):
        await library.read_library_image(state,"one","http://different-server:8188")


@pytest.mark.asyncio
async def test_text_discovery_runs_without_manual_rescan(tmp_path,monkeypatch):
    import hashlib
    from data.plugins.astrbot_plugin_autonomous_companion import library
    plugin,state,event=make_plugin()
    scope=hashlib.sha256(event.unified_msg_origin.encode()).hexdigest()[:16]
    root=tmp_path/"works"/scope
    root.mkdir(parents=True)
    (root/"novel.md").write_text("小说完整正文",encoding="utf-8")
    monkeypatch.setattr(library.StarTools,"get_data_dir",lambda _:tmp_path)
    plugin.context.get_platform_inst=lambda _:None
    plugin.context.conversation_manager.get_conversation=AsyncMock(return_value=None)
    assert await library.discover_text_works(plugin,event.unified_msg_origin)==1
    assert library.read_library_text(state,state["work_library"][0]["id"])=="小说完整正文"


@pytest.mark.asyncio
async def test_legacy_work_root_is_found_when_persona_name_changes(tmp_path,monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import library
    plugin,state,event=make_plugin()
    legacy=tmp_path/"old-persona"/"works";legacy.mkdir(parents=True)
    (legacy/"第三层蓝.md").write_text("真实正文",encoding="utf-8")
    plugin.config["work_library_roots"]=[str(legacy)]
    monkeypatch.setattr(library.StarTools,"get_data_dir",lambda _:tmp_path/"plugin")
    plugin.context.get_platform_inst=lambda _:SimpleNamespace(meta=lambda:SimpleNamespace(name="test"))
    plugin.context.conversation_manager.get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id=None))
    plugin.context.persona_manager=SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("白香夜梨",{},None,False)))
    assert await library.discover_text_works(plugin,event.unified_msg_origin)==1
    assert state["work_library"][0]["kind"]=="text"
    assert library.read_library_text(state,state["work_library"][0]["id"])=="真实正文"


@pytest.mark.asyncio
async def test_companion_bookshelf_sync_displays_body_without_changing_host():
    from data.plugins.astrbot_plugin_autonomous_companion.companion_library import sync_companion_library
    from data.plugins.astrbot_plugin_autonomous_companion.library import read_library_text
    plugin,state,event=make_plugin()
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    plugin.context.get_platform_inst=lambda _:SimpleNamespace(meta=lambda:PlatformMetadata(name="aiocqhttp",description="test",id="test"),client_self_id="bot")
    api=attach_companion(plugin)
    api.export_text_work_library=AsyncMock(return_value={"available":True,"works":[{"id":"novel","title":"小说","content":"真正正文","status":"finished","updated_at":123}]})
    assert await sync_companion_library(plugin,event.unified_msg_origin)==1
    work=state["work_library"][0]
    assert work["producer"]=="private_companion"
    assert read_library_text(state,work["id"])=="真正正文"
    await sync_companion_library(plugin,event.unified_msg_origin)
    assert len(state["work_library"])==1


def test_general_library_indexes_script_image_and_program_bundle(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.library import catalogue_receipt,read_library_text,download_library_file
    script=tmp_path/"tool.py";script.write_text("print('hello')",encoding="utf-8")
    image=tmp_path/"art.png";image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    bundle=tmp_path/"program.zip";bundle.write_bytes(b"PK-test")
    state={}
    assert catalogue_receipt(state,"some_generator",json.dumps({"success":True,"files":[str(script),str(image),str(bundle)]}))==3
    assert {w["kind"] for w in state["work_library"]}=={"code","image","file"}
    code=next(w for w in state["work_library"] if w["kind"]=="code")
    assert read_library_text(state,code["id"])=="print('hello')"
    assert download_library_file(state,code["id"])["filename"]=="tool.py"


def test_code_executor_saved_path_is_indexed_only_if_actual_file_exists(tmp_path):
    from data.plugins.astrbot_plugin_autonomous_companion.library import catalogue_receipt
    file=tmp_path/"script.js";file.write_text("hello",encoding="utf-8")
    state={}
    assert catalogue_receipt(state,"execute_python_code",f"saved: {file} 5\n")==1
    assert catalogue_receipt(state,"execute_python_code",f"saved: {tmp_path/'missing.py'} 5\n")==0


@pytest.mark.asyncio
async def test_action_records_tools_without_forcing_different_image_workflow():
    plugin,state,event = make_plugin()
    plugin.config["check_comfyui_each_round"] = False
    state["manual_plan"] = "补画"
    plugin.tools = lambda *_: module.ToolSet([module.FunctionTool(name="pc_generate_photo",description="configured image generator",parameters={})])
    plugin.context.tool_loop_agent.return_value = SimpleNamespace(completion_text='{"message":"","summary":"checked"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["available_tool_names"] == ["pc_generate_photo"]
    prompt = plugin.context.tool_loop_agent.call_args.kwargs["prompt"]
    # Current guidance (ComfyUI 降级文案) keeps the same intent: prefer the
    # learned skill matching the task and never switch workflows because a
    # tool name looks specialized.
    assert "沿用与任务匹配的已学技能" in prompt
    assert "不因工具名字看似专用而擅自改换工作流" in prompt
    assert "如果有 pc_generate_photo" not in prompt
    assert "pc_generate_photo" in prompt


@pytest.mark.asyncio
async def test_native_skill_catalogue_is_injected_in_autonomous_turn(monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion import skill_context

    plugin,state,event=make_plugin()
    skill=SimpleNamespace(name="learned-template",description="use templates",path="D:/skills/template/SKILL.md",source_type="local_only",active=True)
    monkeypatch.setattr(skill_context.SkillManager,"list_skills",lambda *args,**kwargs:[skill])
    monkeypatch.setattr(skill_context,"build_skills_prompt",lambda skills:"SKILL CATALOGUE: learned-template")
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin)
    assert "SKILL CATALOGUE" in plugin.context.llm_generate.call_args.kwargs["system_prompt"]
    assert state["available_skill_names"]==["learned-template"]


@pytest.mark.asyncio
async def test_drawing_promise_without_tools_cannot_complete_manual_task():
    plugin,state,event=make_plugin()
    state["manual_plan"]="给我画张画"
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text='{"message":"画好了","summary":"完成","manual_plan_completed":true}')
    await plugin.turn(event.unified_msg_origin)
    assert plugin.context.tool_loop_agent.await_count==2
    event.send.assert_awaited_once()
    assert state["manual_plan"]=="给我画张画"
    assert state["last_turn"]["verification"]=="unexecuted"


@pytest.mark.asyncio
async def test_actual_image_receipt_allows_completion_without_replaying():
    plugin,state,event=make_plugin()
    state["manual_plan"]="给我画张画"
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"run_template","status":"verified"})
        return SimpleNamespace(completion_text='{"message":"","summary":"已出图","manual_plan_completed":true}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    assert plugin.context.tool_loop_agent.await_count==1
    assert not state["manual_plan"]
    event.send.assert_awaited_once()


def test_template_task_id_is_pending_not_completion():
    from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import receipt_status
    assert receipt_status("run_template",'{"status":"completed","outputs":[]}')=="unverified"
    assert receipt_status("run_template",'{"task_id":"one"}')=="pending"
    assert receipt_status("run_template",'{"status":"completed","outputs":["result.png"]}')=="verified"


@pytest.mark.asyncio
async def test_ui_operation_matrix_changes_expected_state():
    plugin,state,event=make_plugin()
    page=module.CompanionPageAPI(plugin)
    umo=event.unified_msg_origin
    for action,extra in [
        ("plan",{"value":"manual task"}),
        ("conditions",{"value":[{"id":"http","kind":"http","url":"http://localhost/status"}]}),
        ("project_add",{"title":"Project","goal":"finish"}),
        ("rhythm_reset",{}),
        ("pause",{"minutes":60,"mode":"messages"}),
        ("resume",{}),
        ("stop",{}),
        ("now",{}),
    ]:
        result=await page.perform({"umo":umo,"action":action,**extra})
        assert result["status"]=="ok"
    assert state["next_due"]==0
    assert state["manual_plan"]=="manual task"
    assert state["conditions"][0]["id"]=="http"
    project=state["projects"][0]
    await page.perform({"umo":umo,"action":"project","id":project["id"],"status":"paused"})
    assert project["status"]=="paused"
    state["outbox"]=[{"id":"one","status":"pending","content":"work"}]
    await page.perform({"umo":umo,"action":"outbox","id":"one","operation":"discard"})
    assert state["outbox"][0]["status"]=="discarded"
    state["appointments"]=[{"id":"appointment","status":"pending"}]
    await page.perform({"umo":umo,"action":"appointment_status","id":"appointment","status":"resolved"})
    assert state["appointments"][0]["status"]=="resolved"


@pytest.mark.asyncio
async def test_ui_preview_returns_decision_and_releases_busy_slot():
    plugin,_,event=make_plugin()
    plugin.turn=AsyncMock(return_value={"action":"silent"})
    result=await module.CompanionPageAPI(plugin).perform({"umo":event.unified_msg_origin,"action":"preview"})
    assert result["data"]["decision"]["action"]=="silent"
    assert event.unified_msg_origin not in plugin.running


def test_delivered_generated_image_not_blocked_by_optional_tag_lookup():
    from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import execution_verdict
    event=SimpleNamespace(checkpoint_task="给我画张画",media_deliveries=1,
        action_receipts=[{"tool":"run_template","status":"verified"},{"tool":"search_tags","status":"attempted"}],
        execution_contract={"mode":"action","steps":[
            {"goal":"检索标签","tools":["search_tags"],"accept":"observed"},
            {"goal":"生成画作","tools":["run_template"],"accept":"completed"},
            {"goal":"发送图片","tools":["pc_send_current_media"],"accept":"success"}]})
    assert execution_verdict(event,"contract")[0]=="verified"


def test_generic_contract_requires_each_tool_goal_not_unrelated_success():
    from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import validate_contract,execution_verdict
    contract=validate_contract({"mode":"action","steps":[
        {"goal":"查询设备","tools":["device_status"],"accept":"observed","criterion":"取得当前状态"},
        {"goal":"设置温度","tools":["set_temperature"],"accept":"success","criterion":"设备确认修改"},
    ]},["device_status","set_temperature"])
    event=SimpleNamespace(execution_contract=contract,action_receipts=[{"tool":"device_status","status":"success"}])
    assert execution_verdict(event,"contract")[0]=="unverified"
    event.action_receipts.append({"tool":"set_temperature","status":"success"})
    assert execution_verdict(event,"contract")[0]=="verified"


@pytest.mark.asyncio
async def test_plugin_identity_available_in_normal_chat():
    plugin,_,event=make_plugin()
    event.get_message_str=lambda:"我只在乎你插件在哪里"
    event.get_extra=lambda _:False
    event.set_extra=lambda *args:None
    request=module.ProviderRequest(system_prompt="persona")
    await plugin.mark_busy(event,request)
    assert "data/plugins/astrbot_plugin_autonomous_companion" in request.system_prompt
    assert "不是‘我会永远陪着你’的别称" in request.system_prompt


@pytest.mark.asyncio
async def test_late_identity_has_real_absolute_plugin_directory():
    plugin,_,event=make_plugin()
    event.get_message_str=lambda:"我只在乎你插件在哪里"
    event.get_extra=lambda _:False
    request=module.ProviderRequest(system_prompt="other enrichment")
    await plugin.preserve_local_plugin_identity(event,request)
    assert str(Path(module.__file__).resolve().parent) in request.system_prompt
    assert "中文显示名不是磁盘目录名" in request.system_prompt


@pytest.mark.asyncio
async def test_partial_task_with_media_still_sends_truthful_closing():
    plugin,state,event=make_plugin()
    state["manual_plan"]="画张画"
    plugin.config["general_execution_contract"]=True
    plugin.tools=lambda *_:module.ToolSet([module.FunctionTool(name="lookup",description="lookup",parameters={})])
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text=json.dumps({"mode":"action","steps":[{"goal":"检查素材","tools":["lookup"],"accept":"observed","criterion":"读取结果"}]}))
    async def execute(**kwargs):
        event.media_deliveries=1
        event.action_receipts.append({"tool":"other","status":"unverified"})
        return SimpleNamespace(completion_text='{"message":"","summary":"done"}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_awaited_once()
    assert "部分步骤" in event.send.call_args.args[0].get_plain_text()
    assert state["manual_plan"]


@pytest.mark.asyncio
async def test_authored_dialogue_is_not_rewritten_out_of_sync_with_plan():
    plugin,state,event=make_plugin()
    plugin.config["natural_task_closing"]=True
    state["manual_plan"]="聊聊天"
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text='{"message":"任务完成，请查收","summary":"chat"}')
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text="刚想跟你说这个。")
    await plugin.turn(event.unified_msg_origin)
    assert event.send.call_args.args[0].get_plain_text()=="任务完成，请查收"
    plugin.context.llm_generate.assert_not_called()


@pytest.mark.asyncio
async def test_generated_artifact_is_task_bound_and_sent_only_once():
    from data.plugins.astrbot_plugin_autonomous_companion.artifacts import register_artifacts,SendArtifactTool
    plugin,state,event=make_plugin()
    event.owner=plugin
    event.checkpoint_task="new drawing"
    items=register_artifacts(event,"run_template",json.dumps({"status":"completed","outputs":{"image":{"type":"image","url":"http://localhost/view?filename=new.png"}}}))
    tool=SendArtifactTool(event)
    event.checkpoint_task="other task"
    assert not json.loads(await tool.call(None,items[0]["id"]))["success"]
    event.checkpoint_task="new drawing"
    assert json.loads(await tool.call(None,items[0]["id"]))["sent"]
    assert not json.loads(await tool.call(None,items[0]["id"]))["success"]
    event.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_delivery_regenerated_artifact_is_not_sent():
    """Media regenerated after a successful delivery must not be appended."""
    from data.plugins.astrbot_plugin_autonomous_companion.artifacts import (
        SendArtifactTool,
        register_artifacts,
    )

    plugin, state, event = make_plugin()
    event.owner = plugin
    event.checkpoint_task = "lying in bed photo"

    async def send_through_delivery(chain):
        await plugin.deliver(event, chain)

    event.send = send_through_delivery

    def receipt(*names):
        return json.dumps(
            {
                "status": "completed",
                "outputs": {
                    name: {
                        "type": "image",
                        "url": f"http://localhost/view?filename={name}.png",
                    }
                    for name in names
                },
            }
        )

    batch = register_artifacts(event, "run_template", receipt("first", "second"))
    tool = SendArtifactTool(event)
    assert json.loads(await tool.call(None, batch[0]["id"]))["sent"]
    # 拦截基准随事件走：产物登记和首次投递同属一轮事件（artifacts.py 读 event）。
    assert getattr(event, "first_media_delivery_at", 0)
    # Same-batch siblings were registered before the first delivery: allowed.
    assert json.loads(await tool.call(None, batch[1]["id"]))["sent"]
    assert event.media_deliveries == 2

    # A new generation registered after the delivery (e.g. an interrupted extra
    # call that later resumed) must not become an unexpected extra picture.
    later = register_artifacts(event, "run_template", receipt("third"))
    later[0]["registered_at"] = event.first_media_delivery_at + 1
    blocked = json.loads(await tool.call(None, later[0]["id"]))
    assert not blocked["success"]
    assert "拦截" in blocked["reason"]
    assert event.media_deliveries == 2


@pytest.mark.asyncio
async def test_duplicate_call_key_does_not_block_different_arguments(monkeypatch):
    plugin,state,event=make_plugin()
    event.owner=plugin
    from data.plugins.astrbot_plugin_autonomous_companion.lifecycle import tool_key
    event.action_receipts=[{"tool":"query","call_key":tool_key("query",{"id":"old"}),"status":"success","result":"old result"}]
    called=[]
    async def execute(*args,**kwargs):
        called.append(kwargs)
        yield CallToolResult(content=[TextContent(type="text",text='{"success":true}')])
    monkeypatch.setattr(module.FunctionToolExecutor,"execute",execute)
    wrapped=module.BudgetTool(module.FunctionTool(name="query",description="query",parameters={}),event)
    first=[r async for r in wrapped.call(None,id="old")]
    assert "不重复提交" in first[0].content[0].text
    assert not called
    _=[r async for r in wrapped.call(None,id="new")]
    assert len(called)==1


def test_same_task_wording_new_checkpoint_has_different_identity():
    from data.plugins.astrbot_plugin_autonomous_companion.checkpoint import resume_checkpoint
    event=SimpleNamespace(state={},cid="one")
    assert not resume_checkpoint(event,"draw")
    first=event.checkpoint_id
    event.state={}
    assert not resume_checkpoint(event,"draw")
    assert event.checkpoint_id!=first


def test_checkpoint_exact_task_and_conversation_isolation():
    from data.plugins.astrbot_plugin_autonomous_companion.checkpoint import save_checkpoint,resume_checkpoint
    event=SimpleNamespace(state={},cid="one",checkpoint_task="work",execution_contract={"mode":"action","steps":[]},action_receipts=[{"tool":"submit","status":"pending","result":"job-id"}])
    save_checkpoint(event)
    restored=SimpleNamespace(state=event.state,cid="one")
    assert resume_checkpoint(restored,"work")
    assert restored.action_receipts[0]["result"]=="job-id"
    assert not resume_checkpoint(SimpleNamespace(state=event.state,cid="other"),"work")
    assert not resume_checkpoint(SimpleNamespace(state=event.state,cid="one"),"different")


@pytest.mark.asyncio
async def test_resume_supplies_existing_output_and_excludes_completed_tool():
    plugin,state,event=make_plugin()
    state["manual_plan"]="把结果发给我"
    contract={"mode":"action","steps":[{"goal":"生成结果","tools":["create_result"],"accept":"completed","criterion":"有产物"},{"goal":"发送结果","tools":["send_result"],"accept":"success","criterion":"发送回执"}]}
    state["execution_checkpoint"]={"task":state["manual_plan"],"cid":"cid","pending":True,"contract":contract,"receipts":[{"tool":"create_result","status":"verified","result":"real-output-path"}]}
    plugin.tools=lambda *_:module.ToolSet([module.FunctionTool(name=name,description=name,parameters={}) for name in ("create_result","send_result")])
    async def execute(**kwargs):
        assert [t.name for t in kwargs["tools"].tools]==["send_result"]
        assert "real-output-path" in kwargs["prompt"]
        event.action_receipts.append({"tool":"send_result","status":"success"})
        return SimpleNamespace(completion_text='{"message":"","manual_plan_completed":true}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    assert not state["execution_checkpoint"]["pending"]
    assert not state["manual_plan"]


@pytest.mark.asyncio
async def test_arbitrary_action_uses_generic_contract_without_keyword_match():
    plugin,state,event=make_plugin()
    plugin.config["general_execution_contract"]=True
    state["manual_plan"]="把温度调到二十度"
    plugin.tools=lambda *_:module.ToolSet([module.FunctionTool(name="set_temperature",description="adjust",parameters={})])
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text=json.dumps({"mode":"action","steps":[{"goal":"调节温度","tools":["set_temperature"],"accept":"success","criterion":"设备确认"}]}))
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text='{"message":"调好了","manual_plan_completed":true}')
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_awaited_once()
    assert state["manual_plan"]
    assert state["last_turn"]["verification"]=="unexecuted"


def test_active_window_ignores_budget_but_expiry_restores_it():
    from data.plugins.astrbot_plugin_autonomous_companion.daily import exhausted, usage_for

    plugin,state,_=make_plugin()
    plugin.config.update(ignore_budget_while_active=True,daily_message_limit=1)
    usage_for(state)["messages"]=10
    assert exhausted(plugin,state,"messages","daily_message_limit",200)
    state.update(rounds=1,window_end=time.time()+60,window_admitted=True)
    assert not exhausted(plugin,state,"messages","daily_message_limit",200)
    assert usage_for(state)["messages"]==10
    state["window_end"]=0
    assert exhausted(plugin,state,"messages","daily_message_limit",200)


@pytest.mark.asyncio
async def test_preview_never_runs_registered_dependency_tools(monkeypatch):
    plugin,state,event=make_plugin()
    state["conditions"]=[{"id":"job","kind":"tool","tool":"query_job","args":{}}]
    monkeypatch.setattr(module,"refresh_conditions",AsyncMock(side_effect=AssertionError("preview must not query")))
    monkeypatch.setattr(module,"recover_template_tools",AsyncMock(side_effect=AssertionError("preview must not reconnect")))
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent"}')
    await plugin.turn(event.unified_msg_origin,preview=True)
    assert state.get("progress") is None


@pytest.mark.asyncio
async def test_tool_status_not_written_as_conversation_or_companion_speech():
    plugin,state,event=make_plugin()
    event.companion_adapter=SimpleNamespace(sent=[])
    await plugin.deliver(event,module.MessageChain(type="tool_call").message("🔨 调用工具: test"))
    assert event.companion_adapter.sent==[]
    assert state["journal"][-1]["action"]=="tool_notice"


@pytest.mark.asyncio
async def test_pause_during_cancel_preserves_user_resume_time():
    plugin,state,event=make_plugin()
    async def interrupted(*args,**kwargs):
        plugin.revisions[event.unified_msg_origin]=1
        state.update(pause_until=time.time()+3600,pause_mode="all",next_due=time.time()+3600)
        raise asyncio.CancelledError()
    plugin.turn=interrupted
    with pytest.raises(asyncio.CancelledError):
        await plugin.run_due(event.unified_msg_origin)
    assert state["next_due"]>=state["pause_until"]-1


@pytest.mark.asyncio
async def test_config_rejects_inconsistent_frequency_without_mutating():
    plugin,_,_=make_plugin()
    plugin.config=PageConfig(plugin.config)
    with pytest.raises(ValueError,match="自主频率"):
        await module.CompanionPageAPI(plugin).update_config({"frequency_min_minutes":100,"frequency_max_minutes":20})
    assert "frequency_min_minutes" not in plugin.config


def test_optional_plan_nulls_are_omitted_not_crashes():
    state={"future_task":"keep"}
    module.apply_plan(state,{"future_task":None,"conditions":None,"project_updates":None})
    assert state["future_task"]=="keep"


@pytest.mark.asyncio
async def test_user_message_during_format_repair_prevents_stale_send():
    plugin,state,event=make_plugin()
    state["manual_plan"]="write"
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text="plain reply")
    async def repair(*args):
        plugin.revisions[event.unified_msg_origin]=1
        return {"message":"stale repaired answer"}
    plugin.control_output=repair
    with pytest.raises(asyncio.CancelledError):
        await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_first_round_does_not_gain_override_before_budget_admission():
    from data.plugins.astrbot_plugin_autonomous_companion.daily import usage_for
    plugin,state,event=make_plugin()
    plugin.config.update(ignore_budget_while_active=True,daily_round_limit=1)
    usage_for(state)["rounds"]=1
    result=await plugin.turn(event.unified_msg_origin)
    assert result["action"]=="silent"
    plugin.prepare.assert_not_called()
    assert not state.get("window_admitted")


@pytest.mark.asyncio
async def test_terminate_unregisters_only_own_page_handlers():
    plugin,_,_=make_plugin()
    plugin.worker=None
    plugin.page_api=module.CompanionPageAPI(plugin)
    unrelated=lambda:None
    plugin.context.registered_web_apis=[("/own",plugin.page_api.snapshot,["GET"],""),("/other",unrelated,["GET"],"")]
    await plugin.terminate()
    assert [entry[0] for entry in plugin.context.registered_web_apis]==["/other"]


@pytest.mark.asyncio
async def test_work_tool_writes_complete_text_with_receipt(tmp_path, monkeypatch):
    from data.plugins.astrbot_plugin_autonomous_companion.works import SaveWorkTool, StarTools

    plugin, state, event = make_plugin()
    event.owner = plugin
    monkeypatch.setattr(StarTools, "get_data_dir", lambda _: tmp_path)
    receipt = json.loads(await SaveWorkTool(event).call(None, title="第三层蓝", content="真正的完整正文"))
    assert Path(receipt["path"]).read_text(encoding="utf-8") == "第三层蓝\n\n真正的完整正文"
    assert state["saved_works"][0]["saved"] is True
    assert state["journal"][-1]["action"] == "work_saved"


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_work_optional_companion_sync_preserves_local_file(tmp_path, monkeypatch, fails):
    from data.plugins.astrbot_plugin_autonomous_companion.works import SaveWorkTool, StarTools

    plugin, state, event = make_plugin()
    event.owner = plugin
    api = attach_companion(plugin)
    api.import_completed_text_work = AsyncMock(return_value={"saved": True, "project_id": "shelf-one"})
    if fails:
        api.import_completed_text_work.side_effect = RuntimeError("host offline")
    monkeypatch.setattr(StarTools, "get_data_dir", lambda _: tmp_path)
    receipt = json.loads(await SaveWorkTool(event).call(None, title="Story", content="full text"))
    assert Path(receipt["path"]).is_file()
    assert receipt["companion"]["saved"] is (not fails)
    api.import_completed_text_work.assert_awaited_once()
    assert api.import_completed_text_work.call_args.kwargs["content"] == "full text"


@pytest.mark.asyncio
async def test_tool_work_without_message_still_reaches_user():
    """A round that ran tools silently must not end on the tool bubble alone."""
    plugin,state,event=make_plugin()
    plugin.config["task_completion_message"]=True
    plugin.config["natural_task_closing"]=False
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"tools","task":"翻一下工作目录里的文件","reason":"看看有什么"}')
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"autonomous_dsh_task","status":"verified","result":"listed 12 files"})
        return SimpleNamespace(completion_text='{"message":"","summary":"翻了目录"}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_awaited_once()
    sent="".join(part.text for part in event.send.call_args.args[0].chain)
    assert sent.strip()
    assert sent.strip()!="NO_REPLY"


@pytest.mark.asyncio
async def test_tool_work_delivery_tool_does_not_double_send():
    """When the tool itself sent the message, no extra closing text is added."""
    plugin,state,event=make_plugin()
    plugin.config["task_completion_message"]=True
    plugin.config["natural_task_closing"]=False
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"tools","task":"把结果告诉对方","reason":"想说的话"}')
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"send_message_to_user","status":"success","result":"sent"})
        return SimpleNamespace(completion_text='{"message":"","summary":"说了"}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_tool_work_respects_quiet_round():
    """Background-only rounds still stay silent even after silent tool work."""
    plugin,state,event=make_plugin()
    plugin.config["task_completion_message"]=True
    plugin.config["natural_task_closing"]=False
    event.action_receipts=[]
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"autonomous_dsh_task","status":"verified","result":"done"})
        return SimpleNamespace(completion_text='{"message":"","summary":"后台"}')
    plugin.context.tool_loop_agent.side_effect=execute
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"tools","task":"后台整理文件","notify":false,"reason":"只后台"}')
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_failed_tool_work_is_reported_not_swallowed():
    """A failed tool with no model text still owes the user an honest word."""
    plugin,state,event=make_plugin()
    plugin.config["task_completion_message"]=True
    plugin.config["natural_task_closing"]=False
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"tools","task":"跑个脚本试试","reason":"好奇"}')
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"autonomous_dsh_task","status":"failed","result":"exit code 1"})
        return SimpleNamespace(completion_text='{"message":"","summary":"失败"}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_awaited_once()
    sent="".join(part.text for part in event.send.call_args.args[0].chain)
    assert "没弄成" in sent
    assert "还没完成" not in sent


@pytest.mark.asyncio
async def test_chat_round_query_tool_is_not_forced_to_speak():
    """Chat rounds may query context and stay silent without a forced message."""
    plugin,state,event=make_plugin()
    plugin.config["task_completion_message"]=True
    plugin.config["natural_task_closing"]=False
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"chat","task":"看看他最近在聊什么","reason":"想他了"}')
    async def execute(**kwargs):
        event.action_receipts.append({"tool":"search_tags","status":"unverified","result":"some tags"})
        return SimpleNamespace(completion_text='{"message":"","summary":"查了查"}')
    plugin.context.tool_loop_agent.side_effect=execute
    await plugin.turn(event.unified_msg_origin)
    event.send.assert_not_called()


def test_legacy_quiet_drafts_migrate_on_load():
    """Legacy 本轮暂存成果 entries become one pending message at load."""
    plugin,state,event=make_plugin()
    old=time.time()-80000
    state["outbox"]=[
        {"id":"a","title":"本轮暂存成果","content":"旧话","artifact":"","reason":"本轮只后台工作或消息预算已满","status":"pending","created_at":old},
        {"id":"b","title":"本轮暂存成果","content":"新话","artifact":"","reason":"本轮只后台工作或消息预算已满","status":"pending","created_at":old+100},
        {"id":"c","title":"正常作品","content":"","artifact":"x.png","reason":"从作品库加入待分享","status":"pending","created_at":old,"work_id":"w1"},
    ]
    plugin.migrate_legacy_drafts(state)
    assert state["pending_message"]["content"]=="新话"
    titles=[item["title"] for item in state["outbox"]]
    assert titles==["正常作品"]
    assert all("本轮暂存成果" not in t for t in titles)


@pytest.mark.asyncio
async def test_quiet_turn_draft_is_pending_message_not_outbox():
    """A quiet turn's chat draft must not masquerade as an outbox 成果."""
    plugin,state,event=make_plugin()
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"chat","task":"后台随便聊聊","notify":false,"reason":"只后台"}')
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text='{"message":"晚安，做个好梦。","summary":"后台说了句晚安"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["pending_message"]["content"]=="晚安，做个好梦。"
    assert state.get("outbox",[])==[]
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_pending_draft_waits_for_a_speaking_round():
    """沉默不代替她开口：草稿留住，等下一轮她真的说话时再补。"""
    plugin,state,event=make_plugin()
    state["pending_message"]={"content":"之前想说的","saved_at":time.time(),"expires_at":time.time()+3600,"source_task":"旧"}
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent","reason":"这轮想安静"}')
    await plugin.turn(event.unified_msg_origin)
    assert state["pending_message"]["content"]=="之前想说的"
    event.send.assert_not_called()

    # 下一轮她真的开口，才轮到旧草稿补发。
    state["rounds"]=0
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"chat","task":"正常说话"}')
    plugin.context.tool_loop_agent.return_value=SimpleNamespace(completion_text='{"message":"新的一句","summary":"done"}')
    await plugin.turn(event.unified_msg_origin)
    assert "pending_message" not in state
    texts=["".join(getattr(p,"text","") for p in c.args[0].chain) for c in event.send.await_args_list]
    assert "之前想说的" in texts


@pytest.mark.asyncio
async def test_expired_pending_message_is_dropped():
    """Expired drafts are discarded, never sent stale."""
    plugin,state,event=make_plugin()
    state["pending_message"]={"content":"过时的话","saved_at":time.time()-90000,"expires_at":time.time()-1,"source_task":"旧"}
    event.action_receipts=[]
    plugin.context.llm_generate.return_value=SimpleNamespace(completion_text='{"action":"silent","reason":"沉默"}')
    await plugin.turn(event.unified_msg_origin)
    assert "pending_message" not in state
    event.send.assert_not_called()


@pytest.mark.asyncio
async def test_pending_message_discard_action():
    """The page can drop a stored draft explicitly."""
    from data.plugins.astrbot_plugin_autonomous_companion.page_api import CompanionPageAPI
    plugin,state,event=make_plugin()
    state["pending_message"]={"content":"话","saved_at":time.time(),"expires_at":time.time()+3600,"source_task":"旧"}
    api=CompanionPageAPI(plugin)
    await api.perform({"action":"pending_message_discard","umo":event.unified_msg_origin})
    assert "pending_message" not in state


def test_silent_work_receipts_selection():
    """Only real, non-delivery work counts as owing the user words."""
    delivery=module._delivery_tool_names({})
    work=module.silent_work_receipts
    assert len(work([{"tool":"autonomous_dsh_task","status":"verified"}],delivery))==1
    assert len(work([{"tool":"autonomous_dsh_task","status":"failed"}],delivery))==1
    assert len(work([{"tool":"autonomous_dsh_task","status":"unverified"}],delivery))==1
    assert work([{"tool":"send_message_to_user","status":"success"}],delivery)==[]
    assert work([{"tool":"autonomous_send_artifact","status":"success"}],delivery)==[]
    assert work([{"tool":"autonomous_dsh_task","status":"attempted"}],delivery)==[]
    assert len(work([{"tool":"autonomous_dsh_task","status":"pending"}],delivery))==1
    overridden=module._delivery_tool_names({"delivery_tools":["Autonomous_Dsh_Task"]})
    assert work([{"tool":"autonomous_dsh_task","status":"verified"}],overridden)==[]


@pytest.mark.asyncio
async def test_companion_shelf_import_keeps_full_body_and_deduplicates(monkeypatch):
    from contextlib import asynccontextmanager
    try:
        from data.plugins.astrbot_plugin_private_companion import external_work_import as importer
    except ModuleNotFoundError:
        pytest.skip("astrbot_plugin_private_companion 未安装，书架导入测试随之跳过")

    @asynccontextmanager
    async def authority(_):
        yield

    monkeypatch.setattr(importer, "story_legacy_async_context", authority)
    plugin = SimpleNamespace(_data_lock=asyncio.Lock(), data={}, _save_data_sync=lambda **kwargs: None)
    first = await importer.import_completed_work(plugin, title="Story", content="complete body", source_id="one")
    second = await importer.import_completed_work(plugin, title="Story", content="complete body", source_id="one")
    assert first["saved"] and second["deduplicated"]
    assert len(plugin.data["creative_projects"]) == 1
    assert plugin.data["creative_projects"][0]["draft_chunks"][0]["text"] == "complete body"
    assert plugin.data["creative_projects"][0]["status"] == "finished"


@pytest.mark.asyncio
async def test_timeout_killed_round_leaves_honest_pending_message():
    """A turn killed by the timeout must not vanish silently."""
    plugin, state, event = make_plugin()
    # The round did real work before the timeout killed it.
    state["last_turn"] = {
        "status": "running",
        "action": "tools",
        "task": "整理模型对比",
        "background": False,
        "tool_receipts": [{"tool": "safe_write", "status": "verified"}],
    }

    async def timing_out_turn(umo, preview=False, **kwargs):
        await asyncio.sleep(60)

    plugin.turn = timing_out_turn
    plugin.number = lambda key, default, lo, hi: 0 if key == "turn_timeout_seconds" else default
    await plugin.run_due(event.unified_msg_origin)
    assert state["pending_message"]["content"]
    assert "打断" in state["pending_message"]["content"]
    assert state["last_error"]


@pytest.mark.asyncio
async def test_timeout_note_not_added_for_quiet_or_receiptless_rounds():
    """No pending note when the round was quiet or did no real work."""
    plugin, state, event = make_plugin()

    async def timing_out_turn(umo, preview=False, **kwargs):
        await asyncio.sleep(60)

    plugin.turn = timing_out_turn
    plugin.number = lambda key, default, lo, hi: 0 if key == "turn_timeout_seconds" else default
    # Quiet round: background=True -> no note.
    state["last_turn"] = {"status": "running", "background": True, "tool_receipts": [{"tool": "x", "status": "verified"}]}
    await plugin.run_due(event.unified_msg_origin)
    assert "pending_message" not in state
    # No receipts at all -> no note.
    state["last_turn"] = {"status": "running", "background": False, "tool_receipts": []}
    await plugin.run_due(event.unified_msg_origin)
    assert "pending_message" not in state


@pytest.mark.asyncio
async def test_dispatched_local_media_is_registered_into_work_library(tmp_path):
    """Proactive dispatch archives sent local media into the work library."""
    plugin, state, event = make_plugin()
    from astrbot.api.message_components import File, Image

    art = tmp_path / "art.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\npng")
    doc = tmp_path / "notes.bin"
    doc.write_bytes(b"\x00notes")

    await plugin.deliver_segment(
        event,
        module.MessageChain(
            chain=[Image(file=str(art)), File(name="notes.bin", file=str(doc))]
        ),
    )

    dispatched = [
        w for w in state["work_library"] if w["producer"] == "actual_output_dispatch"
    ]
    names = {Path(w["path"]).name for w in dispatched}
    assert {"art.png", "notes.bin"} <= names
    assert all(w["status"] == "recorded" for w in dispatched)
    assert event.media_deliveries == 1


@pytest.mark.asyncio
async def test_dispatched_remote_media_is_not_registered(tmp_path):
    """URL-only image references have no local file and must stay out of the library."""
    plugin, state, event = make_plugin()
    from astrbot.api.message_components import Image

    await plugin.deliver_segment(
        event,
        module.MessageChain(
            chain=[Image(file="http://127.0.0.1:8188/view?filename=x.png")]
        ),
    )

    assert state.get("work_library", []) == []

@pytest.mark.asyncio
async def test_proactive_round_gets_a_precise_refusal_from_private_only_tools():
    plugin, state, event = make_plugin()
    event.get_extra = lambda key: key == "autonomous_companion"
    refused = json.loads(
        await module.AutonomousCompanion.autonomous_long_task(plugin, event, "list")
    )
    assert refused["ok"] is False and refused["guard"] is True
    assert "主动回轮" in refused["error"] and "真实私聊" in refused["error"]
    assert "别重试" in refused["hint"]
    refused = json.loads(
        await module.AutonomousCompanion.autonomous_task_control(
            plugin, event, "list", []
        )
    )
    assert refused["ok"] is False and refused["guard"] is True
    # 没登记的会话也要说清是"不在名单里"，而不是含糊的"只支持已配置对象"。
    event.get_extra = lambda key: False
    event.unified_msg_origin = "test:FriendMessage:999"
    refused = json.loads(
        await module.AutonomousCompanion.autonomous_long_task(plugin, event, "list")
    )
    assert refused["guard"] is True
    assert "不在配置的私聊对象里" in refused["error"]

def test_default_prompts_encourage_real_tool_use_without_flattery():
    from data.plugins.astrbot_plugin_autonomous_companion import planning

    text = module.DECISION_PROMPT
    # 有具体的事就动手，不用等人开口、也不必先取得同意。
    assert "查、跑、做、验" in text and "不必先取得同意" in text
    # 但不是"讨好主人"：允许分歧，也允许带着情绪做事。
    assert "不是为了讨好谁" in text and "意见不同" in text
    # 也不许为了用而用：每次调用要有具体目的。
    assert "不是为了用而用" in text and "说不出要做成什么就别调" in text
    # 沉默是正常选项，不能被挤成"必须开口"。
    assert "silent 是正常选项" in text
    # 沉默的理由要写全：情绪 + 他在忙，不能只有"没内容"。
    assert "沉默可以是情绪" in text and "沉默也可以是替他想" in text
    assert "按\"忙\"处理" in text and "别翻旧账" in text
    # 但连续主动是插件自带的，不能被写成"攒着等回来"。
    assert "仍然可以按窗口继续主动" in text
    assert "不要为了等他回应而加密频率" in text
    # 也别把"分享"只停在嘴上：能做出成品给他的工具要真用起来，但要有由头。
    assert "分享（画、歌、图）不是日常动作" in text
    assert "说不出由头就不发" in text
    assert "比空口说" not in text
    assert "说不出由头就不发" in text
    # 同一件事不换个说法再讲一遍（凌晨那次连着两条几乎一样的叫起床）。
    assert "同一句嘱咐说过一遍就够了" in text
    # 点歌是最容易变成刷屏的分享：一天一首、不重复、不连发。
    assert "点歌是最容易变成刷屏的" in text and "连着发歌比不说话更烦人" in text and "不要填 platform" in text
    # 计划阶段也别把能做的活只写成设想，但同样不许空转。
    assert "本轮就动手做" in planning.PLANNING_PROMPT
    assert "没有实际推进就不要为了填满一轮而调用" in planning.PLANNING_PROMPT


def test_silence_and_continuation_semantics_are_spelled_out():
    decision = module.DECISION_PROMPT
    # 沉默是"停"：会结束整条链，不能当暂停键用。
    assert "silent 是\"停\"不是\"歇一下\"" in decision
    assert "也不会在沉默时替你发出去" in decision
    # 已有待续目标时是接着做，不是另开话题。
    assert "这一轮是接着做，不是另开话题" in decision
    action = module.ACTION_PROMPT
    # 续行要把两个字段一起给，且每轮都要有新东西。
    assert "续行要两个字段一起给" in action
    assert "换个角度说同一件事不算新东西" in action
    assert "不要用沉默代替\"歇一下\"" in action
    # 连续发送：一条一件事、他回话就停、别把心事压成一大块。
    assert "一条只装一件事" in action
    assert "立刻停下来接他的话" in action
    assert "给你把一件事分几拍说完" in action
    # 但也要说清什么时候值得连着发，不能只写"别发"。
    assert "值得连着发的情形" in action
    assert "不值得的情形" in action
    assert "换个角度说同一件事不算新东西" in action
    # 决策层也要知道：他在的时候不按原计划念稿。
    assert "立刻回到对话里" in module.DECISION_PROMPT


def test_pending_checkpoint_stops_auto_resuming_after_the_cap():
    from data.plugins.astrbot_plugin_autonomous_companion import checkpoint as checkpoint_module

    plugin, state, event = make_plugin()
    state["execution_checkpoints"] = {
        "cp1": {
            "task": "证明发歌功能正常",
            "task_id": "cp1",
            "cid": "cid",
            "updated_at": time.time(),
            "contract": {"mode": "action", "steps": [{"goal": "放一首新歌", "tools": ["play_song_by_name"]}]},
            "receipts": [],
            "media_deliveries": 0,
            "pending": True,
            "project_id": "",
            "next_attempt_at": 0,
        }
    }
    # 断点没有时间过期机制：不限次数的话，同一件事能被自动续跑几个小时。
    for _ in range(checkpoint_module.MAX_CHECKPOINT_RESUMES):
        assert module.next_checkpoint(state, "cid") is not None
        module.count_checkpoint_resume(state, "证明发歌功能正常")
    assert module.next_checkpoint(state, "cid") is None


def test_today_digest_lists_what_the_bot_already_did():
    plugin, state, event = make_plugin()
    # 还没做过任何事：不占位、不编内容。
    assert plugin.today_digest(state) == ""

    plugin.today_record(state, "song", "温柔 五月天")
    plugin.today_record(state, "song", "温柔 五月天")
    plugin.today_record(state, "song", "倔强 五月天")
    plugin.today_slot(state)["tools"]["play_song_by_name"] = 3
    plugin.journal(state, "dispatched", "今天发了两首歌")
    plugin.journal(state, "silent", "他在忙，不打扰")

    text = plugin.today_digest(state)
    assert "【今天你自己已经做过的事】" in text
    # 同一首重复发过两次，必须自己看得见。
    assert "《温柔 五月天》（2 次）" in text
    assert "《倔强 五月天》" in text
    assert "play_song_by_name×3" in text
    assert "主动开口 1 次" in text and "沉默 1 次" in text
    # 汇总末尾要给出"别重做"的用法，否则只是罗列。
    assert "不要再做第二遍" in text


def test_today_ledger_resets_on_a_new_day():
    plugin, state, event = make_plugin()
    plugin.today_record(state, "song", "倔强 五月天")
    assert state["today"]["items"]
    state["today"]["day"] = "2000-01-01"
    # 换天就清空，不能把昨天的账算到今天头上。
    assert plugin.today_digest(state) == ""
    assert plugin.today_slot(state)["day"] != "2000-01-01"


def test_brief_tool_args_keeps_the_meaningful_argument():
    assert (
        module.brief_tool_args({"song_name": "温柔 五月天", "platform": "网易点歌"})
        == "温柔 五月天"
    )
    assert module.brief_tool_args('{"path": "D:/a/b.txt"}') == "D:/a/b.txt"
    assert module.brief_tool_args({}) == ""
    assert module.brief_tool_args("not json") == "not json"


class _ScopeGuard:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _native_scopes():
    return SimpleNamespace(
        adapter_access=lambda *a, **k: _ScopeGuard(),
        resolve_event_memory_access=AsyncMock(
            return_value=SimpleNamespace(allowed=True, scope="scoped", persona_id="persona")
        ),
        is_event_memory_allowed=lambda *a, **k: True,
        resolve_memory_scope=lambda *a, **k: "scoped",
        get_persona_id=AsyncMock(return_value="persona"),
    )


@pytest.mark.asyncio
async def test_livingmemory_native_bridge_recalls_and_records_inside_its_scope():
    engine = SimpleNamespace(
        search_memories=AsyncMock(
            return_value=[
                SimpleNamespace(content="白喜欢五月天", metadata={}),
                SimpleNamespace(content="  ", metadata={}),
            ]
        ),
        add_memory=AsyncMock(return_value=7),
    )
    processor = SimpleNamespace(
        build_memory_from_structured_data=lambda **kw: (
            "MEM:" + kw["structured_data"]["summary"],
            {"importance": 0.4},
            0.4,
        )
    )
    living = SimpleNamespace(
        initializer=SimpleNamespace(memory_engine=engine, memory_processor=processor),
        config_manager={"filtering_settings": {}},
        context=object(),
    )
    event = SimpleNamespace(unified_msg_origin="香夜梨:FriendMessage:123")
    bridge = module.LivingMemoryNativeBridge(living, event, _native_scopes())

    text = await bridge.compose_context(query="音乐 偏好", top_k=6, max_chars=100)
    # 空内容不占行；作用域和人格按 LivingMemory 自己解析的结果传下去。
    assert text == "- 白喜欢五月天"
    assert engine.search_memories.await_args.kwargs["session_id"] == "scoped"
    assert engine.search_memories.await_args.kwargs["persona_id"] == "persona"

    await bridge.record_visible_turn(role="assistant", content="  我刚放了首歌  ")
    written = engine.add_memory.await_args.kwargs
    assert written["content"] == "MEM:我刚放了首歌"
    assert written["session_id"] == "scoped" and written["persona_id"] == "persona"
    assert written["metadata"]["memory_origin"] == "autonomous_companion"

    with pytest.raises(ValueError):
        await bridge.record_visible_turn(role="user", content="不该只记用户这边")


@pytest.mark.asyncio
async def test_memory_bridge_prefers_hooks_then_native_then_companion():
    plugin, state, event = make_plugin()

    hooked = SimpleNamespace(
        autonomous_recall=AsyncMock(return_value="x"), autonomous_record=AsyncMock()
    )
    plugin.context.get_all_stars = lambda: [
        SimpleNamespace(
            activated=True,
            name="LivingMemory",
            root_dir_name="astrbot_plugin_livingmemory",
            star_cls=hooked,
        )
    ]
    bridge = plugin.memory_bridge(event)
    assert isinstance(bridge, module.LivingMemoryBridge)
    assert event.state["memory_provider"] == "LivingMemory"

    bare = SimpleNamespace(initializer=SimpleNamespace())
    plugin.context.get_all_stars = lambda: [
        SimpleNamespace(
            activated=True,
            name="LivingMemory",
            root_dir_name="astrbot_plugin_livingmemory",
            star_cls=bare,
        )
    ]
    plugin.livingmemory_scopes = lambda meta: _native_scopes()
    bridge = plugin.memory_bridge(event)
    # 没有专用接口时用原生服务，而不是悄悄退回 MemoryCompanion。
    assert isinstance(bridge, module.LivingMemoryNativeBridge)
    assert event.state["memory_provider"] == "LivingMemory"

    plugin.livingmemory_scopes = lambda meta: None
    plugin.context.get_all_stars = lambda: [
        SimpleNamespace(
            activated=True,
            name="LivingMemory",
            root_dir_name="astrbot_plugin_livingmemory",
            star_cls=bare,
        ),
        SimpleNamespace(
            activated=True,
            name="MemoryCompanion",
            root_dir_name="astrbot_plugin_memory_companion",
            star_cls=SimpleNamespace(memory_companion="companion-api"),
        ),
    ]
    bridge = plugin.memory_bridge(event)
    assert bridge == "companion-api"
    assert "接口不完整" in event.state["livingmemory_status"]


@pytest.mark.asyncio
async def test_inactive_livingmemory_does_not_hijack_the_memory_bridge():
    plugin, state, event = make_plugin()
    plugin.context.get_all_stars = lambda: [
        SimpleNamespace(
            activated=False,
            name="LivingMemory",
            root_dir_name="astrbot_plugin_livingmemory",
            star_cls=SimpleNamespace(),
        ),
        SimpleNamespace(
            activated=True,
            name="MemoryCompanion",
            root_dir_name="astrbot_plugin_memory_companion",
            star_cls=SimpleNamespace(memory_companion="companion-api"),
        ),
    ]
    # 只开一个记忆插件时，停用的 LivingMemory 不能把桥抢走。
    assert plugin.memory_bridge(event) == "companion-api"
    assert event.state["memory_provider"] == "MemoryCompanion"


def test_native_bridge_detects_group_chat_from_event_or_origin():
    bridge = module.LivingMemoryNativeBridge(None, SimpleNamespace(), None)
    bridge.event = SimpleNamespace(get_message_type=lambda: "GroupMessage")
    assert bridge._is_group_chat() is True
    bridge.event = SimpleNamespace(unified_msg_origin="香夜梨:FriendMessage:123")
    assert bridge._is_group_chat() is False
    bridge.event = SimpleNamespace(unified_msg_origin="香夜梨:GroupMessage:999")
    assert bridge._is_group_chat() is True


@pytest.mark.asyncio
async def test_native_bridge_falls_back_to_older_livingmemory_helpers():
    class _OldEngine:
        def __init__(self):
            self.search_kwargs = None
            self.add_kwargs = None

        async def search_memories(self, *, query, k, session_id, persona_id=None):
            if persona_id is not None:
                raise TypeError("unexpected keyword argument 'persona_id'")
            self.search_kwargs = {"query": query, "k": k, "session_id": session_id}
            return [SimpleNamespace(content="旧版记忆", metadata={})]

        async def add_memory(self, *, content, session_id, importance, metadata):
            self.add_kwargs = {
                "content": content,
                "session_id": session_id,
                "importance": importance,
                "metadata": metadata,
            }
            return 11

    engine = _OldEngine()
    living = SimpleNamespace(
        initializer=SimpleNamespace(memory_engine=engine, memory_processor=None),
        config_manager={},
        context=object(),
    )
    event = SimpleNamespace(
        unified_msg_origin="香夜梨:FriendMessage:123", persona_id="persona-from-event"
    )
    # 旧版 LivingMemory：没有合并解析入口，也没有 get_persona_id。
    scopes = SimpleNamespace(
        adapter_access=lambda *a, **k: _ScopeGuard(),
        resolve_event_memory_access=None,
        is_event_memory_allowed=lambda *a, **k: True,
        resolve_memory_scope=lambda *a, **k: "scoped",
        get_persona_id=None,
    )
    bridge = module.LivingMemoryNativeBridge(living, event, scopes)

    text = await bridge.compose_context(query="旧版", top_k=5, max_chars=100)
    assert text == "- 旧版记忆"
    assert engine.search_kwargs == {"query": "旧版", "k": 5, "session_id": "scoped"}

    await bridge.record_visible_turn(role="assistant", content="旧版写入")
    assert engine.add_kwargs["content"] == "旧版写入"
    assert engine.add_kwargs["session_id"] == "scoped"
    assert engine.add_kwargs["metadata"]["memory_origin"] == "autonomous_companion"


def test_livingmemory_scopes_bundle_tolerates_missing_helpers():
    empty = module.LivingMemoryScopes(SimpleNamespace(), SimpleNamespace())
    assert not empty.usable
    assert empty.resolve_event_memory_access is None

    def _noop(*args, **kwargs):
        return None

    full = module.LivingMemoryScopes(
        SimpleNamespace(
            adapter_access=_noop,
            resolve_event_memory_access=_noop,
            is_event_memory_allowed=_noop,
            resolve_memory_scope=_noop,
        ),
        SimpleNamespace(get_persona_id=_noop),
    )
    assert full.usable


def test_livingmemory_scopes_returns_none_when_helpers_incomplete(monkeypatch):
    plugin, _, _ = make_plugin()
    meta = SimpleNamespace(root_dir_name="astrbot_plugin_livingmemory")

    def fake_import(name):
        return SimpleNamespace()

    monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=fake_import))
    # 旧版模块缺助手时降级返回 None，而不是把 AttributeError 炸穿整轮。
    assert plugin.livingmemory_scopes(meta) is None


@pytest.mark.asyncio
async def test_native_bridge_hands_memory_time_to_livingmemory_formatter():
    engine = SimpleNamespace(
        search_memories=AsyncMock(
            return_value=[
                SimpleNamespace(
                    content="他说烧鸭饭的卤汁很够味",
                    metadata={"create_time": 1762930000.0},
                    final_score=0.9,
                )
            ]
        )
    )
    living = SimpleNamespace(
        initializer=SimpleNamespace(memory_engine=engine, memory_processor=None),
        config_manager={},
        context=object(),
    )
    event = SimpleNamespace(unified_msg_origin="x:FriendMessage:1", persona_id="p")
    seen = {}

    def fake_format(memories, *, max_chars=3000):
        seen["memories"] = memories
        return "格式化后的记忆"

    scopes = SimpleNamespace(
        adapter_access=lambda *a, **k: _ScopeGuard(),
        resolve_event_memory_access=None,
        is_event_memory_allowed=lambda *a, **k: True,
        resolve_memory_scope=lambda *a, **k: "s",
        get_persona_id=None,
        format_memories_for_injection=fake_format,
    )
    bridge = module.LivingMemoryNativeBridge(living, event, scopes)

    assert (
        await bridge.compose_context(query="烧鸭饭", top_k=5, max_chars=800)
        == "格式化后的记忆"
    )
    # 写入时间必须原样交给渲染器，否则模型只能自己猜"昨天"。
    assert seen["memories"][0]["timestamp"] == 1762930000.0
    assert seen["memories"][0]["content"] == "他说烧鸭饭的卤汁很够味"


@pytest.mark.asyncio
async def test_native_bridge_fallback_keeps_memory_date():
    engine = SimpleNamespace(
        search_memories=AsyncMock(
            return_value=[
                SimpleNamespace(
                    content="今天中午吃了烧鸭饭",
                    metadata={"create_time": 1762930000.0},
                    final_score=0.5,
                )
            ]
        )
    )
    living = SimpleNamespace(
        initializer=SimpleNamespace(memory_engine=engine, memory_processor=None),
        config_manager={},
        context=object(),
    )
    event = SimpleNamespace(unified_msg_origin="x:FriendMessage:1", persona_id="p")
    # 旧版没有渲染器可借：降级路径也必须自己带上日期。
    scopes = SimpleNamespace(
        adapter_access=lambda *a, **k: _ScopeGuard(),
        resolve_event_memory_access=None,
        is_event_memory_allowed=lambda *a, **k: True,
        resolve_memory_scope=lambda *a, **k: "s",
        get_persona_id=None,
        format_memories_for_injection=None,
    )
    bridge = module.LivingMemoryNativeBridge(living, event, scopes)

    text = await bridge.compose_context(query="烧鸭饭", top_k=5, max_chars=200)
    assert "- [" in text
    assert "今天中午吃了烧鸭饭" in text


def test_schema_default_extra_prompt_encourages_tools():
    schema = json.loads(
        (Path(module.__file__).parent / "_conf_schema.json").read_text(
            encoding="utf-8-sig"
        )
    )
    default = schema["extra_prompt"]["default"]
    assert "不是为了讨好谁" in default
    assert "意见不同" in default
    # 默认行为偏好不该把沉默说成"要避免的事"，也不该鼓励无由头的分享。
    assert "别为了显得勤快而冒泡" in default
    assert "同一件事不换个说法再讲一遍" in default
    assert "才发一张画、一首歌或别的成品" in default
