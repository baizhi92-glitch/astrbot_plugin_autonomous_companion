"""Create bounded, inert local artifacts from explicit autonomous tool calls."""

import csv
import hashlib
import io
import json
import re
import uuid
from pathlib import Path

from astrbot.api.star import StarTools
from astrbot.core.agent.tool import FunctionTool

from .lifecycle import record_evidence

CREATIVE_ARTIFACT_TOOL = "autonomous_save_creative_artifact"
CREATIVE_ARTIFACT_MAX_BYTES = 256 * 1024
_FORMATS = {"markdown": "md", "json": "json", "csv": "csv", "source": "txt"}
_FILENAME = re.compile(r"artifact-[0-9a-f]{32}\.(?:md|json|csv|txt)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def resolve_creative_artifact_path(path: str | Path, origin: str) -> Path:
    """Validate the generated absolute path against the exact origin's folder.

    Args:
        path: An internally generated or indexed artifact path.
        origin: The authenticated target origin, never a value from the receipt.

    Returns:
        The verified path, which may not yet exist when creating an artifact.

    Raises:
        ValueError: The path is outside the scope, not a generated filename, or redirected.
    """
    if not isinstance(origin, str) or not origin or not isinstance(path, (str, Path)):
        raise ValueError("缺少创作产物的位置或会话范围")
    target = Path(path)
    scope = hashlib.sha256(origin.encode()).hexdigest()[:16]
    root = Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion")).resolve()
    folder = root / "works" / scope / "creative"
    try:
        if (
            not _FILENAME.fullmatch(target.name)
            or target.parent != folder
            or target.resolve() != target
            or (
                target.exists() and (not target.is_file() or target.stat().st_nlink > 1)
            )
        ):
            raise ValueError("创作产物不在当前会话的专属目录")
    except (OSError, RuntimeError) as exc:
        raise ValueError("无法验证创作产物路径") from exc
    return target


class SaveCreativeArtifactTool(FunctionTool):
    """Save one new artifact without importing files or sending messages."""

    def __init__(self, event):
        super().__init__(
            name=CREATIVE_ARTIFACT_TOOL,
            description=(
                "在本地作品库创建一份新创作产物，不发送、不执行代码、不接受已有文件路径。"
                "markdown 适合剧本、诗集和其他排版文本；json/csv 适合结构化创作数据；"
                "source 保存源码原文为 .txt。传完整内容，不套代码围栏，最多 256 KiB UTF-8。"
                "CSV 仅支持逗号分隔且不能包含表格公式。保存后在作品库预览或下载，不改写已有作品。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "作品库展示名称"},
                    "format": {"type": "string", "enum": list(_FORMATS)},
                    "content": {"type": "string", "description": "实际创作的完整内容"},
                },
                "required": ["title", "format", "content"],
                "additionalProperties": False,
            },
        )
        self.event = event

    async def call(self, context, title: str, format: str, content: str) -> str:
        """Validate, write, verify, and index a new UTF-8 artifact.

        Args:
            context: AstrBot's tool execution context.
            title: The display title only; never used as a filename.
            format: The allowlisted storage format.
            content: Inline authored content; never treated as a file or command.

        Returns:
            A JSON receipt for the indexed file, not a delivery receipt.

        Raises:
            ValueError: The title, format, content, or storage scope is invalid.
            OSError: The file could not be written, verified, or indexed.
        """
        from .library import register_local_work

        if not isinstance(format, str) or format not in _FORMATS:
            raise ValueError("不支持的创作格式")
        if (
            not isinstance(title, str)
            or not 0 < len(title.strip()) <= 200
            or _CONTROL.search(title)
            or any(c in title for c in "\r\n\t")
        ):
            raise ValueError("作品标题须为 1–200 字符的单行文本")
        if (
            not isinstance(content, str)
            or len(content) > CREATIVE_ARTIFACT_MAX_BYTES
            or not content.strip()
            or _CONTROL.search(content)
        ):
            raise ValueError("创作内容须为非空文本，UTF-8 编码后最多 256 KiB")
        raw = content.encode("utf-8")
        if len(raw) > CREATIVE_ARTIFACT_MAX_BYTES:
            raise ValueError("创作内容的 UTF-8 大小超过 256 KiB")

        if format == "json":
            try:
                # Reject non-standard NaN/Infinity; keep original JSON bytes.
                json.loads(content, parse_constant=int)
            except (ValueError, RecursionError) as exc:
                raise ValueError("创作内容不是有效 JSON") from exc
        elif format == "csv":
            try:
                clean = io.StringIO(newline="")
                writer = csv.writer(clean, quoting=csv.QUOTE_ALL)
                for row in csv.reader(io.StringIO(content, newline=""), strict=True):
                    # Quote all parsed cells to preserve field boundaries after download.
                    # Reject spreadsheet formulas, including common compatibility variants.
                    if any(
                        re.match(r"[\s\ufeff]*[=+\-@＝＋－＠]", cell) for cell in row
                    ):
                        raise ValueError("CSV 创作产物不能包含公式开头的单元格")
                    writer.writerow(row)
                raw = clean.getvalue().encode("utf-8")
            except csv.Error as exc:
                raise ValueError("创作内容不是有效 CSV") from exc
            if len(raw) > CREATIVE_ARTIFACT_MAX_BYTES:
                raise ValueError("规范化后的 CSV 超过 256 KiB")

        event = self.event
        await event.owner.check_current(event)
        origin = event.unified_msg_origin
        scope = hashlib.sha256(origin.encode()).hexdigest()[:16]
        root = Path(
            StarTools.get_data_dir("astrbot_plugin_autonomous_companion")
        ).resolve()
        target = resolve_creative_artifact_path(
            root
            / "works"
            / scope
            / "creative"
            / f"artifact-{uuid.uuid4().hex}.{_FORMATS[format]}",
            origin,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        resolve_creative_artifact_path(target, origin)
        # Complete this bounded local write and index without yielding between them.
        created = False
        try:
            with target.open("xb") as stream:
                created = True
                stream.write(raw)
            if target.read_bytes() != raw:
                raise OSError("Creative artifact verification failed")
            if not register_local_work(
                event.state, str(target), self.name, title.strip(), status="finished"
            ):
                raise OSError("Creative artifact could not be indexed")
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise

        receipt = json.dumps(
            {
                "saved": True,
                "status": "completed",
                "title": title.strip(),
                "format": format,
                "bytes": len(raw),
                "output_path": str(target),
                "work_id": hashlib.sha256(str(target).encode()).hexdigest()[:20],
            },
            ensure_ascii=False,
        )
        record_evidence(event.state, "work_saved", receipt)
        event.owner.journal(event.state, "work_saved", receipt)
        await event.owner.save()
        return receipt
