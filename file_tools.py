"""不依赖 dsh 的本机文件工具：读、写、列目录、搜文件，以及可选的本机命令。

为什么要有这一节：dsh 通道是把活丢给外部程序干，机器上没装 dsh 的人就等于
没有任何动手能力。这里用纯 Python 在插件进程里实现同样的事，谁都能用。

安全边界（默认值都偏保守）：
* 写：只能写 file_tools_root（默认插件数据目录下的 files），越界一律拒绝。
* 读：默认只在 file_tools_root、作品目录、dsh 工作目录和额外只读目录里读。
* 命令：默认关闭（allow_shell_commands=false），要人显式打开才会出现。
* 每次写入或跑命令前先快照，出事可用 /自主 回退 倒回去。
"""

from __future__ import annotations

import asyncio
import locale
import os
import time
from pathlib import Path

from .guard import check_task, take_snapshot

MAX_READ_CHARS = 20000
MAX_OUTPUT_CHARS = 6000
MAX_WRITE_BYTES = 4 * 1024 * 1024


class Workspace:
    """Resolved, root-bound view of the local file system for the companion."""

    def __init__(self, write_root: Path, read_roots: list[Path]) -> None:
        self.write_root = Path(write_root)
        self.read_roots = [Path(item) for item in read_roots] or [self.write_root]

    def _within(self, path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def write_path(self, raw: str) -> Path:
        """Resolve a path that is about to be written, refusing anything outside the root."""
        if not str(raw or "").strip():
            raise ValueError("没有给路径")
        path = Path(os.path.expandvars(str(raw).strip())).expanduser()
        if not path.is_absolute():
            path = self.write_root / path
        path = path.resolve()
        if not self._within(path, [self.write_root.resolve()]):
            raise PermissionError(
                f"这个路径不在可写目录里：{path}；只能写 {self.write_root}"
            )
        return path

    def read_path(self, raw: str) -> Path:
        """Resolve a path that is about to be read."""
        text = str(raw or "").strip()
        if not text:
            raise ValueError("没有给路径")
        path = Path(os.path.expandvars(text)).expanduser()
        if not path.is_absolute():
            path = self.write_root / path
        path = path.resolve()
        if not self._within(path, [root.resolve() for root in self.read_roots]):
            raise PermissionError(
                "这个路径不在允许读取的目录里："
                + str(path)
                + "；允许读取："
                + "、".join(str(root) for root in self.read_roots)
            )
        return path


def _fail(message: str) -> dict:
    return {"ok": False, "status": "failed", "error": message}


def read_file(workspace: Workspace, path: str, max_chars: int, offset: int) -> dict:
    """Read a text file inside the allowed roots."""
    try:
        target = workspace.read_path(path)
    except (ValueError, PermissionError) as exc:
        return _fail(str(exc))
    if not target.exists():
        return _fail(f"文件不存在：{target}")
    if target.is_dir():
        return _fail(f"这是目录，不是文件：{target}")
    try:
        text = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return _fail(f"读不了这个文件：{exc}")
    total = len(text)
    start = max(0, int(offset or 0))
    limit = max(1, min(int(max_chars or 8000), MAX_READ_CHARS))
    chunk = text[start : start + limit]
    return {
        "ok": True,
        "status": "success",
        "path": str(target),
        "total_chars": total,
        "offset": start,
        "returned_chars": len(chunk),
        "truncated": start + len(chunk) < total,
        "content": chunk,
    }


def write_file(
    workspace: Workspace,
    store: Path,
    path: str,
    content: str,
    mode: str,
    keep: int,
    snapshot: bool,
) -> dict:
    """Write or append text inside the writable root, with a pre-write snapshot."""
    try:
        target = workspace.write_path(path)
    except (ValueError, PermissionError) as exc:
        return _fail(str(exc))
    body = content if isinstance(content, str) else str(content)
    if len(body.encode("utf-8")) > MAX_WRITE_BYTES:
        return _fail("要写的内容超过 4MB，先在别处精简一下")
    append = str(mode or "").strip().lower() in {"append", "a", "追加"}
    backup = ""
    if snapshot:
        backup = take_snapshot(workspace.write_root, store, keep, "write")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if append else "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    except OSError as exc:
        return _fail(f"写不进去：{exc}")
    return {
        "ok": True,
        "status": "success",
        "path": str(target),
        "mode": "append" if append else "overwrite",
        "written_chars": len(body),
        "total_chars": len(
            target.read_text(encoding="utf-8", errors="replace")
            if target.stat().st_size < 2_000_000
            else ""
        ),
        "backup": backup,
    }


def list_dir(workspace: Workspace, path: str, depth: int, limit: int = 200) -> dict:
    """List a directory inside the allowed roots."""
    try:
        target = workspace.read_path(path or ".")
    except (ValueError, PermissionError) as exc:
        return _fail(str(exc))
    if not target.exists():
        return _fail(f"目录不存在：{target}")
    if not target.is_dir():
        return _fail(f"这不是目录：{target}")
    levels = max(0, min(int(depth or 1), 3))
    entries = []
    try:
        for root, dirs, files in os.walk(target):
            current = Path(root)
            relative = current.relative_to(target)
            if len(relative.parts) > levels:
                dirs[:] = []
                continue
            for name in sorted(dirs)[:limit]:
                entries.append(f"{'  ' * len(relative.parts)}[目录] {relative / name}")
            for name in sorted(files)[:limit]:
                item = current / name
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"{'  ' * len(relative.parts)}{name} ({size} 字节)")
            if len(entries) >= limit:
                break
    except OSError as exc:
        return _fail(f"列不出来：{exc}")
    return {
        "ok": True,
        "status": "success",
        "path": str(target),
        "count": len(entries[:limit]),
        "entries": entries[:limit],
    }


def search_files(
    workspace: Workspace,
    root: str,
    pattern: str,
    contains: str,
    limit: int,
) -> dict:
    """Find files by glob and optionally by contained text."""
    try:
        base = workspace.read_path(root or ".")
    except (ValueError, PermissionError) as exc:
        return _fail(str(exc))
    if not base.is_dir():
        return _fail(f"不是目录：{base}")
    needle = str(contains or "")
    hits = []
    cap = max(1, min(int(limit or 20), 100))
    try:
        for path in base.rglob(str(pattern or "*")):
            if not path.is_file():
                continue
            line = ""
            if needle:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                index = text.find(needle)
                if index < 0:
                    continue
                start = max(0, index - 40)
                line = " ".join(text[start : index + len(needle) + 60].split())
            hits.append({"path": str(path), "hit": line})
            if len(hits) >= cap:
                break
    except OSError as exc:
        return _fail(f"搜不了：{exc}")
    return {
        "ok": True,
        "status": "success",
        "root": str(base),
        "pattern": pattern,
        "count": len(hits),
        "results": hits,
    }


async def run_command(
    command: str,
    cwd: Path,
    timeout: int,
    store: Path,
    keep: int,
    snapshot: bool,
) -> dict:
    """Run one local shell command, refused by the shared red lines first."""
    text = str(command or "").strip()
    if not text:
        return _fail("命令是空的")
    refusal = check_task(text, cwd)
    if refusal:
        return _fail(refusal)
    backup = ""
    if snapshot:
        backup = take_snapshot(cwd, store, keep, "cmd")
    limit = max(5, min(int(timeout or 60), 600))
    started = time.time()
    creationflags = 0x08000000 if os.name == "nt" else 0
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            text,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        if proc is not None:
            await _kill_tree(proc)
        return _fail(f"命令超过 {limit} 秒没结束，已强制停止")
    except OSError as exc:
        return _fail(f"起不了进程：{exc}")
    output = _decode_output(out)
    error = _decode_output(err)
    return {
        "ok": proc.returncode == 0,
        "status": "success" if proc.returncode == 0 else "failed",
        "command": text,
        "cwd": str(cwd),
        "exit_code": proc.returncode,
        "duration_seconds": round(time.time() - started, 2),
        "stdout": output[:MAX_OUTPUT_CHARS],
        "stderr": error[:2000],
        "truncated": len(output) > MAX_OUTPUT_CHARS,
        "backup": backup,
    }


def _decode_output(raw: bytes) -> str:
    """Decode command output without garble.

    Windows consoles answer in the local ANSI/OEM code page (GBK here), not
    UTF-8; decoding blindly as UTF-8 turns every Chinese character into "�".
    Try UTF-8 strictly first, then the platform code page.
    """
    if not raw:
        return ""
    candidates = ["utf-8"]
    for encoding in (
        locale.getpreferredencoding(False),
        locale.getencoding() if hasattr(locale, "getencoding") else "",
        "gbk",
    ):
        if encoding and encoding.lower() not in [item.lower() for item in candidates]:
            candidates.append(encoding)
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


async def _kill_tree(proc) -> None:
    """Kill a process and its children (Windows needs taskkill)."""
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            await killer.wait()
        else:
            proc.kill()
    except OSError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, OSError):
        pass
