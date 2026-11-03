"""Reference-based pending sharing for all registered work types."""

import hashlib
import time
import uuid
from pathlib import Path

from astrbot.api.star import StarTools
from astrbot.core.message.components import File, Image
from astrbot.core.message.message_event_result import MessageChain

from .library import read_library_image, read_library_text


def enqueue_work(state, identifier):
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
    if len(box) >= 20:
        retired = next((i for i in box if i["status"] in {"sent", "discarded"}), None)
        if retired is None:
            raise ValueError("成果箱已满，请先处理现有条目")
        box.remove(retired)
    snapshot = {"kind": work["kind"], "title": work["title"]}
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
        return MessageChain().message(item["content"])
    work = item.get("snapshot") or next(
        (w for w in state.get("work_library", []) if w["id"] == item["work_id"]), None
    )
    if work is None:
        raise ValueError("原作品已不在作品库中，不发送猜测内容")
    if work["kind"] == "image":
        if item.get("snapshot"):
            preview_state = {"work_library": [{**work, "id": item["work_id"]}]}
        else:
            preview_state = state
        data = await read_library_image(
            preview_state,
            item["work_id"],
            str(owner.config.get("comfyui_url", "http://127.0.0.1:8188")),
        )
        return MessageChain(chain=[Image.fromBase64(data.split(",", 1)[1])])
    if work["kind"] == "text":
        text = (
            work.get("content")
            if isinstance(work.get("content"), str)
            else Path(work["path"]).read_text(encoding="utf-8-sig")
            if item.get("snapshot")
            else read_library_text(state, work["id"])
        )
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
