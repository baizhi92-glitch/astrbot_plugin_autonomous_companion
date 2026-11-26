import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import (
    compiled_work,
    dsh_worker,
    execution_proof,
    file_tools,
    works,
)


@pytest.mark.asyncio
async def test_dsh_nonzero_exit_with_stdout_is_a_failure(tmp_path, monkeypatch):
    owner = SimpleNamespace(config={"dsh_enabled": True}, number=lambda *args: 120)
    worker = dsh_worker.DSHWorker(owner)
    monkeypatch.setattr(worker, "resolve_command", lambda: "mock-dsh")
    monkeypatch.setattr(worker, "resolve_api_key", lambda: "")
    monkeypatch.setattr(worker, "_backup", Mock())
    proc = Mock(
        communicate=AsyncMock(return_value=(b"partial stdout", b"")), returncode=7
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = await worker.run("Write a report", str(tmp_path))
    assert result["ok"] is False
    assert result["output"] == "partial stdout"
    assert "7" in result["error"]
    assert worker.failed == 1
    assert worker.running == 0


@pytest.mark.asyncio
async def test_shell_cancellation_uses_process_cleanup(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def communicate():
        started.set()
        await asyncio.Future()

    proc = SimpleNamespace(communicate=communicate)
    cleanup = AsyncMock()
    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(file_tools, "_kill_tree", cleanup)
    task = asyncio.create_task(
        file_tools.run_command("echo harmless", tmp_path, 10, tmp_path, 3, False)
    )
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cleanup.assert_awaited_once_with(proc)


def test_media_delivery_count_does_not_complete_other_required_steps():
    event = SimpleNamespace(
        checkpoint_task="画张图并保存正文",
        media_deliveries=1,
        execution_contract={
            "mode": "action",
            "steps": [
                {"goal": "生成图像", "tools": ["generate"], "accept": "completed"},
                {"goal": "发送图片", "tools": ["send"], "accept": "success"},
                {"goal": "保存正文", "tools": ["save"], "accept": "success"},
            ],
        },
        action_receipts=[
            {"tool": "generate", "status": "verified"},
            {"tool": "send", "status": "success"},
        ],
    )
    verdict, reason = execution_proof.execution_verdict(event, "contract")
    assert verdict != "verified"
    assert "保存正文" in reason
    event.action_receipts.append({"tool": "save", "status": "success"})
    assert execution_proof.execution_verdict(event, "contract")[0] == "verified"


def test_one_receipt_is_not_reused_for_two_action_steps():
    event = SimpleNamespace(
        execution_contract={
            "mode": "action",
            "steps": [
                {"goal": "Save first", "tools": ["write", "copy"], "accept": "success"},
                {"goal": "Save second", "tools": ["write"], "accept": "success"},
            ],
        },
        action_receipts=[{"tool": "write", "status": "success"}],
    )
    assert execution_proof.execution_verdict(event, "contract")[0] == "unverified"
    event.action_receipts.append({"tool": "copy", "status": "success"})
    assert execution_proof.execution_verdict(event, "contract")[0] == "verified"


@pytest.mark.parametrize(
    ("number", "ordinal"),
    [("１", 1), ("十", 10), ("二〇二", 202), ("两百零一", 201), ("九百九十九", 999)],
)
def test_chapter_identity_valid(number, ordinal):
    assert compiled_work.chapter_identity(f"故事 · 第{number}章") == ("故事", ordinal)


@pytest.mark.parametrize("number", ["0", "零", "1十", "十十", "一百百", "999999999999"])
def test_chapter_identity_invalid(number):
    assert compiled_work.chapter_identity(f"故事 · 第{number}章") is None


@pytest.mark.asyncio
async def test_save_work_updates_only_owned_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(works.StarTools, "get_data_dir", lambda _: tmp_path)
    owner = SimpleNamespace(
        check_current=AsyncMock(),
        save=AsyncMock(),
        journal=Mock(),
        logger=Mock(),
        config={"companion_integration": False},
    )
    event = SimpleNamespace(owner=owner, unified_msg_origin="owner", state={})
    tool = works.SaveWorkTool(event)
    external = tmp_path / "external.md"
    external.write_text("original", encoding="utf-8")
    works.register_local_work(
        event.state,
        str(external),
        "autonomous_save_work",
        "External",
        status="drafting",
    )
    external_id = event.state["work_library"][0]["id"]
    for mode in ("draft", "final"):
        with pytest.raises(ValueError):
            await tool.call(None, "External", "wrong", mode=mode, work_id=external_id)
    assert external.read_text(encoding="utf-8") == "original"

    draft = json.loads(await tool.call(None, "Story", "draft body", mode="draft"))
    assert draft["work_id"]
    saved = json.loads(
        await tool.call(None, "Story", "final body", work_id=draft["work_id"])
    )
    assert saved["path"] == draft["path"]
    for mode in ("draft", "final"):
        with pytest.raises(ValueError):
            await tool.call(None, "Story", "wrong", mode=mode, work_id=draft["work_id"])
    reuse = json.loads(await tool.call(None, "Story", "final body"))
    new_draft = json.loads(await tool.call(None, "Story", "new draft", mode="draft"))
    assert reuse["path"] == saved["path"] != new_draft["path"]
