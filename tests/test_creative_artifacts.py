"""Test explicit, private creation of controlled work formats."""

import asyncio
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import creative_artifacts, library
from data.plugins.astrbot_plugin_autonomous_companion.execution_proof import (
    receipt_status,
)
from data.plugins.astrbot_plugin_autonomous_companion.tests.test_autonomous import (
    make_plugin,
)

ORIGIN = "test:FriendMessage:123"
TOOL = creative_artifacts.CREATIVE_ARTIFACT_TOOL


@pytest.fixture
def event(tmp_path, monkeypatch):
    monkeypatch.setattr(
        creative_artifacts.StarTools, "get_data_dir", lambda _: tmp_path
    )
    return SimpleNamespace(
        unified_msg_origin=ORIGIN,
        state={},
        owner=SimpleNamespace(
            check_current=AsyncMock(), save=AsyncMock(), journal=Mock()
        ),
        send=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("format", "content", "extension"),
    [
        ("markdown", "# 剧本\n\n**人物**\r\n<script>alert(1)</script>", ".md"),
        ("json", '{"星球":["甲","乙"]}\n', ".json"),
        ("csv", 'actor,"line, first"\n帆,你好\n', ".csv"),
        ("source", 'print("not executed")\n', ".txt"),
    ],
)
async def test_create_writes_verified_and_indexed_file(
    event, format, content, extension
):
    receipt_text = await creative_artifacts.SaveCreativeArtifactTool(event).call(
        None, "作品", format, content
    )
    receipt = json.loads(receipt_text)
    target = Path(receipt["output_path"])
    assert creative_artifacts.resolve_creative_artifact_path(target, ORIGIN) == target
    assert target.suffix == extension
    assert receipt["bytes"] == target.stat().st_size
    assert receipt_status(TOOL, receipt_text) == "verified"
    raw = target.read_bytes().decode("utf-8")
    if format == "csv":
        assert list(csv.reader(io.StringIO(raw))) == list(
            csv.reader(io.StringIO(content))
        )
        assert raw.startswith('"actor","line, first"')
    else:
        assert raw == content
    (item,) = event.state["work_library"]
    assert (item["id"], item["producer"], item["status"]) == (
        receipt["work_id"],
        TOOL,
        "finished",
    )
    assert item["kind"] == "text"
    assert library.catalogue_receipt(event.state, TOOL, receipt_text) == 0
    assert "outbox" not in event.state
    event.send.assert_not_awaited()
    event.owner.save.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("format", "content"),
    [
        ("svg", "<svg/>"),
        ("wav", "data:audio/wav;base64,AAAA"),
        ("video", "AAAA"),
        ("html", "<script>alert(1)</script>"),
        ("markdown", "   "),
        ("markdown", "a\x00b"),
        pytest.param(
            "source",
            "雪" * (creative_artifacts.CREATIVE_ARTIFACT_MAX_BYTES // 3 + 1),
            id="oversized-utf8",
        ),
        ("json", '{"v":NaN}'),
        ("json", '{"v":]'),
        ("csv", 'name,"  =SUM(1,2)"'),
        ("csv", 'name,"\ufeff＠SUM(1,2)"'),
        ("csv", 'name,"\t+2"'),
        ("csv", 'name,"unclosed'),
        pytest.param(
            "csv",
            "x," * (creative_artifacts.CREATIVE_ARTIFACT_MAX_BYTES // 3),
            id="oversized-normalized-csv",
        ),
    ],
)
async def test_invalid_content_has_no_side_effects(event, tmp_path, format, content):
    with pytest.raises(ValueError):
        await creative_artifacts.SaveCreativeArtifactTool(event).call(
            None, "标题", format, content
        )
    assert not list(tmp_path.iterdir())
    assert event.state == {}
    event.owner.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_creation_does_not_accept_paths_and_honors_current_turn(event, tmp_path):
    tool = creative_artifacts.SaveCreativeArtifactTool(event)
    with pytest.raises(TypeError):
        await tool.call(None, "title", "source", "content", path=str(tmp_path))
    event.owner.check_current.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await tool.call(None, "title", "source", "content")
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_fixed_scope_exclusive_creation_and_cross_origin_rejection(
    event, tmp_path
):
    tool = creative_artifacts.SaveCreativeArtifactTool(event)
    first, second = [
        json.loads(await tool.call(None, "../outside.html", "source", content))
        for content in ("first", "second")
    ]
    a = Path(first["output_path"])
    b = Path(second["output_path"])
    assert a != b and a.parent == b.parent and a.read_text() == "first"
    assert not (tmp_path / "outside.html").exists()
    with pytest.raises(ValueError):
        creative_artifacts.resolve_creative_artifact_path(a, "test:FriendMessage:456")
    with pytest.raises(ValueError):
        creative_artifacts.resolve_creative_artifact_path(
            a.with_suffix(".html"), ORIGIN
        )


@pytest.mark.asyncio
async def test_scope_rejects_hardlinked_file(event, tmp_path):
    receipt = json.loads(
        await creative_artifacts.SaveCreativeArtifactTool(event).call(
            None, "title", "source", "story"
        )
    )
    target = Path(receipt["output_path"])
    external = tmp_path / "unrelated.txt"
    external.write_text("unrelated")
    target.unlink()
    try:
        target.hardlink_to(external)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Hardlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="专属目录"):
        creative_artifacts.resolve_creative_artifact_path(target, ORIGIN)


@pytest.mark.asyncio
async def test_creation_rejects_redirected_work_folder(event, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (tmp_path / "works").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="专属目录"):
        await creative_artifacts.SaveCreativeArtifactTool(event).call(
            None, "title", "source", "story"
        )
    assert not list(elsewhere.iterdir())
    assert event.state == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "task", "manual", "allowlist", "message", "enabled", "sent"),
    [
        ("create", "保存一组诗歌为 Markdown", False, [], "", True, False),
        (
            "create",
            "保存一组诗歌为 Markdown",
            False,
            [],
            "在作品库里留了一组诗。",
            True,
            True,
        ),
        ("tools", "写 Markdown 诗歌", True, [], "", False, False),
        ("create", "继续和对方聊聊天", False, [], "", False, False),
        ("chat", "说说对春风的感受", False, [], "", False, False),
        ("tools", "整理项目", False, [], "", False, False),
        ("create", "保存第四章草稿", False, [], "", False, False),
        ("create", "写 Markdown 诗歌", False, ["other_tool"], "", False, False),
    ],
)
async def test_autonomous_round_exposes_tool_only_in_scope_and_preserves_sharing(
    event, action, task, manual, allowlist, message, enabled, sent
):
    plugin, state, turn_event = make_plugin()
    turn_event.owner = plugin
    plugin.config.update(task_completion_message=True, allowed_tools=allowlist)
    if manual:
        state["manual_plan"] = task
    plugin.context.llm_generate.return_value = SimpleNamespace(
        completion_text=json.dumps({"action": action, "task": task})
    )

    async def execute(**kwargs):
        tool = kwargs["tools"].get_tool(TOOL)
        assert (tool is not None) is enabled
        if tool:
            run_context = SimpleNamespace(
                context=SimpleNamespace(event=turn_event), tool_call_timeout=5
            )
            (result,) = [
                result
                async for result in tool.call(
                    run_context, title="河流", format="markdown", content="# 河流"
                )
            ]
            assert receipt_status(TOOL, result.content[0].text) == "verified"
            assert turn_event.action_receipts[-1]["status"] == "verified"
        return SimpleNamespace(
            completion_text=json.dumps({"message": message, "summary": "本轮摘要"})
        )

    plugin.context.tool_loop_agent.side_effect = execute
    await plugin.turn(ORIGIN)
    assert bool(state.get("work_library")) is enabled
    assert turn_event.send.await_count == int(sent)
    if message and sent:
        assert turn_event.send.call_args.args[0].get_plain_text() == message


@pytest.mark.asyncio
async def test_autonomous_artifact_tool_notices_are_private():
    plugin, _, event = make_plugin()
    plugin.context.get_config = lambda *_: {
        "provider_settings": {"show_tool_call_result": True}
    }
    plugin.deliver = AsyncMock()
    await plugin.tool_notice(event, TOOL, "开始调用")
    await plugin.tool_notice(event, TOOL, "返回结果", "stored locally")
    plugin.deliver.assert_not_awaited()
    event.manual_task = True
    await plugin.tool_notice(event, TOOL, "开始调用")
    plugin.deliver.assert_awaited_once()
