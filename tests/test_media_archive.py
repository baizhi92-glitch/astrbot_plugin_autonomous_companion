"""Regression coverage for durable media catalogue storage."""

import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import library


@pytest.mark.asyncio
async def test_media_copy_survives_receipt_replay_and_source_removal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(library.StarTools, "get_data_dir", lambda _: tmp_path / "store")
    source = tmp_path / "original video.mp4"
    source.write_bytes(b"first copy")
    state = {}
    receipt = json.dumps({"success": True, "outputs": {"video": {"path": str(source)}}})
    paths = []
    assert (
        library.catalogue_receipt(
            state, "generator", receipt, media_only=True, local_outputs=paths
        )
        == 1
    )
    work = state["work_library"][0]
    assert (
        await library.archive_local_media(
            state, paths, "generator", origin="test:FriendMessage:1"
        )
        == 1
    )
    first_copy = Path(work["path"])
    assert first_copy.read_bytes() == b"first copy"
    assert work["archive_status"] == "saved"
    assert work["title"] == source.name
    assert library.catalogue_receipt(state, "generator", receipt) == 0
    assert Path(work["path"]) == first_copy
    source.write_bytes(b"new output at reused path")
    assert (
        await library.archive_local_media(
            state, paths, "generator", origin="test:FriendMessage:1"
        )
        == 1
    )
    source.unlink()
    assert Path(work["path"]).read_bytes() == b"new output at reused path"
    assert first_copy.read_bytes() == b"first copy"


@pytest.mark.asyncio
async def test_empty_or_removed_media_is_never_marked_backed_up(tmp_path, monkeypatch):
    monkeypatch.setattr(library.StarTools, "get_data_dir", lambda _: tmp_path / "store")
    empty = tmp_path / "empty.wav"
    empty.touch()
    state = {}
    sources = [empty.as_uri(), "https://example.invalid/remote.mp4"]
    assert (
        await library.archive_local_media(
            state, sources, "generator", origin="test:FriendMessage:1"
        )
        == 0
    )
    (work,) = state["work_library"]
    assert work["archive_status"] == "failed"
    assert not (tmp_path / "store" / "media").exists()
    state["library_removed"] = {work["id"]: True}
    state["work_library"] = []
    empty.write_bytes(b"new data")
    assert (
        await library.archive_local_media(
            state, sources, "generator", origin="test:FriendMessage:1"
        )
        == 0
    )
    assert not state["work_library"]
