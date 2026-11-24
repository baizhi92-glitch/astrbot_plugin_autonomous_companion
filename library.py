"""Catalogue actual outputs from receipts rather than model-authored summaries."""

import asyncio
import base64
import hashlib
import json
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp

from astrbot.api.star import StarTools
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".log",
}
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".css",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".c",
    ".cpp",
    ".h",
    ".java",
    ".go",
    ".rs",
    ".sql",
    ".vue",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | {
    ".mp3",
    ".wav",
    ".ogg",
    ".flac",
    ".m4a",
    ".aac",
    ".opus",
    ".amr",
    ".silk",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
}
MEDIA_ARCHIVE_LIMIT = 100 * 1024 * 1024


def file_kind(path):
    suffix = Path(path).suffix.lower()
    return (
        "code"
        if suffix in CODE_EXTENSIONS
        else "text"
        if suffix in TEXT_EXTENSIONS
        else "image"
        if suffix in IMAGE_EXTENSIONS
        else "file"
    )


def normalized_work_body(content, title=""):
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    while title:
        first, separator, rest = content.partition("\n")
        if not separator or first.lstrip("# ").strip() != title.strip():
            break
        content = rest.strip()
    return content


def text_content_hash(content, title=""):
    return hashlib.sha256(
        normalized_work_body(content, title).encode("utf-8")
    ).hexdigest()


def register_local_work(state, path, producer, title="", status=None):
    """Index a real absolute output file, never execute or render source as HTML.

    Args:
        state: Per-target catalogue.
        path: Path supplied by an actual producer receipt.
        producer: Originating tool or registered output source.
        title: Optional display title.
        status: Optional lifecycle state ("drafting"/"finished"). Omitted keeps
            an existing entry's status and defaults new entries to "recorded",
            so receipt replays never downgrade a draft or a finished work.

    Returns:
        Whether a new catalogue item was created.
    """
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    target = Path(path)
    if not target.is_file():
        return False
    source = str(target.resolve())
    identifier = hashlib.sha256(source.encode()).hexdigest()[:20]
    if identifier in state.get("library_removed", {}):
        return False
    works = state.setdefault("work_library", [])
    item = next(
        (
            w
            for w in works
            if w["id"] == identifier
            or (w.get("archive_status") == "saved" and w.get("path") == source)
        ),
        None,
    )
    metadata = {
        "id": identifier,
        "kind": file_kind(source),
        "path": source,
        "title": (title or target.name)[:200],
        "producer": producer,
        "bytes": target.stat().st_size,
        "modified_at": target.stat().st_mtime,
        "status": status or "recorded",
        "shared": "unknown",
    }
    if metadata["kind"] == "text" and metadata["bytes"] <= 1024 * 1024:
        try:
            metadata["content_hash"] = text_content_hash(
                target.read_text(encoding="utf-8-sig"), metadata["title"]
            )
        except (OSError, UnicodeError):
            pass
    if item:
        preserved = item.get("status") if status is None else status
        if item.get("archive_status") == "saved":
            if status is not None:
                item["status"] = status
            return False
        item.update({k: v for k, v in metadata.items() if k != "shared"})
        if preserved:
            item["status"] = preserved
        return False
    works.append(metadata)
    state["work_library"] = works[-100:]
    return True


async def archive_local_media(state, sources, producer, *, origin):
    """Copy indexed or dispatched local media into durable plugin storage.

    Args:
        state: Exact target's work catalogue.
        sources: Filesystem paths or local file URIs from actual output.
        producer: Source used for a newly registered work.
        origin: Exact conversation origin for storage separation.

    Returns:
        Number of output rows backed by a verified copy.
    """
    root = None

    def copy(source):
        info = source.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= MEDIA_ARCHIVE_LIMIT
        ):
            raise ValueError("媒体文件为空、不受支持或超过100MB")
        folder = root / scope
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temp = None
        try:
            with (
                source.open("rb") as stream,
                tempfile.NamedTemporaryFile(dir=folder, delete=False) as output,
            ):
                temp = Path(output.name)
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > info.st_size:
                        raise OSError("媒体文件在备份期间发生变化")
                    output.write(chunk)
                    digest.update(chunk)
            final_info = source.stat()
            if size != info.st_size or final_info.st_mtime_ns != info.st_mtime_ns:
                raise OSError("媒体文件在备份期间发生变化")
            with temp.open("rb") as stream:
                verified = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    verified.update(chunk)
            if verified.digest() != digest.digest():
                raise OSError("媒体备份校验失败")
            target = folder / (digest.hexdigest() + source.suffix.lower())
            temp.replace(target)
            return target.resolve(), size, digest.hexdigest()
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    scope = hashlib.sha256(origin.encode()).hexdigest()[:16]
    count = 0
    for raw in dict.fromkeys(s for s in sources if isinstance(s, str) and s):
        if is_file_uri(raw) and urlsplit(raw).netloc.lower() not in {"", "localhost"}:
            continue
        source = Path(file_uri_to_path(raw))
        if (
            source.drive.startswith("\\\\")
            or not source.is_absolute()
            or source.suffix.lower() not in MEDIA_EXTENSIONS
        ):
            continue
        work = None
        try:
            source = source.resolve()
            if not source.is_file():
                continue
            if root is None:
                root = (
                    Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
                    / "media"
                )
            if source.is_relative_to(root.resolve()):
                continue
            identifier = hashlib.sha256(str(source).encode()).hexdigest()[:20]
            work = next(
                (
                    row
                    for row in state.get("work_library", [])
                    if row.get("id") == identifier or row.get("path") == str(source)
                ),
                None,
            )
            if work is None:
                register_local_work(state, str(source), producer)
                work = next(
                    (
                        row
                        for row in state.get("work_library", [])
                        if row.get("id") == identifier
                    ),
                    None,
                )
            if work is None:
                continue
            before = source.stat()
            stamp = [before.st_size, before.st_mtime_ns, before.st_ctime_ns]
            if (
                work.get("archive_status") == "saved"
                and work.get("source_stamp") == stamp
                and Path(work["path"]).is_file()
            ):
                continue
            target, size, digest = await asyncio.to_thread(copy, source)
            if any(row is work for row in state.get("work_library", [])):
                work.update(
                    path=str(target),
                    bytes=size,
                    content_hash=digest,
                    archive_status="saved",
                    archive_error="",
                    source_stamp=stamp,
                )
                count += 1
        except (OSError, ValueError) as exc:
            if work is not None and not (
                work.get("archive_status") == "saved" and Path(work["path"]).is_file()
            ):
                work.update(archive_status="failed", archive_error=str(exc)[:300])
    return count


async def discover_text_works(owner, umo):
    """Index actual current-persona text files without requiring a manual rescan.

    Args:
        owner: Current plugin runtime.
        umo: Exact target origin used to resolve persona and scoped output folder.

    Returns:
        Number of actual files inspected.
    """
    import asyncio

    manager = owner.context.conversation_manager
    cid = await manager.get_curr_conversation_id(umo)
    conv = await manager.get_conversation(umo, cid) if cid else None
    platform = owner.context.get_platform_inst(umo.split(":", 1)[0])
    scope = hashlib.sha256(umo.encode()).hexdigest()[:16]
    roots = [
        Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        / "works"
        / scope
    ]
    roots.extend(
        Path(value)
        for value in owner.config.get("work_library_roots", [])
        if isinstance(value, str) and value.strip() and Path(value).is_absolute()
    )
    if conv and platform:
        (
            persona_id,
            _,
            _,
            _,
        ) = await owner.context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conv.persona_id,
            platform_name=platform.meta().name,
        )
        if (
            isinstance(persona_id, str)
            and persona_id
            and Path(persona_id).name == persona_id
            and persona_id not in {".", ".."}
        ):
            roots.append(
                Path(__file__).resolve().parents[3]
                / "astrbot"
                / "数据"
                / persona_id
                / "作品"
            )

    def scan():
        found = []
        for root in roots:
            if root.is_dir():
                found.extend(
                    p
                    for p in root.iterdir()
                    if p.is_file() and p.resolve().is_relative_to(root.resolve())
                )
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[:100]

    files = await asyncio.to_thread(scan)
    state = owner.states.setdefault(umo, owner.new_state())
    for path in files:
        catalogue_receipt(
            state, "safe_write", json.dumps({"ok": True, "file": str(path.resolve())})
        )
    return len(files)


def catalogue_receipt(state, tool, text, *, media_only=False, local_outputs=None):
    """Index image URLs and actual file-write acknowledgements.

    Args:
        state: Per-conversation persistent state.
        tool: Actual producing tool.
        text: Original unmodified result.
        media_only: Ignore non-media paths when recording ordinary tool outputs.
        local_outputs: Optional list to collect explicit local paths from this receipt.

    Returns:
        Number of newly indexed outputs.
    """
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        if tool == "execute_python_code" and isinstance(text, str):
            matches = re.findall(r"(?m)^saved:\s*(.+?)\s+\d+\s*$", text)
            if local_outputs is not None:
                local_outputs.extend(path.strip() for path in matches[:10])
            return sum(
                register_local_work(state, path.strip(), tool)
                for path in matches[:10]
                if not media_only
                or Path(path.strip()).suffix.lower() in MEDIA_EXTENSIONS
            )
        return 0
    if not isinstance(value, dict):
        return 0
    candidates = []
    if value.get("status") == "completed" and isinstance(value.get("outputs"), dict):
        for output in value["outputs"].values():
            if (
                isinstance(output, dict)
                and output.get("type") == "image"
                and isinstance(output.get("url"), str)
            ):
                candidates.append(
                    {"kind": "image", "url": output["url"], "title": "模板成图"}
                )
    body = value
    for _ in range(3):
        if not isinstance(body.get("data"), dict):
            break
        body = body["data"]
    path = body.get("file") or body.get("path")
    local_added = 0
    if not any(value.get(k) is False for k in ("success", "ok")) and not any(
        body.get(k) is False for k in ("success", "ok", "saved")
    ):
        if (
            body.get("ok") is True
            or body.get("saved") is True
            or body.get("success") is True
            or body.get("status") == "completed"
        ):
            paths = [
                path,
                body.get("output_path"),
                body.get("image_path"),
                body.get("audio_path"),
                body.get("video_path"),
            ]
            if isinstance(body.get("outputs"), dict):
                paths.extend(
                    entry.get("path") or entry.get("file")
                    for entry in body["outputs"].values()
                    if isinstance(entry, dict)
                )
            for key in ("files", "artifacts", "output_files"):
                for entry in (
                    body.get(key, []) if isinstance(body.get(key), list) else []
                ):
                    paths.append(
                        entry
                        if isinstance(entry, str)
                        else entry.get("path")
                        if isinstance(entry, dict)
                        else None
                    )
            for actual in paths:
                if (
                    isinstance(actual, str)
                    and actual
                    and (
                        not media_only
                        or Path(actual).suffix.lower() in MEDIA_EXTENSIONS
                    )
                ):
                    if local_outputs is not None:
                        local_outputs.append(actual)
                    local_added += register_local_work(
                        state, actual, tool, str(body.get("title") or "")
                    )
    works = state.setdefault("work_library", [])
    removed = state.get("library_removed", {})
    added = 0
    for item in candidates:
        source = item.get("url") or item["path"]
        identifier = hashlib.sha256(source.encode()).hexdigest()[:20]
        if identifier in removed or any(work["id"] == identifier for work in works):
            continue
        works.append(
            {
                **item,
                "id": identifier,
                "producer": tool,
                "status": "recorded",
                "shared": "unknown",
            }
        )
        added += 1
    if added or not media_only:
        state["work_library"] = works[-100:]
    return added + local_added


def backfill_library(state):
    """Recover output references from stored actual receipts, never infer missing text.

    Args:
        state: Stored per-target state.

    Returns:
        Number of recovered catalogue entries.
    """
    count = 0
    for work in state.get("saved_works", []):
        count += catalogue_receipt(state, "autonomous_save_work", json.dumps(work))
    for artifact in state.get("task_artifacts", []):
        count += catalogue_receipt(
            state,
            artifact.get("producer", "run_template"),
            json.dumps(
                {
                    "status": "completed",
                    "outputs": {"image": {"type": "image", "url": artifact["url"]}},
                }
            ),
        )
        for work in state.get("work_library", []):
            if work.get("url") == artifact["url"]:
                work["shared"] = artifact.get("status", "unknown")
    for record in [state.get("execution_checkpoint", {}), state.get("last_turn", {})]:
        for receipt in record.get("receipts", record.get("tool_receipts", [])):
            count += catalogue_receipt(
                state, receipt.get("tool", ""), receipt.get("result", "")
            )
    for entry in state.get("journal", []):
        if entry.get("action") == "tool_result" and ": " in entry.get("summary", ""):
            tool, result = entry["summary"].split(": ", 1)
            count += catalogue_receipt(state, tool, result)
    return count


def read_library_text(state, identifier):
    """Read only an indexed text output with a strict size limit.

    Args:
        state: Target catalogue.
        identifier: Existing catalogue entry ID, never a client-supplied path.

    Returns:
        Verified file text.

    Raises:
        ValueError: The indexed work is missing, non-text or too large.
    """
    item = next(
        (
            w
            for w in state.get("work_library", [])
            if w["id"] == identifier and w["kind"] in {"text", "code"}
        ),
        None,
    )
    if item is None:
        raise ValueError("文本作品不存在")
    if isinstance(item.get("inline_content"), str):
        return item["inline_content"]
    path = Path(item["path"])
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("正文文件缺失或超过1MB，无法在页面读取")
    return path.read_text(encoding="utf-8-sig")


def refresh_text_fingerprints(state):
    """Backfill existing entries too, rather than requiring re-registration."""
    for item in list(state.get("work_library", [])):
        if item.get("kind") != "text":
            continue
        try:
            item["content_hash"] = text_content_hash(
                read_library_text(state, item["id"]), item.get("title", "")
            )
        except (ValueError, OSError, UnicodeError, KeyError):
            item.pop("content_hash", None)


async def read_library_image(state, identifier, base_url):
    """Serve an indexed ComfyUI image through authenticated dashboard transport.

    Args:
        state: Target work catalogue.
        identifier: Indexed image ID, never an arbitrary request URL.
        base_url: Configured ComfyUI root used to validate the source.

    Returns:
        Image data URI suitable for the sandboxed plugin page.

    Raises:
        ValueError: Source, size or image content is invalid.
    """
    item = next(
        (
            w
            for w in state.get("work_library", [])
            if w["id"] == identifier and w["kind"] == "image"
        ),
        None,
    )
    if item is None:
        raise ValueError("图片作品不存在")
    if item.get("path"):
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("本地图片不存在或超过20MB")
        raw = path.read_bytes()
        mime = (
            "image/png"
            if raw.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if raw.startswith(b"\xff\xd8\xff")
            else "image/gif"
            if raw[:6] in {b"GIF87a", b"GIF89a"}
            else "image/webp"
            if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
            else ""
        )
        if not mime:
            raise ValueError("本地文件不是可预览图片")
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode()
    url = item["url"]
    source, base = urlsplit(url), urlsplit(base_url)
    if (
        (source.scheme, source.hostname, source.port)
        != (base.scheme, base.hostname, base.port)
        or source.username
        or source.password
        or source.path != base.path.rstrip("/") + "/view"
    ):
        raise ValueError("图片来源不是当前配置的 ComfyUI 成图接口")
    query = parse_qs(source.query)
    if query.get("type", ["output"])[0] != "output" or not query.get("filename"):
        raise ValueError("不是有效的成图引用")
    cache = (
        Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        / "gallery"
        / (hashlib.sha256(url.encode()).hexdigest() + ".bin")
    )
    if cache.is_file():
        if cache.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("图片超过20MB，无法内嵌展示")
        raw = await asyncio.to_thread(cache.read_bytes)
    else:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(
                        f"成图读取失败 HTTP {response.status}，请确认 ComfyUI 在线及图片未删除"
                    )
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 20 * 1024 * 1024:
                        raise ValueError("图片超过20MB，无法内嵌展示")
                raw = bytes(data)
    mime = (
        "image/png"
        if raw.startswith(b"\x89PNG\r\n\x1a\n")
        else "image/jpeg"
        if raw.startswith(b"\xff\xd8\xff")
        else "image/webp"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        else "image/gif"
        if raw[:6] in {b"GIF87a", b"GIF89a"}
        else ""
    )
    if not mime:
        if cache.is_file():
            cache.unlink()
        raise ValueError("返回内容不是支持的图片格式")
    if not cache.is_file():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode()


async def archive_library_images(state, base_url, identifiers=None):
    """Persist registered output bytes while the producer is still available."""
    for item in list(state.get("work_library", [])):
        if item.get("kind") != "image" or (
            identifiers is not None and item["id"] not in identifiers
        ):
            continue
        if identifiers is None and item.get("archive_status") == "failed":
            continue
        if item.get("path") and Path(item["path"]).is_file():
            continue
        try:
            uri = await read_library_image(state, item["id"], base_url)
            header, payload = uri.split(",", 1)
            suffix = {
                "data:image/png;base64": ".png",
                "data:image/jpeg;base64": ".jpg",
                "data:image/webp;base64": ".webp",
                "data:image/gif;base64": ".gif",
            }[header]
            raw = base64.b64decode(payload, validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            path = (
                Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
                / "images"
                / (digest + suffix)
            )

            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    try:
                        with path.open("xb") as stream:
                            stream.write(raw)
                    except FileExistsError:
                        pass
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise OSError("图片保存后校验失败")

            await asyncio.to_thread(write)
            item.update(
                path=str(path.resolve()),
                bytes=len(raw),
                content_hash=digest,
                archive_status="saved",
                archive_error="",
                modified_at=path.stat().st_mtime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item.update(archive_status="failed", archive_error=str(exc)[:300])


def download_library_file(state, identifier):
    item = next(
        (
            w
            for w in state.get("work_library", [])
            if w["id"] == identifier and w.get("path")
        ),
        None,
    )
    if item is None:
        raise ValueError("文件作品未登记")
    path = Path(item["path"])
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("文件缺失或超过20MB")
    return {
        "filename": path.name,
        "base64": base64.b64encode(path.read_bytes()).decode(),
    }
