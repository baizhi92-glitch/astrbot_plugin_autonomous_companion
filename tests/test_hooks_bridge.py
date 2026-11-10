"""第二批改动：通知器接线、只暴露台词的钩子、控制原文保留、后台标记。"""

import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main",
    Path(__file__).parents[1] / "main.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_response(text, **extra):
    response = SimpleNamespace(completion_text=text)
    for key, value in extra.items():
        setattr(response, key, value)
    return response


def make_event(autonomous=True):
    extras = {"autonomous_companion": True} if autonomous else {}
    event = SimpleNamespace(
        set_extra=lambda key, value: extras.__setitem__(key, value),
        get_extra=lambda key, default=None: extras.get(key, default),
        extras=extras,
    )
    return event


def make_plugin(**config):
    plugin = object.__new__(module.AutonomousCompanion)
    plugin.config = {"expose_plugin_hooks": True}
    plugin.config.update(config)
    plugin.logger = logging.getLogger("test-hooks")
    plugin.context = SimpleNamespace(llm_generate=AsyncMock())
    return plugin


def test_visible_text_returns_only_message():
    payload = json.dumps(
        {
            "message": "我把封面改好了。",
            "summary": "改封面",
            "continue_after_seconds": 0,
            "next_task": "",
        },
        ensure_ascii=False,
    )
    assert module.visible_control_text(payload) == "我把封面改好了。"


def test_visible_text_blank_when_control_has_no_message():
    payload = json.dumps({"blocked_reason": "素材没到"}, ensure_ascii=False)
    assert module.visible_control_text(payload) == ""


def test_visible_text_passes_through_plain_prose():
    assert module.visible_control_text("今晚月亮挺好看。") is None
    # 普通散文里出现的花括号不是控制内容，必须原样保留
    assert module.visible_control_text("他问我 {这样} 行不行") is None


def test_visible_text_ignores_json_without_control_markers():
    assert module.visible_control_text('{"name": "香夜梨", "age": 3}') is None


@pytest.mark.asyncio
async def test_hook_rewrites_text_for_autonomous_event():
    plugin = make_plugin()
    event = make_event(True)
    response = make_response(
        json.dumps(
            {"message": "我把封面改好了。", "summary": "改封面", "next_task": ""},
            ensure_ascii=False,
        )
    )
    original = response.completion_text

    await module.AutonomousCompanion.expose_only_visible_text(plugin, event, response)
    assert response.completion_text == "我把封面改好了。"
    assert response.autonomous_original_text == original
    assert event.extras["autonomous_original_text"] == original


@pytest.mark.asyncio
async def test_hook_ignores_normal_chat():
    plugin = make_plugin()
    event = make_event(False)
    response = make_response('{"message": "不该被改"}')
    await module.AutonomousCompanion.expose_only_visible_text(plugin, event, response)
    assert response.completion_text == '{"message": "不该被改"}'
    assert "autonomous_original_text" not in event.extras


@pytest.mark.asyncio
async def test_hook_respects_switch():
    plugin = make_plugin(expose_plugin_hooks=False)
    event = make_event(True)
    response = make_response('{"message": "不该被改"}')
    await module.AutonomousCompanion.expose_only_visible_text(plugin, event, response)
    assert response.completion_text == '{"message": "不该被改"}'


def test_notification_hooks_switch():
    assert make_plugin().notification_hooks is module.MAIN_AGENT_HOOKS
    assert make_plugin(expose_plugin_hooks=False).notification_hooks is None


@pytest.mark.asyncio
async def test_control_output_reads_original_without_repair():
    plugin = make_plugin()
    payload = json.dumps(
        {
            "message": "我把封面改好了。",
            "summary": "改封面",
            "continue_after_seconds": 0,
            "next_task": "",
        },
        ensure_ascii=False,
    )
    response = make_response("我把封面改好了。", autonomous_original_text=payload)
    result = await module.AutonomousCompanion.control_output(
        plugin, response, "provider", "action"
    )
    assert result["message"] == "我把封面改好了。"
    plugin.context.llm_generate.assert_not_called()


@pytest.mark.asyncio
async def test_control_output_still_repairs_plain_text():
    plugin = make_plugin()
    plugin.context.llm_generate = AsyncMock(
        return_value=make_response(
            json.dumps({"message": "好的", "summary": "说一句"}, ensure_ascii=False)
        )
    )
    response = make_response("不加 JSON 的普通回复")
    result = await module.AutonomousCompanion.control_output(
        plugin, response, "provider", "action"
    )
    assert result["message"] == "好的"
    plugin.context.llm_generate.assert_awaited_once()


def test_budget_tool_copies_background_flag():
    event = make_event(True)
    inner = module.FunctionTool(
        name="draw", description="draw", parameters={}, is_background_task=True
    )
    wrapped = module.BudgetTool(inner, event)
    assert wrapped.is_background_task is True
    plain = module.BudgetTool(
        module.FunctionTool(name="draw2", description="draw", parameters={}), event
    )
    assert plain.is_background_task is False
