"""Read-only runtime checks and the exportable diagnostic bundle.

Nothing here calls a model or sends a message. The export is meant to be handed
to someone else, so every secret and account number is masked before it is
written; the raw values never leave this process.
"""

import asyncio
import dataclasses
import importlib
import json
import os
import platform
import re
import sys
import time
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

from astrbot.api.star import StarTools
from astrbot.core.platform.platform import PlatformStatus

# 键名命中这些词的值一律不导出
_SECRET_KEY = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|pwd|cookie|credential"
    r"|authorization|private[_-]?key|access[_-]?key|sign|webhook|proxy)$",
    re.IGNORECASE,
)
# 账号/会话标识里的数字串
_DIGITS = re.compile(r"\d{5,}")
_SECRET_INLINE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})"
)


def mask_digits(text) -> str:
    """Mask account-like digit runs while keeping the shape readable.

    Args:
        text: Any value; non-strings are returned unchanged by ``str`` rules.

    Returns:
        The text with each 5+ digit run replaced by its first and last two digits.
    """

    def _keep(match):
        value = match.group(0)
        # 账号是 5-12 位；更长的多半是紧凑时间戳，更短的只是普通数字
        if not 5 <= len(value) <= 12:
            return value
        return value[:2] + "***" + value[-2:]

    return _DIGITS.sub(_keep, str(text))


def redact(value, key: str = ""):
    """Recursively mask secrets and account numbers in a config-like structure.

    Args:
        value: Any JSON-able value.
        key: Key this value was stored under, used to detect secret fields.

    Returns:
        A copy safe to hand to someone else.
    """
    if key and _SECRET_KEY.search(str(key)):
        if isinstance(value, (dict, list)):
            return "***（已隐去）"
        return "***" if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        return mask_digits(_SECRET_INLINE.sub("***", value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if dataclasses.is_dataclass(value):
        return redact(dataclasses.asdict(value), key)
    return value


def identifies_plugin(meta, needle: str) -> bool:
    """Report whether a star metadata entry is the plugin we are looking for.

    Plugins are not always installed under the folder name their author
    published: a release zip unpacks to something like ``LivingMemory-main``,
    and ``metadata.yaml`` decides ``meta.name``. Matching only the directory
    name is why an installed memory plugin could still read as "not connected".

    Args:
        meta: Star metadata from the star manager.
        needle: Plugin family key, e.g. "livingmemory".

    Returns:
        True when any identity attribute contains the key.
    """
    key = re.sub(r"[^a-z0-9]", "", str(needle).lower())
    if not key:
        return False
    for attr in ("root_dir_name", "name", "display_name", "module_path"):
        value = re.sub(r"[^a-z0-9]", "", str(getattr(meta, attr, "") or "").lower())
        if key in value:
            return True
    return False


def _package_candidates(meta) -> list:
    """List every plausible import package for a plugin, best first.

    A plugin's folder name is not always importable (release zips may keep
    ``-main``), so the module Python actually loaded wins, then the recorded
    module path, then the folder name, then whatever LivingMemory scope module
    is already imported.

    Args:
        meta: Star metadata.

    Returns:
        Deduplicated package names, most trustworthy first.
    """
    seen = []
    module = getattr(meta, "module", None)
    for candidate in (
        str(getattr(module, "__package__", "") or ""),
        str(getattr(meta, "module_path", "") or "").removesuffix(".main"),
        "data.plugins." + str(getattr(meta, "root_dir_name", "") or ""),
    ):
        if candidate and candidate not in seen:
            seen.append(candidate)
    for name in list(sys.modules):
        if name.endswith(".core.memory_scope"):
            package = name[: -len(".core.memory_scope")]
            if package not in seen:
                seen.append(package)
    return seen or ["data.plugins.astrbot_plugin_livingmemory"]


def plugin_package(meta) -> str:
    """Return the first package that really exposes LivingMemory's scope module.

    Args:
        meta: Star metadata.

    Returns:
        A package name whose ``core.memory_scope`` is importable, falling back
        to the best guess.
    """
    candidates = _package_candidates(meta)
    for candidate in candidates:
        try:
            importlib.import_module(f"{candidate}.core.memory_scope")
        except Exception:
            continue
        return candidate
    return candidates[0]


def _missing_scope_helpers(meta) -> list:
    """List the LivingMemory scope helpers this bridge needs but cannot see.

    Args:
        meta: LivingMemory star metadata.

    Returns:
        Names of missing helpers, or a single import error description.
    """
    try:
        scope_module = importlib.import_module(
            f"{plugin_package(meta)}.core.memory_scope"
        )
    except Exception as exc:
        return ["core.memory_scope 无法导入（" + type(exc).__name__ + "）"]
    return [
        name
        for name in ("adapter_access", "is_event_memory_allowed", "resolve_memory_scope")
        if not callable(getattr(scope_module, name, None))
    ]


def _version() -> str:
    """Read this plugin's version from its metadata file.

    Returns:
        The declared version, or "unknown" when it cannot be read.
    """
    try:
        for line in Path(__file__).with_name("metadata.yaml").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.strip().startswith("version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _environment() -> dict:
    """Describe the host stack for a bug report.

    Returns:
        Framework, interpreter and OS versions.
    """
    astrbot = "unknown"
    try:
        import astrbot

        astrbot = getattr(astrbot, "__version__", "unknown")
    except Exception:
        pass
    return {
        "插件版本": _version(),
        "AstrBot": astrbot,
        "Python": sys.version.split()[0],
        "系统": platform.platform(),
    }


def livingmemory_probe(owner, umo: str | None = None) -> dict:
    """Explain whether LivingMemory is connected, and if not, why.

    Args:
        owner: Active plugin instance.
        umo: Optional session origin used to read the last real read result.

    Returns:
        Plugin identity, exposed interface and a one-line conclusion.
    """
    state = owner.states.get(umo, {}) if umo else {}
    info = {
        "上次读取结果": state.get("memory_status", ""),
        "上次提供者": state.get("memory_provider", ""),
        "上次接口说明": state.get("livingmemory_status", ""),
    }
    try:
        stars = list(owner.context.get_all_stars())
    except Exception as exc:
        info["结论"] = "读取插件列表失败：" + type(exc).__name__
        return info
    found = [meta for meta in stars if identifies_plugin(meta, "livingmemory")]
    if not found:
        info["结论"] = "没找到 LivingMemory：确认插件已安装并出现在插件列表里"
        return info
    meta = found[0]
    info["插件"] = {
        "名称": getattr(meta, "name", "") or "",
        "显示名": getattr(meta, "display_name", "") or "",
        "版本": getattr(meta, "version", "") or "",
        "目录": getattr(meta, "root_dir_name", "") or "",
        "已启用": bool(getattr(meta, "activated", False)),
    }
    if not getattr(meta, "activated", False):
        info["结论"] = "LivingMemory 已安装但未启用：先在插件管理里启用它"
        return info
    plugin = getattr(meta, "star_cls", None)
    if callable(getattr(plugin, "autonomous_recall", None)) and callable(
        getattr(plugin, "autonomous_record", None)
    ):
        info["接口"] = "专用接口（autonomous_recall / autonomous_record）"
        info["结论"] = "可用"
        return info
    missing = _missing_scope_helpers(meta)
    if missing:
        info["接口"] = "原生接口不可用"
        info["缺少"] = missing
        info["结论"] = (
            "接口不完整：更新 LivingMemory 后完整重启 AstrBot"
            "（只重载插件会留着进程里的旧模块）"
        )
        return info
    info["接口"] = "原生接口直连"
    info["结论"] = "可用"
    return info


_ACTION_LABELS = {
    "dispatched": "实际发出",
    "dispatch_attempt": "准备发出",
    "dispatch_blocked": "发送被拦",
    "chat": "回合完成",
    "silent": "选择沉默",
    "speak_due": "到点开口",
    "speak_scheduled": "约定开口",
    "speak_merged": "合并约定",
    "speak_cancelled": "取消约定",
    "speak_cancel_missed": "取消没找到",
    "speak_ignored": "约定被忽略",
    "wish_kept": "记下一句",
    "wish_spoken": "说出那句",
    "night_silent": "夜间安静",
    "morning_report": "早间汇报",
    "blocked": "回合被拦",
    "failed": "回合失败",
}


_TRIGGER_LABELS = {
    "heartbeat": "心跳",
    "speak": "到点开口",
    "continuation": "续行",
    "event": "外部事件",
}


def _short_stamp(value) -> str:
    """Render a journal timestamp as a short readable local time.

    Args:
        value: ISO timestamp string, or epoch seconds.

    Returns:
        ``MM-DD HH:MM:SS`` when parseable, otherwise the original text.
    """
    text = str(value or "")
    try:
        moment = datetime.fromisoformat(text)
        return moment.astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(float(text)).astimezone().strftime(
            "%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError, OSError):
        return text


def _timeline(state, limit: int = 200) -> list:
    """Build the proactive record: what she did, when, and why.

    The journal is the spine: every send, silence, scheduled promise and
    blocked round is written there with its own reason, so one ordered list
    answers "她这半天到底做了什么" without cross-referencing three fields.

    Args:
        state: Target state.
        limit: Maximum entries to include, newest kept.

    Returns:
        Ordered entries with a readable stage label.
    """
    entries = (state.get("journal") or [])[-limit:]
    rows = []
    for entry in entries:
        action = str(entry.get("action", ""))
        trigger = str(entry.get("trigger", ""))
        row = {
            "时间": _short_stamp(entry.get("time")),
            "阶段": _ACTION_LABELS.get(action, action),
            "触发": _TRIGGER_LABELS.get(trigger, trigger),
            "动作": action,
            "内容": str(entry.get("summary", ""))[:2000],
        }
        # 一次发送会写"准备发出"和"实际发出"两条同文记录：合成一条，
        # 否则记录被稀释成两倍噪音。缺后半段说明消息没送到，留着更有用。
        if (
            action == "dispatched"
            and rows
            and rows[-1]["动作"] == "dispatch_attempt"
            and rows[-1]["内容"] == row["内容"]
        ):
            rows[-1] = row
            continue
        rows.append(row)
    for row in rows:
        if row["动作"] == "dispatch_attempt":
            row["阶段"] = "准备发出（未见送达记录）"
    return rows


def _proactive_summary(state) -> dict:
    """Summarise today's proactive output and budget counters.

    Args:
        state: Target state.

    Returns:
        Counters plus a delivery cross-check: the journal can be trimmed while
        the delivery list is not, so a mismatch is reported as a caveat rather
        than hidden.
    """
    today = state.get("today") or {}
    usage = state.get("usage") or {}
    tools = today.get("tools")
    delivered = len(state.get("visible_dispatches") or [])
    journal_sent = sum(
        1
        for entry in (state.get("journal") or [])
        if entry.get("action") == "dispatched"
    )
    return {
        "账本日期": today.get("day", ""),
        "今天发出条数": today.get("sent", 0),
        "今天沉默次数": today.get("silent", 0),
        "今天工具调用": len(tools) if isinstance(tools, dict) else (tools or 0),
        "送达明细": f"保留最近 {delivered} 条",
        "日志里的发出条数": f"{journal_sent} 条（日志最多留 200 条）",
        "预算计数": {
            key: usage.get(key)
            for key in ("checks", "rounds", "tools", "messages", "failures")
            if key in usage
        },
        "口径": (
            "今天发出条数按消息段累计，是准数；"
            "送达明细和日志只保留最近若干条，条数天然更少，不要当成漏发"
        ),
    }


def _state_digest(owner, umo: str) -> dict:
    """Collect the per-conversation facts a maintainer needs first.

    Args:
        owner: Active plugin instance.
        umo: Target session origin.

    Returns:
        A redacted, human-readable snapshot of the conversation state.
    """
    state = owner.states.get(umo, {})
    speak = []
    for item in state.get("speak_schedule", []) or []:
        try:
            at = datetime.fromtimestamp(float(item.get("at", 0))).strftime(
                "%m-%d %H:%M"
            )
        except (TypeError, ValueError, OSError):
            at = str(item.get("at"))
        speak.append({"约定于": at, "原因": str(item.get("reason", ""))[:200]})
    last_turn = state.get("last_turn") or {}
    return {
        "会话": mask_digits(umo),
        "启用": owner.config.get("enabled", True),
        "下次判断时间": _stamp(state.get("next_due")),
        "基础间隔分钟": state.get("rhythm_minutes"),
        "节奏原因": str(state.get("rhythm_reason", ""))[:120],
        "暂停到": _stamp(state.get("pause_until")),
        "记忆": state.get("memory_status", "尚未执行"),
        "记忆提供者": state.get("memory_provider", ""),
        "陪伴库": state.get("companion_status", "尚未执行"),
        "上次回合": {
            "触发": last_turn.get("trigger", ""),
            "状态": last_turn.get("status", ""),
            "动作": last_turn.get("action", ""),
            "原因": str(last_turn.get("reason", ""))[:200],
        },
        "最近错误": str(state.get("last_error", ""))[:300],
        "开口约定": speak,
        "待补发正文": bool(state.get("pending_message")),
        "今日概况": _proactive_summary(state),
        "今日账本": (state.get("today") or {}).get("items", [])[-20:],
        "主动记录": _timeline(state),
        "活跃时间摘要": _activity(owner, state),
    }


def _stamp(value) -> str:
    """Render an epoch timestamp as a local date-time string.

    Args:
        value: Epoch seconds, possibly missing.

    Returns:
        A readable timestamp, or "" when unusable.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


def _activity(owner, state) -> dict:
    """Summarise the learned activity histogram.

    Args:
        owner: Active plugin instance.
        state: Target state.

    Returns:
        Sample count and the quiet/active ranges, never raw weights.
    """
    try:
        from .activity import summary

        info = summary(
            state, owner.number("activity_half_life_days", 30, 7, 365)
        )
    except Exception as exc:
        return {"读取失败": type(exc).__name__}
    return {
        "样本数": info.get("samples"),
        "足够判断": info.get("enough"),
        "常在线": info.get("active"),
        "基本不在": info.get("quiet"),
    }


def _trim(value, depth: int = 0, limit: int = 4000, items: int = 200):
    """Cap pathological sizes so one huge field cannot swamp the report.

    Args:
        value: Any JSON-able value.
        depth: Current nesting depth.
        limit: Longest string kept before it is collapsed.
        items: Longest list kept before it is shortened.

    Returns:
        A size-bounded copy.
    """
    if depth > 8:
        return "…（层级过深已省略）"
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return (
            value[:limit]
            + f"…（截断，原长 {len(value)} 字）…"
            + value[-500:]
        )
    if isinstance(value, dict):
        return {
            str(k): _trim(v, depth + 1, limit, items) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        kept = [_trim(item, depth + 1, limit, items) for item in value[:items]]
        if len(value) > items:
            kept.append(f"…（还有 {len(value) - items} 条已省略）")
        return kept
    if dataclasses.is_dataclass(value):
        return _trim(dataclasses.asdict(value), depth + 1, limit, items)
    return value


def _logs_dir() -> Path | None:
    """Locate AstrBot's log directory from the places it can live.

    Returns:
        The directory holding ``astrbot.log``, or None.
    """
    candidates = []
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(Path(get_astrbot_data_path()) / "logs")
    except Exception:
        pass
    try:
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        candidates.append(data_dir.parent.parent / "logs")
    except Exception:
        pass
    candidates.append(Path.cwd() / "data" / "logs")
    for candidate in candidates:
        try:
            if (candidate / "astrbot.log").exists():
                return candidate
        except OSError:
            continue
    return None


def _log_tail(limit: int = 300, scan_bytes: int = 3 * 1024 * 1024) -> dict:
    """Read the tail of AstrBot's log and keep the lines that matter here.

    Only lines mentioning this plugin, its memory dependencies, or a traceback
    are kept: the full log mixes every other plugin's traffic and private chat.

    Args:
        limit: Maximum lines to keep.
        scan_bytes: How much of the file tail to scan.

    Returns:
        Log file name, scanned size and the filtered lines.
    """
    folder = _logs_dir()
    if folder is None:
        return {"文件": "", "说明": "没找到 AstrBot 日志目录（data/logs）", "内容": []}
    try:
        path = max(folder.glob("*.log"), key=lambda item: item.stat().st_mtime)
    except (OSError, ValueError):
        return {"文件": "", "说明": "日志目录里没有 .log 文件", "内容": []}
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - scan_bytes))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"文件": path.name, "说明": "读取失败：" + type(exc).__name__, "内容": []}
    pattern = re.compile(
        r"(autonomous_companion|AutonomousCompanion|我只在乎你|香夜梨"
        r"|livingmemory|LivingMemory|Traceback|ERROR|CRITICAL)",
        re.IGNORECASE,
    )
    kept = [
        line[:600]
        for line in chunk.splitlines()
        if line.strip() and pattern.search(line)
    ][-limit:]
    return {
        "文件": path.name,
        "扫描末尾": f"{min(size, scan_bytes) // 1024} KB / 全文 {size // 1024} KB",
        "命中行数": len(kept),
        "注意": "这些行可能含聊天原文，转发前先扫一眼。",
        "内容": kept,
    }


async def memory_sample(owner, umo: str, query: str = "") -> dict:
    """Run one real memory recall so the report shows what she actually reads.

    Uses the same bridge a normal turn uses, against a throwaway state copy so
    the live session state is never touched.

    Args:
        owner: Active plugin instance.
        umo: Session origin to recall for.
        query: Optional recall query.

    Returns:
        The recall text, how many entries it contained, or why it failed.
    """
    state = dict(owner.states.get(umo) or {})
    identity = state.get("identity") or {}
    platform = umo.split(":", 1)[0] if ":" in umo else ""
    shim = SimpleNamespace(
        unified_msg_origin=umo,
        state=state,
        persona_id=None,
        plugins_name=None,
        get_sender_id=lambda: str(identity.get("user_id", "")),
        get_self_id=lambda: str(identity.get("bot_id", "")),
        get_platform_name=lambda: platform,
        get_message_type=lambda: "FriendMessage",
        get_message_str=lambda: "",
    )
    try:
        bridge = owner.memory_bridge(shim)
    except Exception as exc:
        return {"结论": "选择记忆插件失败：" + type(exc).__name__}
    if bridge is None:
        return {"结论": "没有可用的记忆插件，本轮召回未执行"}
    try:
        text = await asyncio.wait_for(
            bridge.compose_context(
                query=query or "用户偏好、最近话题和未完成创作",
                session_context={
                    "session_id": umo,
                    "scope": "private",
                    "platform": platform,
                    "user_id": str(identity.get("user_id", "")),
                    "bot_id": str(identity.get("bot_id", "")),
                    "persona_id": None,
                },
                top_k=6,
                max_chars=5000,
            ),
            30,
        )
    except Exception as exc:
        return {
            "接口": state.get("livingmemory_status", ""),
            "结论": "召回失败：" + type(exc).__name__ + "：" + str(exc)[:200],
        }
    return {
        "提供者": state.get("memory_provider", ""),
        "接口": state.get("livingmemory_status", ""),
        "条数": text.count("记忆 #") if text else 0,
        "字数": len(text or ""),
        "内容": mask_digits(text or ""),
        "结论": "已召回" if text else "已连接但没召回任何记忆",
    }


def build_export(owner, umo: str | None = None, extra: dict | None = None) -> dict:
    """Assemble the diagnostic report as a redacted dictionary.

    Args:
        owner: Active plugin instance.
        umo: Optional single session; omit to include every configured target.
        extra: Pre-computed async checks (e.g. the runtime self-inspection).

    Returns:
        A JSON-able report with environment, config, self-checks, every state
        field and the relevant log tail.
    """
    origins = [umo] if umo else list(owner.target_origins())
    states = [
        (origin, owner.states[origin]) for origin in origins if origin in owner.states
    ]
    return {
        "生成时间": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "运行环境": _environment(),
        "运行自检": (extra or {}).get("自检", {}),
        "记忆采样": (extra or {}).get("记忆采样", {}),
        "配置（已脱敏）": redact(dict(owner.config)),
        "记忆插件自检": livingmemory_probe(owner, umo),
        "会话": [redact(_state_digest(owner, origin)) for origin, _ in states],
        # 摘要只挑常用的；排障真正要靠完整状态，一个字段都不省。
        "完整状态": [
            {"会话": mask_digits(origin), "状态": redact(_trim(state))}
            for origin, state in states
        ],
        "插件日志": _log_tail(),
        "说明": (
            "由 /自主 诊断 或设置页导出；密钥与账号数字已隐去。"
            "完整状态与日志在 json 里，md 只做摘要。"
        ),
    }


def render_markdown(report: dict) -> str:
    """Render a report as readable markdown for a chat window.

    Args:
        report: Output of :func:`build_export`.

    Returns:
        Markdown text.
    """
    lines = ["# 我只在乎你 · 诊断包", ""]
    for name, value in report.get("运行环境", {}).items():
        lines.append(f"- {name}: {value}")
    lines += ["", f"- 生成时间: {report.get('生成时间', '')}", ""]
    checks = report.get("运行自检") or {}
    for row in checks.get("targets", []) if isinstance(checks, dict) else []:
        lines += ["", f"## 运行自检 · {row.get('target')}", ""]
        for check in row.get("checks", []):
            lines.append(
                f"- [{check.get('status')}] {check.get('name')} · {check.get('detail')}"
            )
    probe = report.get("记忆插件自检") or {}
    if probe:
        lines += ["", "## 记忆插件自检"]
        for name, value in probe.items():
            lines.append(f"- {name}: {json.dumps(value, ensure_ascii=False)}")
    sample = report.get("记忆采样") or {}
    if sample:
        lines += ["", "## 记忆采样（这次实际读到的内容）"]
        for name in ("提供者", "接口", "条数", "字数", "结论"):
            if sample.get(name) not in (None, ""):
                lines.append(f"- {name}: {sample[name]}")
        if sample.get("内容"):
            lines += ["", "```", str(sample["内容"])[:6000], "```"]
    config = report.get("配置（已脱敏）") or {}
    lines += ["## 配置（密钥与账号已隐去）", "```json"]
    lines.append(json.dumps(config, ensure_ascii=False, indent=2)[:6000])
    lines += ["```", "", f"## 会话（{len(report.get('会话', []))} 个）"]
    for digest in report.get("会话", []):
        lines += ["", f"### {digest.get('会话', '')}", ""]
        for name, value in digest.items():
            if name in {"会话", "主动记录", "今日账本", "开口约定"}:
                continue
            lines.append(f"- {name}: {json.dumps(value, ensure_ascii=False)}")
        if digest.get("开口约定"):
            lines += ["", "**开口约定**"]
            for item in digest["开口约定"]:
                lines.append(f"- {item.get('约定于')}  {item.get('原因')}")
        if digest.get("今日账本"):
            lines += ["", "**今日账本（末尾 12 条）**"]
            for item in digest["今日账本"]:
                lines.append(
                    f"- {item.get('t', '')} [{item.get('kind', '')}] "
                    f"{str(item.get('text', ''))[:120]}"
                )
        record = digest.get("主动记录") or []
        if record:
            lines += [
                "",
                f"**主动记录（{len(record)} 条，按时间先后；含每次发出、沉默、约定与拦截的理由）**",
                "",
            ]
            for item in record:
                stage = item.get("阶段", "")
                if item.get("触发"):
                    stage += "｜" + str(item["触发"])
                lines.append(f"- {item.get('时间')} 【{stage}】{item.get('内容')}")
    logs = report.get("插件日志") or {}
    if logs:
        lines += ["", f"## 插件日志（{logs.get('文件', '')}）", ""]
        if logs.get("扫描末尾"):
            lines.append(
                f"扫描 {logs['扫描末尾']}，命中 {logs.get('命中行数', 0)} 行。"
            )
        if logs.get("说明"):
            lines.append(f"> {logs['说明']}")
        if logs.get("注意"):
            lines.append(f"> {logs['注意']}")
        lines += ["", "```"]
        lines += logs.get("内容", [])
        lines += ["```"]
    full = report.get("完整状态") or []
    if full:
        lines += [
            "",
            f"## 完整状态（{len(full)} 个会话）",
            "",
            "完整字段在随附的 json 文件里，逐条列出，未做取舍。",
        ]
    lines += ["", "---", report.get("说明", "")]
    return "\n".join(lines)


def _prune_exports(folder: Path, keep: int = 20) -> None:
    """Keep only the newest export files so troubleshooting cannot fill the disk.

    Args:
        folder: Export directory.
        keep: How many files (markdown and json counted together) to retain.

    Returns:
        None.
    """
    try:
        files = sorted(
            folder.glob("诊断包-*"), key=lambda item: item.stat().st_mtime, reverse=True
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def write_export(owner, umo: str | None = None, extra: dict | None = None) -> dict:
    """Write the diagnostic bundle to disk and return the created paths.

    Args:
        owner: Active plugin instance.
        umo: Optional single session to include.

    Returns:
        Dict with the markdown and json paths plus a short summary.
    """
    report = build_export(owner, umo, extra)
    target = (
        Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion")) / "exports"
    )
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    markdown_path = target / f"诊断包-{stamp}.md"
    json_path = target / f"诊断包-{stamp}.json"
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _prune_exports(target)
    return {
        "markdown": str(markdown_path),
        "json": str(json_path),
        "session_count": len(report.get("会话", [])),
        "version": report.get("运行环境", {}).get("插件版本", ""),
    }


async def inspect_runtime(owner):
    """Inspect configured routes and their live session dependencies.

    Args:
        owner: Active autonomous companion plugin.

    Returns:
        Timestamped per-target checks with explicit unknown states.
    """
    rows = []
    targets = owner.target_origins()
    for raw in owner.config.get("targets", []):
        raw = str(raw).strip()
        matches = [umo for umo in targets if umo == raw or umo.split(":", 2)[-1] == raw]
        if not matches:
            rows.append(
                {
                    "target": raw,
                    "checks": [
                        {
                            "name": "平台映射",
                            "status": "error",
                            "detail": "无法识别平台，请在设置选择 QQ 平台实例",
                        }
                    ],
                }
            )
    for umo in targets:
        checks = []
        platform = owner.context.get_platform_inst(umo.split(":", 1)[0])
        checks.append(
            {
                "name": "平台",
                "status": "ok"
                if platform and platform.status == PlatformStatus.RUNNING
                else "error",
                "detail": "运行中"
                if platform and platform.status == PlatformStatus.RUNNING
                else "实例不存在或未运行",
            }
        )
        try:
            manager = owner.context.conversation_manager
            cid = await asyncio.wait_for(manager.get_curr_conversation_id(umo), 5)
            conv = (
                await asyncio.wait_for(manager.get_conversation(umo, cid), 5)
                if cid
                else None
            )
            checks.append(
                {
                    "name": "会话",
                    "status": "ok" if conv else "error",
                    "detail": "已有当前会话" if conv else "请先与 bot 聊天",
                }
            )
            if conv and platform:
                persona_id, persona, _, _ = await asyncio.wait_for(
                    owner.context.persona_manager.resolve_selected_persona(
                        umo=umo,
                        conversation_persona_id=conv.persona_id,
                        platform_name=platform.meta().name,
                    ),
                    5,
                )
                checks.append(
                    {
                        "name": "人格",
                        "status": "ok"
                        if persona or persona_id in {None, "default", "[%None]"}
                        else "error",
                        "detail": str(persona_id or "默认人格"),
                    }
                )
        except Exception as exc:
            checks.append(
                {"name": "会话与人格", "status": "error", "detail": type(exc).__name__}
            )
        try:
            provider = await asyncio.wait_for(
                owner.context.get_current_chat_provider_id(umo), 5
            )
            checks.append(
                {
                    "name": "模型",
                    "status": "ok",
                    "detail": str(provider) + "（已配置，尚未调用）",
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "模型", "status": "error", "detail": type(exc).__name__}
            )
        state = owner.states.get(umo, {})
        probe = livingmemory_probe(owner, umo)
        conclusion = probe.get("结论", "")
        if conclusion == "可用":
            memory_state = "ok"
        elif conclusion:
            memory_state = "error"
        else:
            memory_state = "waiting"
        detail = "；".join(
            part
            for part in (
                conclusion,
                "接口：" + str(probe["接口"]) if probe.get("接口") else "",
                "上次读取：" + str(probe["上次读取结果"])
                if probe.get("上次读取结果")
                else "",
                "上次说明：" + str(probe["上次接口说明"])
                if probe.get("上次接口说明")
                else "",
            )
            if part
        ) or "尚未执行，首次主动或预览后可确认"
        checks.append(
            {"name": "记忆读取", "status": memory_state, "detail": detail}
        )
        checks.append(
            {
                "name": "陪伴适配",
                "status": "waiting",
                "detail": state.get(
                    "companion_status", "尚未执行，首次主动或预览后可确认"
                ),
            }
        )
        schedule_detail = (
            "正在行动"
            if umo in owner.running
            else "下次判断时间戳：" + str(state.get("next_due", "等待初始化"))
        )
        if state.get("next_speak_at", 0):
            schedule_detail += "；自己约定开口时间戳：" + str(state["next_speak_at"])
        checks.append(
            {
                "name": "调度",
                "status": "ok" if owner.config.get("enabled", True) else "waiting",
                "detail": schedule_detail,
            }
        )
        rows.append({"target": umo, "checks": checks})
    return {
        "checked_at": time.time(),
        "targets": rows,
        "note": "只读自检：未调用模型、未发送消息；记忆及陪伴状态为上次实际读取结果。",
    }
