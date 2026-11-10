"""开口约定队列与"心愿到点才说"的专项用例。

2026-11-09 凌晨那次事故：23:21 记下"明早叫白起床"（约定 480 分钟后，约 07:21），
23:57 又记下"明天再联系"（1440 分钟后），后者把前一个约定覆盖掉了；可"叫起床"
这句还挂在不带时间的 speak_wish 里，于是 00:54 那一轮主动醒来就把它说了出去。

这里守住两件事：
1. 多个开口约定各排各的，后写的不会顶掉先写的，到点只吃掉到期的那一条；
2. 带时间的心愿在到点之前连内容都不给模型看，避免被提前说出去。
"""

import importlib.util
import logging
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main",
    Path(__file__).parents[1] / "main.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def make_plugin(**overrides):
    plugin = object.__new__(module.AutonomousCompanion)
    plugin.config = {
        "self_scheduled_speak": True,
        "next_speak_min_minutes": 5,
        "next_speak_max_minutes": 1440,
        "speak_wish_ttl_hours": 24,
    }
    plugin.config.update(overrides)
    plugin.logger = logging.getLogger("test")
    return plugin


def make_state():
    return {
        "journal": [],
        "speak_wish": "",
        "speak_wish_at": 0,
        "speak_wish_kept": 0,
    }


def test_two_commitments_coexist_and_earliest_wakes():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_commitment(
        state, {"next_speak_minutes": 60, "next_speak_reason": "一小时后再聊"}
    )
    plugin.update_speak_commitment(
        state, {"next_speak_minutes": 480, "next_speak_reason": "明早叫他起床"}
    )
    slots = plugin.speak_schedule(state)
    assert len(slots) == 2
    assert [item["reason"] for item in slots] == ["一小时后再聊", "明早叫他起床"]
    # 旧字段仍然指向最早那条，旧的读取方不受影响。
    assert state["next_speak_at"] == slots[0]["at"]
    assert state["next_speak_reason"] == "一小时后再聊"
    assert plugin.wake_at({**state, "next_due": time.time() + 7200}) == slots[0]["at"]


def test_consume_pops_only_the_due_one():
    plugin = make_plugin()
    state = make_state()
    now = time.time()
    plugin.store_speak_schedule(
        state,
        [
            {"at": now - 10, "reason": "到点的那条", "set_at": now - 600},
            {"at": now + 600, "reason": "还没到点的", "set_at": now - 60},
        ],
    )
    assert plugin.consume_speak_commitment(state) == "到点的那条"
    left = plugin.speak_schedule(state)
    assert [item["reason"] for item in left] == ["还没到点的"]
    assert state["next_speak_at"] == left[0]["at"]
    # 剩下那条没到点，不会被吃掉。
    assert plugin.consume_speak_commitment(state) == ""
    assert len(plugin.speak_schedule(state)) == 1


def test_same_payload_twice_does_not_duplicate():
    plugin = make_plugin()
    state = make_state()
    payload = {"next_speak_minutes": 30, "next_speak_reason": "半小时后说读后感"}
    # 决策与行动会各报一次同一条约定。
    plugin.update_speak_commitment(state, payload)
    plugin.update_speak_commitment(state, payload)
    assert len(plugin.speak_schedule(state)) == 1


def test_cancel_clears_the_whole_queue():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30, "next_speak_reason": "a"})
    plugin.update_speak_commitment(state, {"next_speak_minutes": 60, "next_speak_reason": "b"})
    plugin.update_speak_commitment(state, {"next_speak_minutes": 0})
    assert plugin.speak_schedule(state) == []
    assert "next_speak_at" not in state
    assert "speak_schedule" not in state
    assert state["journal"][-1]["action"] == "speak_cancelled"


def test_pause_pushes_every_commitment_back():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30, "next_speak_reason": "a"})
    plugin.update_speak_commitment(state, {"next_speak_minutes": 90, "next_speak_reason": "b"})
    state["pause_mode"] = "all"
    state["pause_until"] = time.time() + 7200
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30, "next_speak_reason": "c"})
    slots = plugin.speak_schedule(state)
    assert len(slots) == 3
    assert all(item["at"] >= state["pause_until"] for item in slots)


def test_legacy_single_slot_still_readable_and_migrated():
    plugin = make_plugin()
    state = make_state()
    state["next_speak_at"] = time.time() - 1
    state["next_speak_reason"] = "旧数据里的一条约定"
    assert plugin.next_speak_time(state) == state["next_speak_at"]
    assert plugin.consume_speak_commitment(state) == "旧数据里的一条约定"
    assert plugin.speak_schedule(state) == []
    assert "next_speak_at" not in state


def test_queue_is_capped_by_config():
    plugin = make_plugin(speak_schedule_limit=2)
    state = make_state()
    for minutes in (10, 20, 30, 40):
        plugin.update_speak_commitment(
            state, {"next_speak_minutes": minutes, "next_speak_reason": f"{minutes}分钟"}
        )
    slots = plugin.speak_schedule(state)
    assert len(slots) == 2
    # 留下的是最早的两条，晚的才丢。
    assert [item["reason"] for item in slots] == ["10分钟", "20分钟"]


def test_speak_plan_text_lists_pending_times():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_commitment(state, {"next_speak_minutes": 30, "next_speak_reason": "一条"})
    text = plugin.speak_plan_text(state)
    assert "你已和自己约定于" in text and "一条" in text
    # 过期的约定不再出现在提示里。
    plugin.store_speak_schedule(state, [{"at": time.time() - 10, "reason": "过期", "set_at": 0}])
    assert plugin.speak_plan_text(state) == ""


def test_wish_with_future_time_is_hidden_until_due():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_wish(
        state,
        {"speak_wish": "明早按时叫醒他，别让他赖床", "speak_wish_after_minutes": 360},
    )
    assert state["speak_wish_due_at"] > time.time()
    assert plugin.wish_ready(state) is False
    text = plugin.wish_prompt_text(state)
    # 没到点连内容都不给：这正是凌晨被说出去的原因。
    assert "叫醒他" not in text
    assert "还没到能说的时候" in text
    # 到点后内容才出现，并提示可以说。
    state["speak_wish_due_at"] = time.time() - 1
    assert plugin.wish_ready(state) is True
    assert "叫醒他" in plugin.wish_prompt_text(state)


def test_wish_without_time_is_speakable_right_away():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_wish(state, {"speak_wish": "想问问他今天累不累"})
    assert state["speak_wish_due_at"] == 0
    assert plugin.wish_ready(state) is True
    assert "想问问他今天累不累" in plugin.wish_prompt_text(state)


def test_wish_clear_drops_its_time():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_wish(
        state, {"speak_wish": "明早叫他", "speak_wish_after_minutes": 480}
    )
    plugin.update_speak_wish(state, {"speak_wish_clear": True})
    assert not state.get("speak_wish")
    assert not state.get("speak_wish_due_at")
    assert plugin.wish_prompt_text(state) == ""


def test_wish_time_can_be_moved_without_rewriting_the_text():
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_wish(
        state, {"speak_wish": "明早叫他", "speak_wish_after_minutes": 480}
    )
    first = state["speak_wish_due_at"]
    plugin.update_speak_wish(
        state, {"speak_wish": "明早叫他", "speak_wish_after_minutes": 60}
    )
    assert state["speak_wish_due_at"] < first
    assert state["speak_wish"] == "明早叫他"


@pytest.mark.parametrize("bad", [True, "soon", {"x": 1}, float("nan")])
def test_bad_wish_time_is_ignored(bad):
    plugin = make_plugin()
    state = make_state()
    plugin.update_speak_wish(
        state, {"speak_wish": "一句想说的话", "speak_wish_after_minutes": bad}
    )
    assert state["speak_wish"] == "一句想说的话"
    assert state["speak_wish_due_at"] == 0
