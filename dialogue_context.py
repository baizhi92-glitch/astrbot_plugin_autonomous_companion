"""Bounded dispatched dialogue, separate from model reports and tool logs."""

import json
import time


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
        retained.append({"at": item.get("at"), "text": content})
        remaining -= len(content)
        if remaining <= 0:
            break
    return (
        "最近实际向当前对象提交的正文（旧到新；不代表用户已读）：\n"
        + json.dumps(list(reversed(retained)), ensure_ascii=False)
        + "\n续行要承接这些已说出口的话。不得把助手发言当成用户回复；没有新回复不能假定用户醒着、同意或参与。"
        "若刚说不催、不用现在看、明天再看，后续不要马上催看或索取感想；可继续后台创作，"
        "想继续聊天也应换成不要求立即回应的新内容。作品状态以真实保存/生成回执为准，不照抄旧计划中的未完成状态。\n"
    )
