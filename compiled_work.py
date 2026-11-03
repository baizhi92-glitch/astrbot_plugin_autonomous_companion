"""Compile registered chapters without modifying or deleting source works."""

import hashlib
import re
from pathlib import Path

from astrbot.api.star import StarTools

from .library import (
    normalized_work_body,
    read_library_text,
    register_local_work,
    text_content_hash,
)


def chapter_identity(title):
    match = re.fullmatch(
        r"(.+?)[·•：:\s_-]*第([零〇一二三四五六七八九十百两\d]+)[章节段回](?:\s*)",
        title.strip(),
    )
    if not match:
        return None
    number = match[2]
    if number.isdigit():
        ordinal = int(number)
    else:
        digits = dict(
            zip("零〇一二三四五六七八九两", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2])
        )
        ordinal, current = 0, 0
        for character in number:
            if character in digits:
                current = digits[character]
            else:
                ordinal += (current or 1) * {"十": 10, "百": 100}[character]
                current = 0
        ordinal += current
    return match[1].rstrip("·•：: _-"), ordinal


def compile_chapters(state, umo, title, identifiers=None):
    """Resolve chapters, reject ambiguous versions, and save an idempotent edition."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("请填写整篇标题")
    title = title.strip()
    if identifiers is not None and (
        not isinstance(identifiers, list)
        or not identifiers
        or any(not isinstance(i, str) for i in identifiers)
    ):
        raise ValueError("请选择要合并的章节")
    selected = set(identifiers) if identifiers is not None else None
    candidates = []
    for item in state.get("work_library", []):
        chapter = chapter_identity(item.get("title", ""))
        if (
            item.get("kind") == "text"
            and chapter
            and (
                item["id"] in selected if selected is not None else chapter[0] == title
            )
        ):
            candidates.append((item, chapter))
    if selected is not None and {item["id"] for item, _ in candidates} != selected:
        raise ValueError("所选作品必须是带第X章/节/段编号的文本章节")
    if len({identity[0] for _, identity in candidates}) > 1:
        raise ValueError("不能将不同系列的章节合并为一篇")
    by_number = {}
    for item, (_, ordinal) in candidates:
        content = normalized_work_body(
            read_library_text(state, item["id"]), item["title"]
        )
        if not content:
            raise ValueError("章节正文为空")
        fingerprint = text_content_hash(content)
        if ordinal in by_number:
            previous = by_number[ordinal]
            if previous[2] != fingerprint:
                raise ValueError(f"第{ordinal}章存在不同版本，请勾选需要的版本后合并")
            previous[3].append(item["id"])
        else:
            by_number[ordinal] = [item, content, fingerprint, [item["id"]]]
    numbers = sorted(by_number)
    if len(numbers) < 2:
        raise ValueError("至少需要两章正文才能合并")
    if numbers != list(range(1, numbers[-1] + 1)):
        raise ValueError("章节缺号或不是从第一章开始，请补齐后合并")
    body = "\n\n".join(
        by_number[n][0]["title"] + "\n\n" + by_number[n][1] for n in numbers
    )
    text = title + " · 合订终稿\n\n" + body
    if len(text) > 500000:
        raise ValueError("合订正文超过50万字符，请分卷整理")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    scope = hashlib.sha256(umo.encode()).hexdigest()[:16]
    root = (
        Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        / "works"
        / scope
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("compiled-" + digest + ".txt")
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    if path.read_text(encoding="utf-8") != text:
        raise OSError("合订终稿读回校验失败")
    register_local_work(
        state, str(path.resolve()), "chapter_compilation", title + " · 合订终稿"
    )
    work = next(
        w for w in state["work_library"] if w.get("path") == str(path.resolve())
    )
    work.update(
        compilation=True,
        chapter_ids=[identifier for n in numbers for identifier in by_number[n][3]],
        chapter_count=len(numbers),
        series_title=title,
    )
    from .library_management import organize_work

    organize_work(state, work["chapter_ids"], True)
    return {
        "saved": True,
        "id": work["id"],
        "path": str(path.resolve()),
        "chapters": len(numbers),
    }
