"""第三批改动：紧凑行动清单（让她知道自己做过什么）。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main",
    Path(__file__).parents[1] / "main.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

from data.plugins.astrbot_plugin_autonomous_companion import action_digest  # noqa: E402


def make_event(receipts):
    return SimpleNamespace(action_receipts=receipts)


def test_digest_lists_receipts_newest_first():
    event = make_event(
        [
            {"tool": "draw", "status": "success", "args": '{"prompt": "海边"}', "result": "已生成 1 张"},
            {"tool": "send_artifact", "status": "verified", "args": '{"id": "a1"}', "result": "已投递"},
        ]
    )
    text = action_digest.action_digest(event, {})
    assert "不是待办" in text
    assert "send_artifact" in text.split("\n")[1]
    assert "draw" in text.split("\n")[2]
    assert "已核验" in text
    assert "结果：已生成 1 张" in text


def test_digest_includes_recent_journal():
    event = make_event([])
    state = {"journal": [{"action": "dispatched", "summary": "发了一条早安"}]}
    text = action_digest.action_digest(event, state)
    assert "发了一条早安" in text
    assert "不要重做" in text


def test_digest_empty_when_nothing_recorded():
    assert action_digest.action_digest(make_event([]), {}) == ""


def test_digest_respects_budget_and_switch():
    event = make_event(
        [{"tool": "draw", "status": "success", "args": "x" * 100, "result": "y" * 200}]
    )
    short = action_digest.action_digest(event, {}, budget=80)
    assert len(short) <= 120
    assert "清单已截断" in short
    assert action_digest.action_digest(event, {}, budget=0) == ""


def test_digest_skips_malformed_receipts():
    event = make_event([{"status": "success"}, "junk", {"tool": "", "status": "x"}])
    assert action_digest.action_digest(event, {}) == ""


def test_receipt_carries_args_for_the_digest():
    """BudgetTool 记录回执时要把参数摘要带上，否则清单里看不到调用内容。"""
    import inspect

    source = inspect.getsource(module.BudgetTool.call)
    assert '"args"' in source
