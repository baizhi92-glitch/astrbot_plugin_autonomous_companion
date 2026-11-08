"""Compact factual summary of the last autonomous action for normal chat."""

import json
import time

# Read-only lookup tools add noise; the task line already says what was consulted.
_SKIP_TOOLS = {
    "search_task_skills",
    "list_templates",
    "get_template",
    "get_anima_format",
    "search_tags",
    "read_file",
    "list_files",
}

_STATUS = {
    "completed": "做成了",
    "tools": "动手做了",
    "failed": "没做成",
    "blocked": "没做完，有步骤被卡住",
    "cancelled": "做到一半被新消息打断",
    "deferred": "这轮先搁着，没继续",
    "silent": "这轮没动手",
}
_VERIFICATION = {
    "verified": "已核验",
    "unverified": "还有步骤没核验",
    "unknown": "结果无法确认",
}
_ARTIFACT_STATUS = {
    "sent": "已发送",
    "saved": "已保存",
    "created": "已生成",
    "generated": "已生成",
    "completed": "已完成",
}


def _clip(value, limit):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _receipt_line(receipt):
    tool = str(receipt.get("tool") or "").strip()
    if not tool or tool in _SKIP_TOOLS:
        return ""
    raw = receipt.get("result")
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
    if isinstance(data, dict):
        if data.get("ok") is False or data.get("saved") is False:
            reason = data.get("error") or data.get("reason") or "没有成功"
            return f"{tool}：失败，{_clip(reason, 150)}"
        if data.get("saved") is True:
            title = data.get("title") or data.get("path") or "文件"
            return f"{tool}：保存成功《{_clip(title, 80)}》"
        if data.get("status") == "completed" and data.get("outputs"):
            return f"{tool}：完成，产出 {len(data.get('outputs') or [])} 项"
        if data.get("ok") is True:
            return f"{tool}：成功"
        if data.get("error"):
            return f"{tool}：失败，{_clip(data.get('error'), 150)}"
        if data.get("status"):
            return f"{tool}：{_clip(data.get('status'), 80)}"
        text = json.dumps(data, ensure_ascii=False)
    else:
        text = str(raw or "")
    return f"{tool}：{_clip(text, 150)}" if text else ""


def action_memory_summary(state):
    """One-line natural-language fact line for long-term bot-action memory.

    Unlike recent_action_context, this drops prompt framing entirely and only
    keeps what the bot actually did or failed to do, so MemoryCompanion can
    store it as a self_action fact without internal vocabulary.
    """
    if not isinstance(state, dict):
        return ""
    last = state.get("last_turn")
    if not isinstance(last, dict):
        return ""
    receipts = last.get("tool_receipts")
    if str(last.get("action") or "") not in {"tools", "create"} and not receipts:
        return ""
    task = _clip(last.get("task") or last.get("summary") or "", 200)
    if not task:
        return ""
    status = str(last.get("status") or "")
    verification = str(last.get("verification") or "")
    outcome = _STATUS.get(status, status or "已结束")
    if verification:
        outcome += "（" + _VERIFICATION.get(verification, verification) + "）"
    reason = _clip(
        last.get("verification_reason")
        or last.get("error")
        or last.get("reason")
        or "",
        200,
    )
    summary = _clip(last.get("summary") or "", 200)
    if summary in {task, reason}:
        summary = ""
    parts = [f"自主行动“{task}”：{outcome}"]
    if reason and reason != task:
        parts.append(reason)
    if summary:
        parts.append(summary)
    if isinstance(receipts, list):
        action_lines = []
        for receipt in receipts[-8:]:
            if isinstance(receipt, dict):
                line = _receipt_line(receipt)
                if line and line not in action_lines:
                    action_lines.append(line)
        parts.extend(action_lines[-3:])
    artifacts = state.get("task_artifacts")
    if isinstance(artifacts, list):
        seen = set()
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            if item.get("task") != last.get("task"):
                continue
            producer = str(item.get("producer") or "").strip()
            label = {"run_template": "图片", "autonomous_save_work": "作品"}.get(
                producer, ""
            )
            if not label:
                continue
            title = _clip(item.get("title") or item.get("name"), 60)
            if title:
                label += f"《{title}》"
            raw_status = str(item.get("status") or "registered")
            text = f"{label}：{_ARTIFACT_STATUS.get(raw_status, raw_status)}"
            if text not in seen:
                seen.add(text)
                parts.append(text)
    # Drop tool names: long-term memory should read like a fact, not a receipt.
    cleaned = []
    for part in parts:
        for tool in (
            "autonomous_save_work",
            "autonomous_long_task",
            "autonomous_dsh_cli",
            "autonomous_dsh_task",
            "autonomous_send_artifact",
            "run_template",
        ):
            part = part.replace(tool + "：", "")
        part = part.strip()
        if part and part not in cleaned:
            cleaned.append(part)
    # Prioritize facts over receipts: keep first 800 chars of core facts, last 400 of tools
    core = [c for c in cleaned if not any(r in c for r in ["保存成功", "失败，", "已发送", "完成，"])]
    receipts = [c for c in cleaned if any(r in c for r in ["保存成功", "失败，", "已发送", "完成，"])]
    result = core[:6] + receipts[:2]
    return "；".join(result)[:1200]


def recent_action_context(state, now=None):
    """Return a short prompt block describing the last proactive action.

    Returns an empty string when the last action is missing, still running,
    or older than one hour, so normal chat does not confuse old work with now.
    """
    if not isinstance(state, dict):
        return ""
    last = state.get("last_turn")
    if not isinstance(last, dict):
        return ""
    finished = last.get("finished_at")
    if not isinstance(finished, (int, float)) or not finished:
        return ""
    now = time.time() if now is None else now
    if now - finished > 3600:
        return ""
    status = str(last.get("status") or "")
    if status == "running":
        return ""
    receipts = last.get("tool_receipts")
    if str(last.get("action") or "") not in {"tools", "create"} and not receipts:
        return ""
    task = _clip(last.get("task") or last.get("summary") or "", 400)
    if not task:
        return ""
    verification = str(last.get("verification") or "")
    reason = _clip(
        last.get("verification_reason")
        or last.get("error")
        or last.get("reason")
        or "",
        300,
    )
    summary = _clip(last.get("summary") or "", 300)
    if summary in {task, reason}:
        summary = ""

    lines = [
        "【最近一次主动做事的真实记录】",
        f"任务：{task}",
    ]
    result = _STATUS.get(status, status or "已结束")
    if verification:
        result += "（" + _VERIFICATION.get(verification, verification) + "）"
    lines.append(f"结果：{result}")
    if reason:
        lines.append(f"卡点：{reason}")
    if summary:
        lines.append(f"简述：{summary}")

    if isinstance(receipts, list):
        seen = set()
        action_lines = []
        for receipt in receipts[-8:]:
            if isinstance(receipt, dict):
                line = _receipt_line(receipt)
                if line and line not in seen:
                    seen.add(line)
                    action_lines.append(line)
        if action_lines:
            lines.append("实际动作：")
            lines.extend("- " + line for line in action_lines[-4:])

    artifacts = state.get("task_artifacts")
    artifact_lines = []
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            if item.get("task") != last.get("task"):
                continue
            producer = str(item.get("producer") or "").strip()
            label = {
                "run_template": "图片",
                "autonomous_save_work": "作品",
            }.get(producer, "产物")
            title = _clip(item.get("title") or item.get("name"), 60)
            if title:
                label += f"《{title}》"
            raw_status = str(item.get("status") or "registered")
            artifact_status = _ARTIFACT_STATUS.get(raw_status, raw_status)
            text = _clip(f"{label}：{artifact_status}", 120)
            if text and text not in artifact_lines:
                artifact_lines.append(text)
    if artifact_lines:
        lines.append("本轮产物：")
        lines.extend("- " + line for line in artifact_lines[-3:])

    lines.append(
        "这些是你自己刚做的事，不是用户说的话。用户问起时按这个回答，用自然的话说你做了什么、卡在哪；"
        "没问就不要主动汇报。不要向用户复述工具名、JSON、路径或内部术语；"
        "不要把这轮的事和别的上下文混在一起。如果同一件事也出现在待办里，"
        "这里记录的是刚做完或刚被打断的那次，不要当成新任务再做一遍。"
    )
    return "\n".join(lines)