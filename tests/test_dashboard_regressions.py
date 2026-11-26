"""Isolated regressions for the embedded dashboard's secret and catalogue."""

import hashlib
import json
import time
import wave
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


def test_library_view_detects_manuscript_versions_without_changing_source(
    dashboard, tmp_path
):
    page, _ = dashboard
    state = {}
    for i, text in enumerate(
        (
            "# 故事\n\n## 第一节 起点\n正文\n## 第二节\n正文",
            "## 第一节\n改稿\n## 第二节\n正文\n## 第三节\n正文",
            "同名但没有章节的独立作品",
        )
    ):
        path = tmp_path / f"{i}.md"
        path.write_text("故事\n\n" + text, encoding="utf-8")
        library.register_local_work(
            state, str(path), "autonomous_save_work", "故事", status="drafting"
        )
    rows = state["work_library"]
    for _ in range(2):
        views = page.inspect_library_views({ORIGIN: [dict(row) for row in rows]})[
            ORIGIN
        ]
        assert [row.get("manuscript_sections") for row in views] == [
            [1, 2],
            [1, 2, 3],
            None,
        ]
    assert all("manuscript_sections" not in row for row in rows)
    Path(rows[1]["path"]).write_text("故事\n\n无章节的新稿", encoding="utf-8")
    views = page.inspect_library_views({ORIGIN: [dict(row) for row in rows]})[ORIGIN]
    assert "manuscript_sections" not in views[1]


def test_library_views_report_pcm_wav_duration_without_playback(dashboard, tmp_path):
    page, _ = dashboard
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 84000)
    row = {"kind": "file", "path": str(path)}
    for _ in range(2):
        view = page.inspect_library_views({ORIGIN: [dict(row)]})[ORIGIN][0]
        assert view["media_duration"] == 1.75
        assert view["bytes"] == path.stat().st_size
    path.write_bytes(b"not a WAV")
    assert (
        "media_duration"
        not in page.inspect_library_views({ORIGIN: [dict(row)]})[ORIGIN][0]
    )


def test_library_preserves_more_than_one_hundred_sources(tmp_path):
    state = {}
    for i in range(105):
        path = tmp_path / f"{i}.txt"
        path.write_text(f"Original {i}", encoding="utf-8")
        assert library.register_local_work(state, str(path), "safe_write")
    for i in range(105):
        assert library.catalogue_receipt(
            state,
            "run_template",
            json.dumps(
                {
                    "status": "completed",
                    "outputs": {
                        "image": {
                            "type": "image",
                            "url": f"https://example.test/{i}.png",
                        }
                    },
                }
            ),
        )
    assert len(state["work_library"]) == 210
    assert (
        library.read_library_text(state, state["work_library"][0]["id"]) == "Original 0"
    )


@pytest.mark.asyncio
async def test_creative_artifact_access_is_scoped_and_receipt_replay_is_ignored(
    dashboard, tmp_path, monkeypatch
):
    from data.plugins.astrbot_plugin_autonomous_companion.creative_artifacts import (
        CREATIVE_ARTIFACT_TOOL,
    )

    page, owner = dashboard
    monkeypatch.setattr(library.StarTools, "get_data_dir", lambda _: tmp_path)
    scope = hashlib.sha256(ORIGIN.encode()).hexdigest()[:16]
    folder = tmp_path / "works" / scope / "creative"
    folder.mkdir(parents=True)
    file = folder / ("artifact-" + "b" * 32 + ".json")
    file.write_text('{"a": 1}', encoding="utf-8")
    state = owner.states[ORIGIN]
    assert not library.catalogue_receipt(
        state,
        CREATIVE_ARTIFACT_TOOL,
        json.dumps({"saved": True, "output_path": str(file)}),
    )
    assert not library.register_local_work(state, str(file), "safe_write")
    assert library.register_local_work(
        state, str(file), CREATIVE_ARTIFACT_TOOL, "Structured work"
    )
    row = state["work_library"][0]
    for action in ("work_read", "work_download", "work_to_outbox"):
        response = await page.perform(
            {"action": action, "umo": ORIGIN, "id": row["id"]}
        )
        assert response["status"] == "ok"
    with pytest.raises(ValueError):
        library.read_library_text(state, row["id"], origin="other:FriendMessage:123")
    with pytest.raises(ValueError):
        library.download_library_file(
            state, row["id"], origin="other:FriendMessage:123"
        )


@pytest.mark.asyncio
async def test_draft_versions_restore_and_report_actual_finalize(
    dashboard, tmp_path, monkeypatch
):
    from data.plugins.astrbot_plugin_autonomous_companion.work_versions import (
        replace_work_text,
    )

    page, owner = dashboard
    owner.logger = Mock()
    monkeypatch.setattr(library.StarTools, "get_data_dir", lambda _: tmp_path)
    scope = hashlib.sha256(ORIGIN.encode()).hexdigest()[:16]
    drafts = tmp_path / "works" / scope / "drafts"
    drafts.mkdir(parents=True)
    path = drafts / ("draft-" + "a" * 32 + ".md")
    first = "长篇·第一章 出发\n\n上一版"
    second = "长篇·第一章 出发\n\n本版"
    path.write_bytes(first.encode())
    state = owner.states[ORIGIN]
    assert library.register_local_work(
        state, str(path), "autonomous_save_work", "长篇·第一章 出发", status="drafting"
    )
    identifier = state["work_library"][0]["id"]
    replace_work_text(path, second)
    versions = (
        await page.perform({"umo": ORIGIN, "action": "work_versions", "id": identifier})
    )["data"]["versions"]
    assert len(versions) == 1
    await page.perform(
        {
            "umo": ORIGIN,
            "action": "work_restore_version",
            "id": identifier,
            "version_id": versions[0]["id"],
        }
    )
    assert path.read_text(encoding="utf-8") == first
    with pytest.raises(ValueError):
        await page.perform(
            {
                "umo": ORIGIN,
                "action": "work_restore_version",
                "id": identifier,
                "version_id": "../draft",
            }
        )
    result = await page.perform(
        {"umo": ORIGIN, "action": "work_finalize_draft", "id": identifier}
    )
    assert result["data"]["compilation"] == {"status": "not_ready"}
    assert state["work_library"][0]["status"] == "finished"


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
