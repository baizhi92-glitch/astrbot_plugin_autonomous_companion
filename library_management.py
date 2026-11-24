"""Reversible catalogue organization; source files remain untouched."""

import time
import uuid

from .library import text_content_hash


def organize_work(state, identifiers, archived):
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or any(not isinstance(i, str) for i in identifiers)
    ):
        raise ValueError("请选择作品")
    works = {w["id"]: w for w in state.get("work_library", [])}
    if any(i not in works for i in identifiers):
        raise ValueError("作品不存在，请刷新")
    marks = dict(state.get("library_archived", {}))
    for identifier in identifiers:
        work = works[identifier]
        keys = ["id:" + identifier]
        if work.get("content_hash"):
            keys.append("hash:" + work["content_hash"])
        for key in keys:
            if archived:
                marks[key] = time.time()
            else:
                marks.pop(key, None)
    state["library_archived"] = marks


def remove_work(state, identifier):
    """Forget a catalogue row and its grouped copies without touching sources.

    Args:
        state: Per-target persistent state.
        identifier: Existing catalogue row ID.

    Raises:
        ValueError: The row does not exist in this target's catalogue.
    """
    works = state.get("work_library", [])
    selected = next((w for w in works if w["id"] == identifier), None)
    if selected is None:
        raise ValueError("作品不存在，请刷新")
    identifiers = {identifier}
    digest = selected.get("content_hash") if selected.get("kind") == "text" else None
    if digest:
        # Match the dashboard's grouping; unrelated content at a new ID is not blocked.
        identifiers.update(
            w["id"]
            for w in works
            if w.get("kind") == "text" and w.get("content_hash") == digest
        )
    removed = state.setdefault("library_removed", {})
    removed.update(dict.fromkeys(identifiers, True))
    state["work_library"] = [w for w in works if w["id"] not in identifiers]
    archived = state.get("library_archived", {})
    for work_id in identifiers:
        archived.pop("id:" + work_id, None)
    if digest:
        archived.pop("hash:" + digest, None)


def add_text_work(state, title, content):
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("标题为1～200字符")
    if not isinstance(content, str) or not content.strip() or len(content) > 200000:
        raise ValueError("正文为1～200000字符")
    title = title.strip()
    digest = text_content_hash(content, title)
    existing = next(
        (w for w in state.get("work_library", []) if w.get("content_hash") == digest),
        None,
    )
    if existing:
        organize_work(state, [existing["id"]], False)
        return existing["id"]
    identifier = uuid.uuid4().hex[:20]
    state.setdefault("work_library", []).append(
        {
            "id": identifier,
            "kind": "text",
            "title": title,
            "inline_content": content,
            "content_hash": digest,
            "producer": "manual",
            "modified_at": time.time(),
        }
    )
    return identifier
