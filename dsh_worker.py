"""本机执行通道：把真动手的活交给 DeepSeek Harness（dsh）跑。

自带实现，不依赖 astrbot_plugin_dsh_bridge。每轮自主行动里，只有模型判断
"这事得在本机动手"时才会调用；每次调用起一个 `dsh headless -` 子进程，
任务文本走 stdin，输出回传为工具结果。

沙箱边界：dsh 子进程的 cwd 就是它可写的工作目录（dsh_working_dir），
读操作不受限。超时会连同整棵进程树一起结束。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.agent.tool import FunctionTool

try:  # 正常按包导入
    from .guard import check_task, take_snapshot
except ImportError:  # pragma: no cover - 单独以脚本方式加载时兜底
    from guard import check_task, take_snapshot

DEFAULT_DSH_COMMAND = r"D:\dsh\resources\runtime\cli\bin\dsh.cmd"
COMMAND_FALLBACK_ROOTS = (
    r"D:\dsh",
    r"C:\Program Files\DeepSeek Harness",
    r"C:\Program Files (x86)\DeepSeek Harness",
)


def _decode(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamp(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…（结果已截断，原文 {len(text)} 字）"


def _split_args(value: Any) -> list[str]:
    """把字符串或列表规整成 dsh 的参数列表（保留路径反斜杠）。"""
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        text = str(value or "").strip()
        if not text:
            return []
        items = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', text)
    args: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
            cleaned = cleaned[1:-1]
        if cleaned:
            args.append(cleaned)
    return args


class DSHWorker:
    """串行执行 dsh 任务，供自主回合调用。"""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.running = 0
        self.completed = 0
        self.failed = 0
        self.last_error = ""
        self.last_duration = 0.0
        self._procs: set[asyncio.subprocess.Process] = set()
        self._semaphore = asyncio.Semaphore(
            max(1, self._int("dsh_max_concurrency", 1))
        )
        self._api_key_source = ""
        self.last_snapshot = ""

    # ---------------------------------------------------------------- 配置读取

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self.owner.config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _int(self, key: str, default: int) -> int:
        return _as_int(self._get(key, default), default)

    def _flag(self, key: str, default: bool) -> bool:
        """Read a boolean switch that may arrive as bool, string or number."""
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "off", "no", ""}

    def _backup(self, cwd: Path, label: str) -> str:
        """Snapshot the working directory before a real action.

        Args:
            cwd: Directory the local run may modify.
            label: Short tag for the snapshot file name.

        Returns:
            Snapshot name, or an empty string when disabled or failing.
        """
        self.last_snapshot = ""
        if not self._flag("dsh_snapshot_before_task", True):
            return ""
        try:
            self.last_snapshot = take_snapshot(
                cwd,
                self._plugin_data_dir() / "snapshots",
                max(1, self._int("dsh_snapshot_keep", 5)),
                label,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[autonomous] 快照失败，继续执行：{exc}")
            self.last_snapshot = ""
        return self.last_snapshot

    def snapshots(self, limit: int = 10) -> list[str]:
        """List local snapshots, newest first."""
        from .guard import list_snapshots

        return list_snapshots(self._plugin_data_dir() / "snapshots", limit)

    def restore(self, name: str = "", working_dir: str = "") -> tuple[bool, str]:
        """Roll the working directory back to a snapshot."""
        from .guard import restore_snapshot

        return restore_snapshot(
            self._plugin_data_dir() / "snapshots",
            self.working_dir(working_dir),
            name,
        )

    # ---------------------------------------------------------------- 路径解析

    def _plugin_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        except Exception:  # noqa: BLE001
            return Path(__file__).resolve().parent.parent.parent / "plugin_data"

    def default_working_dir(self) -> Path:
        """默认工作目录：她的插件数据目录下，不碰系统临时目录。"""
        return self._plugin_data_dir() / "dsh_workspace"

    def working_dir(self, override: str = "") -> Path:
        text = (override or "").strip() or str(self._get("dsh_working_dir", "") or "").strip()
        path = Path(os.path.expandvars(text)) if text else self.default_working_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"工作目录不可用（{path}）：{exc}"
            logger.warning("DSH worker: %s", self.last_error)
            fallback = self.default_working_dir()
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                return fallback
            except Exception:  # noqa: BLE001
                return Path.cwd()
        return path

    def resolve_command(self) -> str:
        configured = str(self._get("dsh_command", DEFAULT_DSH_COMMAND) or "").strip()
        if configured:
            configured = os.path.expandvars(configured)
            if Path(configured).exists():
                return configured
            found = shutil.which(configured)
            if found:
                return found
        env_bin = os.environ.get("DSH_BIN", "").strip()
        if env_bin and Path(env_bin).exists():
            return env_bin
        for root in COMMAND_FALLBACK_ROOTS:
            candidate = Path(root) / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
            if candidate.exists():
                return str(candidate)
        raise FileNotFoundError(
            "找不到 dsh，请在插件配置 dsh_command 里填 dsh.cmd 的完整路径"
        )

    def _credential_files(self) -> list[Path]:
        files: list[Path] = []
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            files.append(Path(appdata) / "dsh-desktop" / "harness" / ".credentials.yaml")
        files.append(Path.home() / ".dsh" / ".credentials.yaml")
        return files

    def resolve_api_key(self) -> str:
        """配置 → 环境变量 → DeepSeek Harness 自己的凭据文件。"""
        configured = str(self._get("dsh_api_key", "") or "").strip()
        if configured:
            self._api_key_source = "插件配置"
            return configured
        env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_key:
            self._api_key_source = "环境变量"
            return env_key
        for path in self._credential_files():
            try:
                if not path.exists():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            match = re.search(
                r"^\s*DEEPSEEK_API_KEY:\s*['\"]?([^\s'\"]+)", text, re.MULTILINE
            )
            if match:
                self._api_key_source = f"Harness 凭据（{path}）"
                return match.group(1).strip()
        self._api_key_source = ""
        return ""

    def _timeout(self) -> int:
        cap = self.owner.number("tool_timeout_seconds", 120, 5, 600) - 8
        wanted = self._int("dsh_timeout", 200)
        if cap <= 0:
            return max(10, wanted)
        return max(10, min(wanted, cap))

    # ------------------------------------------------------------------ 执行

    async def run(self, task: str, working_dir: str = "", timeout: int = 0) -> dict:
        """跑一次 dsh 任务。

        Args:
            task: 自然语言任务描述。
            working_dir: 可选的工作目录覆盖。
            timeout: 可选的秒数覆盖，仍受外层工具超时约束。

        Returns:
            含 ok / status / output / error 的结果字典，不抛异常。
        """
        text = (task or "").strip()
        if not text:
            return self._result(False, "", "任务内容是空的，什么都没执行。")
        if str(self._get("dsh_enabled", False)).strip().lower() in {
            "0",
            "false",
            "off",
            "no",
        }:
            return self._result(False, "", "本机执行通道已在插件配置里关闭（dsh_enabled）")

        try:
            command = self.resolve_command()
        except FileNotFoundError as exc:
            return self._result(False, "", str(exc))

        limit = self._timeout()
        if timeout and timeout > 0:
            limit = max(10, min(int(timeout), limit))
        cwd = self.working_dir(working_dir)
        refusal = check_task(text, cwd)
        if refusal:
            logger.warning(f"[autonomous] 本机任务被红线拦下：{text[:120]}")
            return self._result(False, "", refusal, cwd)
        self._backup(cwd, "task")

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        dsh_home = str(self._get("dsh_home", "") or "").strip()
        if dsh_home:
            env["DSH_HOME"] = os.path.expandvars(dsh_home)
        api_key = self.resolve_api_key()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        base_url = str(self._get("dsh_base_url", "") or "").strip()
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url

        argv = [command, "headless", "-"]
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        started = time.time()
        async with self._semaphore:
            self.running += 1
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=str(cwd),
                    creationflags=creationflags,
                )
                self._procs.add(proc)
                out_raw, err_raw = await asyncio.wait_for(
                    proc.communicate(input=text.encode("utf-8")), timeout=limit
                )
                code = proc.returncode or 0
                output = _decode(out_raw).strip()
                error = _decode(err_raw).strip()
                self.last_duration = round(time.time() - started, 1)
                if code != 0 and not output:
                    self.failed += 1
                    self.last_error = error[:400] or f"dsh 退出码 {code}"
                    self.completed += 1
                    return self._result(
                        False,
                        _clamp(output, self._result_limit()),
                        f"dsh 退出码 {code}：{error[:800] or '没有输出'}",
                        cwd,
                    )
                self.completed += 1
                if not output:
                    return self._result(
                        True,
                        "（dsh 执行完成，但没有输出）",
                        "",
                        cwd,
                    )
                return self._result(
                    True, _clamp(output, self._result_limit()), "", cwd
                )
            except asyncio.TimeoutError:
                self.failed += 1
                self.last_error = f"超时（{limit} 秒）"
                await self._kill(proc)
                return self._result(
                    False,
                    "",
                    f"任务超过 {limit} 秒仍未结束，已结束整棵进程树；可拆小再试。",
                    cwd,
                )
            except asyncio.CancelledError:
                self.last_error = "本轮被新消息或停用打断"
                await self._kill(proc)
                raise
            except BaseException as exc:  # noqa: BLE001
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self._kill(proc)
                return self._result(False, "", f"执行失败：{exc}", cwd)
            finally:
                self.running -= 1
                if proc is not None:
                    self._procs.discard(proc)

    async def run_cli(
        self,
        args: Any,
        stdin: str = "",
        working_dir: str = "",
        timeout: int = 0,
    ) -> dict:
        """把参数原样交给 dsh 命令行执行。"""
        argv = _split_args(args)
        if not argv:
            return self._result(
                False, "", "没有给 dsh 任何参数，什么都没执行（比如传 ['--version']）。"
            )
        if str(self._get("dsh_enabled", False)).strip().lower() in {
            "0",
            "false",
            "off",
            "no",
        }:
            return self._result(False, "", "本机执行通道已在插件配置里关闭（dsh_enabled）")
        try:
            command = self.resolve_command()
        except FileNotFoundError as exc:
            return self._result(False, "", str(exc))

        limit = self._timeout()
        if timeout and timeout > 0:
            limit = max(10, min(int(timeout), limit))
        cwd = self.working_dir(working_dir)
        refusal = check_task(" ".join(argv), cwd)
        if refusal:
            logger.warning(f"[autonomous] 命令行任务被红线拦下：{argv}")
            return self._result(False, "", refusal, cwd)
        self._backup(cwd, "cli")
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        dsh_home = str(self._get("dsh_home", "") or "").strip()
        if dsh_home:
            env["DSH_HOME"] = os.path.expandvars(dsh_home)
        api_key = self.resolve_api_key()
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
        base_url = str(self._get("dsh_base_url", "") or "").strip()
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url

        creationflags = 0x08000000 if os.name == "nt" else 0
        started = time.time()
        async with self._semaphore:
            self.running += 1
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    command,
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=str(cwd),
                    creationflags=creationflags,
                )
                self._procs.add(proc)
                out_raw, err_raw = await asyncio.wait_for(
                    proc.communicate(input=stdin.encode("utf-8")), timeout=limit
                )
                code = proc.returncode or 0
                output = _clamp(_decode(out_raw).strip(), self._result_limit())
                error = _decode(err_raw).strip()
                self.last_duration = round(time.time() - started, 1)
                self.completed += 1
                if code != 0:
                    self.failed += 1
                    self.last_error = error[:400] or f"dsh 退出码 {code}"
                return {
                    "ok": code == 0,
                    "output": output,
                    "error": error[:4000],
                    "exit_code": code,
                    "argv": argv,
                    "working_dir": str(cwd),
                    "duration_seconds": self.last_duration,
                }
            except asyncio.TimeoutError:
                self.failed += 1
                self.last_error = f"超时（{limit} 秒）"
                await self._kill(proc)
                return {
                    "ok": False,
                    "output": "",
                    "error": f"dsh 超过 {limit} 秒仍未结束，已结束整棵进程树。",
                    "exit_code": None,
                    "argv": argv,
                    "working_dir": str(cwd),
                    "duration_seconds": round(time.time() - started, 1),
                }
            except asyncio.CancelledError:
                await self._kill(proc)
                raise
            except BaseException as exc:  # noqa: BLE001
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await self._kill(proc)
                return {
                    "ok": False,
                    "output": "",
                    "error": f"dsh 执行失败：{exc}",
                    "exit_code": None,
                    "argv": argv,
                    "working_dir": str(cwd),
                    "duration_seconds": round(time.time() - started, 1),
                }
            finally:
                self.running -= 1
                if proc is not None:
                    self._procs.discard(proc)

    def _result_limit(self) -> int:
        return max(400, self._int("dsh_max_result_chars", 5000))

    def _result(
        self,
        ok: bool,
        output: str,
        error: str,
        cwd: Path | None = None,
    ) -> dict:
        return {
            "ok": ok,
            "output": output,
            "error": error,
            "working_dir": str(cwd) if cwd else "",
            "duration_seconds": self.last_duration,
        }

    async def _kill(self, proc: asyncio.subprocess.Process | None) -> None:
        if proc is None or proc.returncode is not None:
            return
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
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "running": self.running,
            "completed": self.completed,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_duration_seconds": self.last_duration,
            "working_dir": str(self.working_dir()),
            "api_key_source": self._api_key_source,
        }


class DSHTaskTool(FunctionTool):
    """把需要在本机真实动手的任务交给 dsh 执行。"""

    def __init__(self, event, worker: DSHWorker) -> None:
        super().__init__(
            name="autonomous_dsh_task",
            description=(
                "在本机真实动手时调用：查看磁盘/内存/进程/网络、翻目录和文件、读日志找报错、"
                "统计文件、批量改名、跑命令或脚本、检查软件是否安装、抓取本地数据、"
                "把整理好的结果写成文件。纯聊天、常识问答、写正文、编故事不要调用。"
                "每次调用都是一次完整本机执行，较慢也不免费，一次只交一件具体的事。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "要在本机执行的任务，用自然语言写清目标、路径和输出要求，"
                            "例如「统计 D:\\logs 里今天 ERROR 最多的三个服务，只给结论」。"
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "可选。指定工作目录（能写文件的边界）；不填用配置的默认目录。",
                    },
                },
                "required": ["task"],
            },
        )
        self.event = event
        self.worker = worker

    async def call(self, context, task: str, working_dir: str = ""):
        """执行一次本机任务并返回可核对回执。

        Args:
            context: AstrBot 工具执行上下文。
            task: 任务描述。
            working_dir: 可选工作目录。

        Returns:
            JSON 字符串回执；status=success 表示 dsh 正常结束。
        """
        event = self.event
        await event.owner.check_current(event)
        result = await self.worker.run(task, working_dir=working_dir)
        if result["ok"]:
            payload = {
                "ok": True,
                "status": "success",
                "output": result["output"],
                "duration_seconds": result["duration_seconds"],
                "working_dir": result["working_dir"],
            }
        else:
            payload = {
                "ok": False,
                "status": "failed",
                "error": result["error"] or result["output"] or "执行失败，没有输出",
                "output": result["output"][:2000],
                "working_dir": result["working_dir"],
            }
        return json.dumps(payload, ensure_ascii=False)


class DSHCliTool(FunctionTool):
    """直接调用 dsh 命令行，参数原样交给 dsh。"""

    def __init__(self, event, worker: DSHWorker) -> None:
        super().__init__(
            name="autonomous_dsh_cli",
            description=(
                "直接跑 dsh 命令行：你给的参数会原样跟在 dsh 后面执行。可以自主调用，不用先问。"
                "想看版本/帮助、指定 profile、自己拼参数跑一次性任务，都用这个。"
                "常用参数：['--version']、['--help']、['headless','-']（任务文本放 stdin）。"
                "web 和 tui 是交互式界面，会在后台一直等输入，别在这里用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "传给 dsh 的参数列表，例如 ['--version'] 或 "
                            "['headless','-']；不要带 dsh 本身。"
                        ),
                    },
                    "stdin": {
                        "type": "string",
                        "description": (
                            "可选。dsh 的标准输入文本；headless 一次性任务就写在这里。"
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "可选。dsh 的工作目录；不填用插件默认目录。",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "可选。秒数，不填用插件配置的超时。",
                    },
                },
                "required": ["args"],
            },
        )
        self.event = event
        self.worker = worker

    async def call(
        self,
        context,
        args,
        stdin: str = "",
        working_dir: str = "",
        timeout: int = 0,
    ):
        """执行 dsh 命令并返回开机结果。"""
        event = self.event
        await event.owner.check_current(event)
        result = await self.worker.run_cli(
            args,
            stdin=stdin,
            working_dir=working_dir,
            timeout=timeout,
        )
        payload = {
            "ok": result["ok"],
            "status": "success" if result["ok"] else "failed",
            "argv": result["argv"],
            "output": result["output"],
            "error": result["error"],
            "exit_code": result["exit_code"],
            "duration_seconds": result["duration_seconds"],
            "working_dir": result["working_dir"],
        }
        return json.dumps(payload, ensure_ascii=False)