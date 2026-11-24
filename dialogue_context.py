"""Bounded dispatched dialogue, separate from model reports and tool logs."""

import json
import time
from datetime import datetime

REAL_TIME_RULES = (
    "主动消息以本轮现实时间为准，不以旧聊天、人格小剧场或待续计划里的时间为准。"
    "保留角色口吻、关系和真实偏好，但不要默认上一幕仍在发生。"
    "没有当前证据或用户明确要求续演，不要继续假装见面、出门、一起点餐、等人或补写共同经历。"
    "旧邀约和随口应答只说明当时说过什么，不证明后来成行或现在仍在进行。"
    "跨日或隔了很久，只能把旧话当回忆或另提新话题；没合适的话可以不发。"
    "即使在短时续行窗口内，也只能补充仍适用的内容，不能替用户推进剧情。"
)


def real_time_context(state, continuation=False, now=None):
    """Render the real clock and the age of the latest observed user message.

    Args:
        state: Per-target state containing the last inbound timestamp.
        continuation: Whether a previously opened short continuation is active.
        now: Optional timezone-aware clock for tests; defaults to server local time.

    Returns:
        A prompt block that distinguishes the current turn from past dialogue.
    """
    now = now or datetime.now().astimezone()
    weekdays = "一二三四五六日"
    inbound = state.get("last_inbound") or 0
    latest = "无可靠记录；不要把历史中的最后一句当作刚说的"
    try:
        stamp = float(inbound)
        if 0 < stamp <= now.timestamp():
            moment = datetime.fromtimestamp(stamp, now.tzinfo)
            minutes = int((now.timestamp() - stamp) // 60)
            hours, minutes = divmod(minutes, 60)
            day_note = "，不是今天的消息" if moment.date() != now.date() else ""
            latest = f"{moment.isoformat(timespec='seconds')}，距今 {hours} 小时 {minutes} 分钟{day_note}"
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    window = "原短时续行窗口内" if continuation else "新的主动轮，不在旧续行窗口内"
    return (
        f"【现实时间】{now.isoformat(timespec='seconds')}（星期{weekdays[now.weekday()]}，Bot 服务器本地时间）\n"
        f"最近观察到用户发言：{latest}。本轮：{window}。\n"
    )


def remember_dispatch(event, text):
    entries = event.state.setdefault("visible_dispatches", [])
    entries.append({"cid": event.cid, "at": time.time(), "text": text[:8000]})
    event.state["visible_dispatches"] = entries[-12:]


def dispatched_context(state, cid):
    entries = [
        item for item in state.get("visible_dispatches", []) if item.get("cid") == cid
    ][-8:]
    remaining = 12000
    retained = []
    for item in reversed(entries):
        content = item.get("text", "")[-remaining:]
        stamp = item.get("at")
        try:
            at = (
                datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
            )
        except (TypeError, ValueError, OverflowError, OSError):
            at = None
        retained.append({"at": at, "text": content})
        remaining -= len(content)
        if remaining <= 0:
            break
    return (
        "最近实际向当前对象提交的正文（旧到新；不代表用户已读）：\n"
        + json.dumps(list(reversed(retained)), ensure_ascii=False)
        + "\n短时续行可以承接仍适用的话；这些历史不是当前情景。不得把助手发言当成用户回复。"
        "没有新回复时，不要推断用户的状态：既不要假定用户醒着，也不要假定用户睡着、离开或没看见。"
        "不要写“你醒了再说”“你睡了吧”“你还没醒”“晚安”这类话；只能说你这边发生的事，"
        "或者换成不要求立即回应的新内容。若没有可说的，宁可不说。"
        "若刚说不催、不用现在看、明天再看，后续不要马上催看或索取感想；可继续后台创作。"
        "作品标题、创作设定和旧计划都不是用户当前状态；作品状态以真实保存/生成回执为准，不照抄旧计划中的未完成状态。\n"
    )
