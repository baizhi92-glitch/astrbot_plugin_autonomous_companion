"""紧凑行动清单：把已经真实发生过的调用压成几行，放进提示里。

目的只有一个：让她不必凭印象重做同一件事。清单只写已经发生的事，
明确标注“不是待办”，长度封顶，超了就按最近的留。
"""

from __future__ import annotations

import json

STATUS_WORDS = {
    "verified": "已核验",
    "success": "成功",
    "completed": "完成",
    "pending": "等结果",
    "attempted": "进行中",
    "failed": "失败",
    "error": "报错",
    "unverified": "未见回执",
}


def _args_summary(receipt):
    """Return a short, single-line rendering of the call arguments."""
    raw = receipt.get("args")
    if isinstance(raw, str) and raw.strip():
        return raw
    if isinstance(raw, dict) and raw:
        try:
            return json.dumps(raw, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return ""


def receipt_lines(event, limit=8):
    """Return newest-first receipt lines for the current turn."""
    lines = []
    for receipt in reversed(getattr(event, "action_receipts", []) or []):
        if not isinstance(receipt, dict) or not receipt.get("tool"):
            continue
        status = STATUS_WORDS.get(str(receipt.get("status", "")), str(receipt.get("status", "")))
        line = f"- {receipt['tool']} · {_args_summary(receipt)[:60] or '无参数'} · {status}"
        result = receipt.get("result")
        if isinstance(result, str) and result.strip():
            first = " ".join(result.split())[:100]
            line += f" · 结果：{first}"
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def action_digest(event, state, budget=1500):
    """Render the compact, bounded action list for the action prompt.

    Args:
        event: Current autonomous event holding action receipts.
        state: Per-target state with the recent journal.
        budget: Character cap; zero or less disables the block.

    Returns:
        Text block for the prompt, or an empty string when there is nothing
        recorded or the block is disabled.
    """
    if budget <= 0:
        return ""
    lines = receipt_lines(event)
    journal = [
        f"- {item.get('action')} · {str(item.get('summary', ''))[:80]}"
        for item in (state.get("journal") or [])[-4:]
        if isinstance(item, dict) and item.get("summary")
    ]
    if not lines and not journal:
        return ""
    body = []
    if lines:
        body.append("本轮已经实际发生的调用（不是待办，不要重做）：")
        body.extend(lines)
    if journal:
        body.append("最近几轮做过的事（已经发生，不要重做）：")
        body.extend(journal)
    text = "\n".join(body)
    if len(text) > budget:
        text = text[:budget].rstrip() + "\n…（清单已截断）"
    return text
