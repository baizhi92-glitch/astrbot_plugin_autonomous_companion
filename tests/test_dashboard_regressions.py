"""Isolated regressions for the embedded dashboard's secret and catalogue."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import library
from data.plugins.astrbot_plugin_autonomous_companion.page_api import CompanionPageAPI

ORIGIN = "test:FriendMessage:123"


class FakeConfig(dict):
    save_config = Mock()


@pytest.fixture
def dashboard():
    config = FakeConfig(dsh_api_key="opaque-configured-test-value")
    config.save_config = Mock()
    owner = SimpleNamespace(
        config=config,
        states={ORIGIN: {}},
        running={},
        chat_tasks={},
        revisions={},
        target_origins=lambda: [ORIGIN],
        new_state=dict,
        interval=lambda _state: 60,
        number=lambda _key, default, *_bounds: default,
        save=AsyncMock(),
        context=SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[]),
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value=None),
            ),
            get_platform_inst=lambda _: None,
            get_all_stars=list,
        ),
    )
    page = CompanionPageAPI(owner)
    page.library_checked = {ORIGIN: time.time()}  # No background refresh.
    return page, owner


@pytest.mark.asyncio
async def test_library_views_hash_local_images_without_mutating_catalogue(
    dashboard, tmp_path
):
    page, owner = dashboard
    one, two = tmp_path / "one.png", tmp_path / "two.bin"
    one.write_bytes(b"image data")
    two.write_bytes(b"image data")
    items = [
        {"id": "a", "kind": "image", "path": str(one)},
        {"id": "b", "kind": "image", "path": str(two)},
        {"id": "c", "kind": "image", "url": "https://example.invalid/remote"},
        {"id": "d", "kind": "text", "path": str(tmp_path / "absent.txt")},
        {"id": "e", "kind": "text", "inline_content": "private work body"},
    ]
    owner.states[ORIGIN]["work_library"] = items
    view = (await page.snapshot())["data"]["sessions"][0]["state"]["work_library"]
    assert [w["availability"] for w in view] == [
        "local",
        "local",
        "remote",
        "missing",
        "inline",
    ]
    assert view[0]["image_digest"] == view[1]["image_digest"] != ""
    assert "inline_content" not in view[-1]
    assert items[-1]["inline_content"] == "private work body"
    assert "availability" not in items[0]
    two.write_bytes(b"different image data")
    updated = (await page.snapshot())["data"]["sessions"][0]["state"]["work_library"]
    assert updated[1]["image_digest"] != view[1]["image_digest"]
    owner.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_dsh_secret_is_write_only_and_blank_patches_keep_it(dashboard):
    page, owner = dashboard
    initial = owner.config["dsh_api_key"]
    page.schema["dsh_api_key"]["default"] = "opaque-schema-test-value"
    snapshot = (await page.snapshot())["data"]
    assert "dsh_api_key" not in snapshot["config"]
    assert snapshot["schema"]["dsh_api_key"]["write_only"] is True
    assert "default" not in snapshot["schema"]["dsh_api_key"]
    assert initial not in json.dumps(snapshot)
    assert "opaque-schema-test-value" not in json.dumps(snapshot)

    await page.update_config({"enabled": False})
    await page.update_config({"enabled": True, "dsh_api_key": ""})
    await page.update_config({"dsh_api_key": " \t\n "})
    assert owner.config["enabled"] is True
    assert owner.config["dsh_api_key"] == initial
    with pytest.raises(ValueError):
        await page.update_config({"enabled": False, "dsh_api_key": None})
    assert owner.config["enabled"] is True

    new = "opaque-replacement-test-value"
    result = await page.update_config({"dsh_api_key": new})
    assert result == {"status": "ok", "data": {"saved": True}}
    assert owner.config["dsh_api_key"] == new
    assert new not in json.dumps(await page.snapshot())
    assert owner.config.save_config.call_count == 4


@pytest.mark.asyncio
async def test_catalogue_removal_survives_snapshot_receipt_replay_and_rescan(
    dashboard, tmp_path, monkeypatch
):
    page, owner = dashboard
    monkeypatch.setattr(
        library.StarTools, "get_data_dir", lambda _: tmp_path / "plugin"
    )
    owner.config["work_library_roots"] = [str(tmp_path)]
    doomed = tmp_path / "removed.txt"
    duplicate = tmp_path / "duplicate.txt"
    other = tmp_path / "other.txt"
    for path, content in (
        (doomed, "Same body"),
        (duplicate, "Same body"),
        (other, "Different body"),
    ):
        path.write_text(content, encoding="utf-8")
    state = owner.states[ORIGIN]
    state["saved_works"] = [{"ok": True, "path": str(doomed)}]
    assert await library.discover_text_works(owner, ORIGIN) == 3
    ids = {Path(work["path"]).name: work["id"] for work in state["work_library"]}
    assert (
        await page.perform(
            {"umo": ORIGIN, "action": "work_delete", "id": ids[doomed.name]}
        )
    )["data"] == {"deleted": True}
    owner.save.assert_awaited_once()

    # The persisted JSON record contains the tombstones, not removed files.
    owner.states[ORIGIN] = json.loads(json.dumps(state))
    snapshot = (await page.snapshot())["data"]["sessions"][0]["state"]
    assert [work["id"] for work in snapshot["work_library"]] == [ids[other.name]]
    await page.perform({"umo": ORIGIN, "action": "work_rescan"})
    state = owner.states[ORIGIN]
    assert [work["id"] for work in state["work_library"]] == [ids[other.name]]
    assert doomed.read_text(encoding="utf-8") == duplicate.read_text(encoding="utf-8")
    assert state["saved_works"] == [{"ok": True, "path": str(doomed)}]

    # Explicitly adding the same content as a new work is still allowed.
    await page.perform(
        {
            "umo": ORIGIN,
            "action": "work_add_text",
            "title": "New work",
            "content": "Same body",
        }
    )
    assert {work["producer"] for work in state["work_library"]} == {
        "safe_write",
        "manual",
    }


@pytest.mark.asyncio
async def test_removed_remote_image_is_not_backfilled(dashboard):
    page, owner = dashboard
    state = owner.states[ORIGIN]
    state["task_artifacts"] = [
        {"url": "http://example.invalid/view?filename=image.png"}
    ]
    assert library.backfill_library(state) == 1
    await page.perform(
        {"umo": ORIGIN, "action": "work_delete", "id": state["work_library"][0]["id"]}
    )
    assert library.backfill_library(state) == 0
    assert state["work_library"] == []
    assert len(state["task_artifacts"]) == 1


@pytest.mark.asyncio
async def test_removed_companion_copy_is_not_resynchronized(dashboard):
    page, owner = dashboard
    result = {
        "available": True,
        "works": [{"id": "one", "title": "Work", "content": "Test body"}],
    }
    reader = AsyncMock(return_value=result)
    meta = SimpleNamespace(
        name="fake-companion",
        activated=True,
        root_dir_name="astrbot_plugin_private_companion",
        star_cls=SimpleNamespace(
            extension_api=SimpleNamespace(export_text_work_library=reader)
        ),
    )
    owner.context.get_all_stars = lambda: [meta]
    owner.context.get_platform_inst = lambda _: SimpleNamespace(
        meta=lambda: SimpleNamespace(id="test", name="test"), client_self_id="bot"
    )
    owner.context.get_config = lambda _: {}
    action = {"umo": ORIGIN, "action": "companion_sync"}
    assert (await page.perform(action))["data"]["synced"] == 1
    state = owner.states[ORIGIN]
    await page.perform(
        {"umo": ORIGIN, "action": "work_delete", "id": state["work_library"][0]["id"]}
    )
    assert (await page.perform(action))["data"]["synced"] == 0
    assert state["work_library"] == []
    assert result["works"][0]["content"] == "Test body"


@pytest.mark.asyncio
async def test_dashboard_now_queues_check_without_erasing_last_inbound(dashboard):
    page, owner = dashboard
    owner.revisions[ORIGIN] = 3
    state = owner.states[ORIGIN]
    state["last_inbound"] = 123
    await page.perform({"umo": ORIGIN, "action": "now"})
    assert state["last_inbound"] == 123
    assert state["force_check_revision"] == 3
    assert state["next_due"] == 0
    await page.perform({"umo": ORIGIN, "action": "stop"})
    assert "force_check_revision" not in state
    assert state["last_inbound"] == 123
