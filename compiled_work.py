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

CHAPTER_NUMBER = r"[零〇一二三四五六七八九十百两\d]+"


def chapter_identity(title):
    match = re.fullmatch(
        rf"(.+?)[·•：:\s_-]*第({CHAPTER_NUMBER})[章节段回](?:$|[\s·•：:—_-].*|《[^》]+》)",
        title.strip(),
    )
    if not match:
        return None
    series = match[1].rstrip("·•：: _-")
    number = match[2]
    if number.isdecimal() and len(number) <= 3:
        ordinal = int(number)
    elif re.fullmatch(r"[零〇一二两三四五六七八九]{1,3}", number):
        ordinal = int(
            number.translate(str.maketrans("零〇一二两三四五六七八九", "001223456789"))
        )
    else:
        # Match the positional units in order; reject repeated units and mixed Arabic digits.
        digit = "[一二两三四五六七八九]"
        units = re.fullmatch(
            rf"(?:(?P<hundreds>{digit})百(?:(?:[零〇](?P<ones>{digit}))|(?P<tens>{digit})十(?P<tail>{digit})?)?|(?P<small>{digit})?十(?P<last>{digit})?)",
            number,
        )
        if not units:
            return None
        digits = str.maketrans("一二两三四五六七八九", "1223456789")
        ordinal = (
            100 * int((units["hundreds"] or "0").translate(digits))
            + 10
            * int(
                (
                    units["tens"]
                    or units["small"]
                    or ("0" if units["hundreds"] else "1")
                ).translate(digits)
            )
            + int(
                (units["ones"] or units["tail"] or units["last"] or "0").translate(
                    digits
                )
            )
        )
    return (series, ordinal) if series and 1 <= ordinal <= 999 else None


def draft_sections(series, content):
    """Split an existing manuscript at numbered Markdown chapter headings.

    Args:
        series: Registered manuscript title.
        content: Manuscript body after removing its catalogue title.

    Returns:
        Tuples of ordinal, original heading, and chapter body.
    """
    headings = list(
        re.finditer(
            rf"(?m)^#{{2,6}}[ \t]+(第{CHAPTER_NUMBER}[章节段回][^\n]*)$", content
        )
    )
    if not headings or content[: headings[0].start()].strip() not in {
        "",
        f"# {series}",
    }:
        return []
    sections = []
    for index, heading in enumerate(headings):
        label = heading[1].strip()
        identity = chapter_identity(f"{series}·{label}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        body = content[heading.end() : end].strip()
        if not identity or not body:
            return []
        sections.append((identity[1], label, body))
    return sections


def compile_chapters(state, umo, title, identifiers=None, *, finished_prefix=False):
    """Resolve chapters and save an idempotent edition.

    Args:
        state: Per-conversation work catalogue.
        umo: Conversation origin used to scope saved files.
        title: Series title or explicit edition title.
        identifiers: Optional list of source work IDs.
        finished_prefix: Collect consecutive finalized chapters without ending the series.

    Returns:
        The save receipt, or None when an automatic collection is not needed yet.

    Raises:
        ValueError: A source is invalid, missing, or conflicts with another version.
        OSError: An edition could not be written or verified.
    """
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
        if item.get("kind") != "text" or item.get("compilation"):
            continue
        if finished_prefix and (
            item.get("status") != "finished"
            or item.get("producer") != "autonomous_save_work"
        ):
            continue
        if selected is not None:
            if item["id"] not in selected:
                continue
        elif (chapter[0] if chapter else item.get("title")) != title:
            continue
        content = normalized_work_body(
            read_library_text(state, item["id"], origin=umo), item["title"]
        )
        if chapter and not content:
            raise ValueError("章节正文为空")
        sections = (
            [(chapter[1], item["title"], content)]
            if chapter
            else draft_sections(title, content)
            if item["title"] == title
            else []
        )
        if sections:
            candidates.append((item, chapter[0] if chapter else title, sections))
    if selected is not None and {item["id"] for item, _, _ in candidates} != selected:
        raise ValueError("请选择带编号的章节，或标题一致且正文有编号小标题的合稿")
    if len({series for _, series, _ in candidates}) > 1:
        raise ValueError("不能将不同系列的章节合并为一篇")
    prefix_end = 0
    if finished_prefix:
        available = {n for _, _, sections in candidates for n, _, _ in sections}
        while prefix_end + 1 in available:
            prefix_end += 1
        if prefix_end < 2 or any(
            row.get("compilation")
            and row.get("series_title") == title
            and (
                row.get("chapter_count", 0) > prefix_end
                if row.get("finished_chapter_collection")
                else row.get("status") != "drafting"
            )
            for row in state["work_library"]
        ):
            return None
    by_number = {}
    for item, _, sections in candidates:
        for ordinal, heading, content in sections:
            if finished_prefix and ordinal > prefix_end:
                continue
            fingerprint = text_content_hash(content)
            if ordinal in by_number:
                previous = by_number[ordinal]
                if previous[2] != fingerprint:
                    raise ValueError(
                        f"第{ordinal}章存在不同版本，请勾选需要的合稿和章节"
                    )
                previous[3].append(item["id"])
            else:
                by_number[ordinal] = [heading, content, fingerprint, [item["id"]]]
    numbers = sorted(by_number)
    if len(numbers) < 2:
        raise ValueError("至少需要两章正文才能合并")
    if any(number != expected for expected, number in enumerate(numbers, 1)):
        raise ValueError("章节缺号或不是从第一章开始，请补齐后合并")
    body = "\n\n".join(by_number[n][0] + "\n\n" + by_number[n][1] for n in numbers)
    drafting = any(item.get("status") == "drafting" for item, _, _ in candidates)
    edition_title = title + (
        " · 已定稿章节合订"
        if finished_prefix
        else " · 合订草稿"
        if drafting
        else " · 合订终稿"
    )
    text = edition_title + "\n\n" + body
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
        raise OSError("合订正文读回校验失败")
    chapter_ids = list(
        dict.fromkeys(identifier for n in numbers for identifier in by_number[n][3])
    )
    previous = next(
        (
            row
            for row in state.get("work_library", [])
            if row.get("path") == str(path.resolve())
        ),
        None,
    )
    unchanged_collection = bool(
        finished_prefix
        and previous
        and previous.get("finished_chapter_collection")
        and set(previous.get("chapter_ids", [])) == set(chapter_ids)
    )
    register_local_work(
        state,
        str(path.resolve()),
        "chapter_compilation",
        edition_title,
        status="drafting" if drafting else "finished" if not finished_prefix else None,
    )
    work = next(
        (w for w in state["work_library"] if w.get("path") == str(path.resolve())),
        None,
    )
    if work is None:
        raise ValueError("该合订版本已移除，未自动恢复")
    work.update(
        compilation=True,
        chapter_ids=chapter_ids,
        chapter_count=len(numbers),
        series_title=title,
        finished_chapter_collection=finished_prefix,
    )
    if not drafting and not unchanged_collection:
        from .library_management import organize_work

        superseded = [
            row["id"]
            for row in state["work_library"]
            if row.get("series_title") == title
            and row.get("finished_chapter_collection")
            and row["id"] != work["id"]
            and (not finished_prefix or row.get("chapter_count", 0) <= len(numbers))
        ]
        source_ids = set(work["chapter_ids"]) | set(superseded)
        organize_work(
            state,
            [row["id"] for row in state["work_library"] if row["id"] in source_ids],
            True,
        )
    return {
        "saved": True,
        "id": work["id"],
        "path": str(path.resolve()),
        "chapters": len(numbers),
        "status": work.get("status"),
        "changed": not unchanged_collection,
    }


def refresh_finished_collection(state, umo, series, logger):
    """Refresh a collection without turning an optional merge into a save failure.

    Args:
        state: Per-conversation work catalogue.
        umo: Conversation origin.
        series: Name of the parent work.
        logger: Logger for conflicts and file errors.

    Returns:
        User-facing collection status and chapter count when available.
    """
    try:
        edition = compile_chapters(state, umo, series, finished_prefix=True)
    except (ValueError, OSError) as exc:
        logger.warning("Could not collect finalized chapters for %s: %s", series, exc)
        return {
            "status": "blocked",
            "reason": str(exc) if isinstance(exc, ValueError) else "无法保存合订文件",
        }
    if edition is None:
        return {"status": "not_ready"}
    return {
        "status": "updated" if edition["changed"] else "unchanged",
        "chapters": edition["chapters"],
    }
