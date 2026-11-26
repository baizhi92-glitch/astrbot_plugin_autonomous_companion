"""Reference-based sharing and its transport outcome bookkeeping."""

import base64
import hashlib
import time
import uuid
from pathlib import Path

from astrbot.api.star import StarTools
from astrbot.core.message.components import File, Image
from astrbot.core.message.message_event_result import MessageChain

from .creative_artifacts import (
    CREATIVE_ARTIFACT_MAX_BYTES,
    CREATIVE_ARTIFACT_TOOL,
    resolve_creative_artifact_path,
)
from .library import read_library_image, read_library_text


class DispatchOutcome:
    """Track attempted segments and transport results across interruptions."""

    def __init__(self) -> None:
        self.segments: list[MessageChain] = []
        self.sent: list[str] = []
        self.uncertain = False

    @property
    def complete(self) -> bool:
        return bool(self.segments) and len(self.sent) == len(self.segments)


def enqueue_work(state, identifier, *, origin=None):
    work = next(
        (w for w in state.get("work_library", []) if w["id"] == identifier), None
    )
    if work is None:
        raise ValueError("作品不存在，请刷新")
    box = state.setdefault("outbox", [])
    if any(
        item.get("work_id") == identifier
        and item["status"] in {"pending", "sending", "sent", "uncertain"}
        for item in box
    ):
        return
    retired = None
    if len(box) >= 20:
        retired = next((i for i in box if i["status"] in {"sent", "discarded"}), None)
        if retired is None:
            raise ValueError("成果箱已满，请先处理现有条目")
    snapshot = {"kind": work["kind"], "title": work["title"]}
    if work.get("producer") == CREATIVE_ARTIFACT_TOOL:
        path = resolve_creative_artifact_path(work.get("path"), origin)
        if not path.is_file() or path.stat().st_size > CREATIVE_ARTIFACT_MAX_BYTES:
            raise ValueError("创作产物缺失或超过分享限制")
    if isinstance(work.get("inline_content"), str):
        snapshot["content"] = work["inline_content"]
    elif work.get("path"):
        path = Path(work["path"])
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("原文件不存在或超过20MB")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        root = (
            Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
            / "outbox_files"
        )
        root.mkdir(parents=True, exist_ok=True)
        target = root / (digest + path.suffix)
        if not target.exists():
            target.write_bytes(raw)
        snapshot.update(path=str(target.resolve()), filename=path.name, sha256=digest)
    else:
        snapshot["url"] = work.get("url", "")
    if retired is not None:
        box.remove(retired)
    box.append(
        {
            "id": uuid.uuid4().hex[:12],
            "work_id": identifier,
            "kind": work["kind"],
            "title": work["title"],
            "content": "",
            "reason": "从作品库加入待分享",
            "status": "pending",
            "created_at": time.time(),
            "snapshot": snapshot,
        }
    )


async def output_chain(owner, state, item):
    """Resolve a registered reference immediately before dispatch; never execute files.

    Args:
        owner: Plugin runtime.
        state: Target library and outbox.
        item: Pending reference or legacy text entry.

    Returns:
        Message chain appropriate for the work type.

    Raises:
        ValueError: Reference vanished or cannot be safely sent.
    """
    if not item.get("work_id"):
        return MessageChain().message(str(item.get("content") or ""))
    snapshot = item.get("snapshot")
    work = snapshot or next(
        (w for w in state.get("work_library", []) if w["id"] == item["work_id"]), None
    )
    if work is None:
        raise ValueError("原作品已不在作品库中，不发送猜测内容")
    digest = work.get("sha256")
    path = Path(work["path"]) if work.get("path") else None
    if (snapshot and path is not None) or digest is not None:
        if path is None or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("原文件不存在或超过20MB")
        if not digest or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("成果快照校验失败，未发送")
    if work["kind"] == "image":
        preview_state = (
            {"work_library": [{**work, "id": item["work_id"]}]} if snapshot else state
        )
        data = await read_library_image(
            preview_state,
            item["work_id"],
            str(owner.config.get("comfyui_url", "http://127.0.0.1:8188")),
        )
        encoded = data.split(",", 1)[1]
        if digest and hashlib.sha256(base64.b64decode(encoded)).hexdigest() != digest:
            raise ValueError("成果快照校验失败，未发送")
        return MessageChain(chain=[Image.fromBase64(encoded)])
    if work["kind"] == "text":
        if isinstance(work.get("content"), str):
            text = work["content"]
        elif snapshot and path is not None:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("成果快照校验失败，未发送")
            text = raw.decode("utf-8-sig")
        else:
            text = read_library_text(state, work["id"])
        if len(text) <= 8000:
            return MessageChain().message(text)
        if not work.get("path"):
            root = (
                Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
                / "outbox_files"
            )
            root.mkdir(parents=True, exist_ok=True)
            path = root / (hashlib.sha256(text.encode()).hexdigest() + ".txt")
            path.write_text(text, encoding="utf-8")
            return MessageChain(
                chain=[File(name="作品正文.txt", file=str(path.resolve()))]
            )
    path = Path(work.get("path", ""))
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("原文件不存在或超过20MB")
    if (
        work.get("sha256")
        and hashlib.sha256(path.read_bytes()).hexdigest() != work["sha256"]
    ):
        raise ValueError("成果快照校验失败，未发送")
    return MessageChain(
        chain=[File(name=work.get("filename", path.name), file=str(path.resolve()))]
    )
