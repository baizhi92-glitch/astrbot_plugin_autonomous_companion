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
        + "\n续行要承接这些已说出口的话。不得把助手发言当成用户回复。"
        "没有新回复时，不要推断用户的状态：既不要假定用户醒着，也不要假定用户睡着、离开或没看见。"
        "不要写“你醒了再说”“你睡了吧”“你还没醒”“晚安”这类话；只能说你这边发生的事，"
        "或者换成不要求立即回应的新内容。若没有可说的，宁可不说。"
        "若刚说不催、不用现在看、明天再看，后续不要马上催看或索取感想；可继续后台创作。"
        "作品标题、创作设定和旧计划都不是用户当前状态；作品状态以真实保存/生成回执为准，不照抄旧计划中的未完成状态。\n"
    )
