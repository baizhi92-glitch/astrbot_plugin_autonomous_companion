"""Isolated regressions for indexed local media previews."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrbot.dashboard.api.files import router
from astrbot.dashboard.services.file_service import FileService
from data.plugins.astrbot_plugin_autonomous_companion import page_api

ORIGIN = "test:FriendMessage:123"
OTHER = "test:FriendMessage:456"


def test_snapshot_media_metadata_does_not_change_index(tmp_path):
    mp3 = tmp_path / "sample.MP3"
    mp3.write_bytes(b"audio")
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"audio")
    big = tmp_path / "large.webm"
    big.write_bytes(b"too large for the patched limit")
    missing = tmp_path / "missing.mp4"
    rows = [
        {"kind": "file", "path": str(mp3)},
        {"kind": "audio", "path": str(wav)},
        {"kind": "file", "path": str(missing)},
        {"kind": "video", "path": str(big)},
        {"kind": "file", "url": "https://example.invalid/video.mp4"},
    ]
    page = page_api.CompanionPageAPI(None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(page_api, "MEDIA_PREVIEW_MAX_BYTES", 16)
        view = page.inspect_library_views({ORIGIN: [dict(row) for row in rows]})[ORIGIN]
    assert [
        (w["kind"], w["media_kind"], w["availability"], w.get("media_preview"))
        for w in view
    ] == [
        ("file", "audio", "local", "ready"),
        ("audio", "audio", "local", "ready"),
        ("file", "video", "missing", None),
        ("video", "video", "local", "too_large"),
        ("file", None, "unavailable", None),
    ]
    assert all("media_kind" not in w and "availability" not in w for w in rows)


@pytest.mark.asyncio
async def test_preview_rejects_unknown_session_remote_missing_and_oversized(
    tmp_path, monkeypatch
):
    large = tmp_path / "large.wav"
    large.write_bytes(b"oversize")
    directory = tmp_path / "directory.mp4"
    directory.mkdir()
    empty = tmp_path / "empty.mp4"
    empty.touch()
    rows = [
        {"id": "missing", "path": str(tmp_path / "missing.mp4")},
        {"id": "remote", "url": "https://example.invalid/audio.mp3"},
        {"id": "url-path", "path": "https://example.invalid/audio.mp3"},
        {"id": "relative", "path": "audio.mp3"},
        {"id": "network", "path": r"\\server\share\audio.mp3"},
        {"id": "unsupported", "path": str(tmp_path / "file.exe")},
        {"id": "large", "path": str(large)},
        {"id": "directory", "path": str(directory)},
        {"id": "empty", "path": str(empty)},
    ]
    register = Mock(
        side_effect=AssertionError("Invalid media must not receive a token")
    )
    monkeypatch.setattr(page_api.file_token_service, "register_file", register)
    monkeypatch.setattr(page_api, "MEDIA_PREVIEW_MAX_BYTES", 4)
    owner = SimpleNamespace(
        running={},
        states={ORIGIN: {"work_library": [{"kind": "file", **row} for row in rows]}},
        target_origins=lambda: [ORIGIN, OTHER],
    )
    page = page_api.CompanionPageAPI(owner)
    for session, identifier in [(ORIGIN, row["id"]) for row in rows] + [
        (OTHER, "large"),
        ("test:FriendMessage:unknown", "large"),
        (ORIGIN, ["large"]),
        (ORIGIN, "unindexed"),
    ]:
        with pytest.raises(ValueError):
            await page.perform(
                {
                    "umo": session,
                    "action": "work_media",
                    "id": identifier,
                    "path": str(large),
                }
            )
    register.assert_not_called()
    assert OTHER not in owner.states


def test_media_uses_existing_repeatable_range_endpoint(tmp_path, monkeypatch):
    audio = (tmp_path / "sample.mp3").resolve()
    audio.write_bytes(b"0123456789")
    owner = SimpleNamespace(
        running={},
        target_origins=lambda: [ORIGIN],
        states={
            ORIGIN: {
                "work_library": [{"id": "audio", "kind": "file", "path": str(audio)}]
            }
        },
    )
    # Keep the token registry isolated while exercising the actual file router.
    tokens = type(page_api.file_token_service)()
    monkeypatch.setattr(page_api, "file_token_service", tokens)
    monkeypatch.setattr(
        "astrbot.dashboard.services.file_service.file_token_service", tokens
    )
    page = page_api.CompanionPageAPI(owner)
    app = FastAPI()
    app.state.services = SimpleNamespace(files=FileService())
    app.include_router(router, prefix="/api/v1")

    @app.post("/issue-test-token")
    async def issue_test_token():
        return await page.perform(
            {"umo": ORIGIN, "action": "work_media", "id": "audio", "path": "ignored"}
        )

    with TestClient(app) as client:
        result = client.post("/issue-test-token").json()
        assert result["status"] == "ok"
        assert set(result["data"]) == {"token"}
        path = "/api/v1/files/tokens/" + result["data"]["token"]
        for interval, expected in [("bytes=1-3", b"123"), ("bytes=8-", b"89")]:
            response = client.get(path, headers={"Range": interval})
            assert response.status_code == 206
            assert response.content == expected
            assert response.headers["content-type"] == "audio/mpeg"
        assert client.get("/api/v1/files/tokens/unknown").status_code == 404
