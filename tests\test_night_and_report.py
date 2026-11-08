"""昼夜额度分池与早报的专项用例。

这些用例不依赖真实时钟：夜间判定通过显式传入 now 或替换 night_pool_key 完成，
早报到点判定也传入 now，避免在半夜跑测试时结果随环境变化。
"""

import importlib.util
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main",
    Path(__file__).parents[1] / "main.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

from data.plugins.astrbot_plugin_autonomous_companion import daily, morning  # noqa: E402


def make_owner(**overrides):
    """Build a minimal owner exposing config and the plugin's number() clamp."""
    config = {
        "split_night_budget": True,
        "night_window_start_hour": 23,
        "night_window_end_hour": 8,
        "night_budget_percent": 40,
        "daily_tool_limit": 300,
        "daily_round_limit": 96,
        "daily_check_limit": 144,
        "daily_message_limit": 200,
        "morning_report_enabled": True,
        "morning_report_hour": 8,
        "morning_report_min_gap_hours": 20,
        "morning_report_max_chars": 400,
        "morning_report_timeout_seconds": 60,
    }
    config.update(overrides)
    owner = SimpleNamespace(config=config)

    def number(key, default, low, high):
        try:
            return max(low, min(high, int(config.get(key, default))))
        except (TypeError, ValueError):
            return default

    owner.number = number
    return owner


def at(hour, minute=0, day=8):
    return datetime(2026, 11, day, hour, minute).astimezone()


def test_night_pool_key_spans_midnight():
    owner = make_owner()
    assert daily.night_pool_key(owner, at(23, 30, day=7)) == "2026-11-07"
    assert daily.night_pool_key(owner, at(0, 10, day=8)) == "2026-11-07"
    assert daily.night_pool_key(owner, at(7, 50, day=8)) == "2026-11-07"
    assert daily.night_pool_key(owner, at(8, 0, day=8)) == ""
    assert daily.night_pool_key(owner, at(12, 0, day=8)) == ""


def test_night_pool_key_disabled_returns_empty():
    owner = make_owner(split_night_budget=False)
    assert daily.night_pool_key(owner, at(2)) == ""


def test_night_end_timestamp_rolls_forward():
    owner = make_owner()
    assert daily.night_end_timestamp(owner, at(2)) == at(8).timestamp()
    assert daily.night_end_timestamp(owner, at(9)) == at(8, day=9).timestamp()


def test_night_limit_is_configured_share(monkeypatch):
    owner = make_owner()
    monkeypatch.setattr(daily, "night_pool_key", lambda owner, now=None: "n1")
    assert daily.pool_limit(owner, "daily_tool_limit", 300) == 120
    assert daily.pool_limit(owner, "daily_round_limit", 96) == 38
    monkeypatch.setattr(daily, "night_pool_key", lambda owner, now=None: "")
    assert daily.pool_limit(owner, "daily_tool_limit", 300) == 300


def test_count_feeds_day_and_night_pools(monkeypatch):
    owner = make_owner()
    state = {}
    monkeypatch.setattr(daily, "night_pool_key", lambda owner, now=None: "n1")
    daily.count(owner, state, "tools")
    daily.count(owner, state, "tools")
    assert daily.usage_for(state)["tools"] == 2
    assert state["night_usage"]["tools"] == 2
    assert state["night_usage"]["pool"] == "n1"


def test_exhausted_reads_night_pool_not_day_total(monkeypatch):
    owner = make_owner()
    state = {}
    monkeypatch.setattr(daily, "night_pool_key", lambda owner, now=None: "n1")
    daily.usage_for(state)["tools"] = 290
    assert daily.exhausted(owner, state, "tools", "daily_tool_limit", 300) is False
    state["night_usage"] = {"pool": "n1", "tools": 120}
    assert daily.exhausted(owner, state, "tools", "daily_tool_limit", 300) is True


def test_pool_snapshot_reports_both_sides(monkeypatch):
    owner = make_owner()
    state = {}
    monkeypatch.setattr(daily, "night_pool_key", lambda owner, now=None: "n1")
    state["night_usage"] = {"pool": "n1", "tools": 12, "rounds": 3, "messages": 1}
    daily.usage_for(state)["tools"] = 250
    snapshot = daily.pool_snapshot(owner, state)
    assert snapshot["pool"] == "night"
    assert snapshot["tools"] == 12
    assert snapshot["limit_tools"] == 120
    assert snapshot["today"]["tools"] == 250


def test_report_due_respects_hour_gap_and_day():
    owner = make_owner()
    state = {}
    assert morning.report_due(owner, state, at(7)) is False
    assert morning.report_due(owner, state, at(8, 5)) is True
    state["morning_report"] = {"day": "2026-11-08", "at": at(8, 5).timestamp()}
    assert morning.report_due(owner, state, at(20)) is False
    state["morning_report"] = {
        "day": "2026-11-07",
        "at": at(8, day=7).timestamp(),
    }
    assert morning.report_due(owner, state, at(8, 5, day=8)) is True
    # 间隔不足二十小时时即便换了日期也不发，避免重复打扰
    state["morning_report"] = {"day": "2026-11-07", "at": at(6, day=8).timestamp()}
    assert morning.report_due(owner, state, at(8, 5, day=8)) is False


def test_report_disabled_by_switch():
    owner = make_owner(morning_report_enabled=False)
    assert morning.report_due(owner, {}, at(9)) is False


def test_digest_reports_real_records_and_alerts():
    owner = make_owner()
    now = at(8, 5)
    state = {
        "journal": [
            {
                "time": at(2).isoformat(timespec="seconds"),
                "action": "dispatched",
                "summary": "发了一条早安",
            }
        ],
        "stalls": {
            "draw": {"count": 3, "reason": "接口超时", "signature": "s"},
        },
        "outbox": [{"title": "等你确认的封面", "status": "pending"}],
        "night_usage": {"pool": "2026-11-07", "tools": 120, "rounds": 4, "messages": 2},
        "next_task": "把第二章写完",
    }
    digest = morning.build_digest(owner, state, now)
    assert "发了一条早安" in digest["actions"][0]
    assert digest["failures"][0]["reason"] == "接口超时"
    assert digest["waiting"] == ["等你确认的封面"]
    assert digest["night"]["tools"] == 120
    assert len(digest["alerts"]) == 2
    text = morning.digest_text(digest)
    assert "接口超时" in text
    assert "只作素材" in text
    fallback = morning.fallback_text(digest)
    assert fallback.startswith("早上好")
    assert "接口超时" in fallback


def make_plugin():
    plugin = object.__new__(module.AutonomousCompanion)
    plugin.config = {
        "enabled": True,
        "morning_report_enabled": True,
        "morning_report_hour": 0,
        "morning_report_min_gap_hours": 20,
        "morning_report_max_chars": 400,
        "morning_report_timeout_seconds": 30,
        "split_night_budget": False,
    }
    plugin.logger = logging.getLogger("test-night")
    plugin.states = {}
    plugin.reporting = {}

    def number(key, default, low, high):
        try:
            return max(low, min(high, int(plugin.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    plugin.number = number
    plugin.save = AsyncMock()
    plugin.deliver = AsyncMock()
    plugin.journal = lambda state, action, summary: state.setdefault(
        "journal", []
    ).append({"action": action, "summary": summary})
    plugin.context = SimpleNamespace(
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="昨晚我在改封面。")),
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid")
        ),
    )
    event = SimpleNamespace(
        set_extra=lambda *args: None,
        quiet=False,
        state=None,
    )
    plugin.prepare = AsyncMock(
        return_value=(event, {"prompt": "persona"}, "provider", [], "memory")
    )
    state = plugin.new_state()
    state["stalls"] = {"draw": {"count": 3, "reason": "接口超时"}}
    plugin.states["test:FriendMessage:123"] = state
    plugin.target_origins = lambda: ["test:FriendMessage:123"]
    return plugin, state, event


@pytest.mark.asyncio
async def test_morning_report_uses_model_words_with_alert_prefix():
    plugin, state, event = make_plugin()
    await module.AutonomousCompanion.morning_report(plugin, "test:FriendMessage:123")
    plugin.deliver.assert_awaited_once()
    chain = plugin.deliver.call_args.args[1]
    text = "".join(part.text for part in chain.chain)
    assert text.startswith("⚠️")
    assert "昨晚我在改封面" in text
    assert state["morning_report"]["day"]
    assert state["journal"][-1]["action"] == "morning_report"


@pytest.mark.asyncio
async def test_morning_report_falls_back_when_model_fails():
    plugin, state, _ = make_plugin()
    plugin.context.llm_generate = AsyncMock(side_effect=RuntimeError("boom"))
    await module.AutonomousCompanion.morning_report(plugin, "test:FriendMessage:123")
    plugin.deliver.assert_awaited_once()
    chain = plugin.deliver.call_args.args[1]
    assert "早上好" in "".join(part.text for part in chain.chain)
    assert state["morning_report"]["day"]


@pytest.mark.asyncio
async def test_morning_report_does_not_record_failed_delivery():
    plugin, state, _ = make_plugin()
    plugin.deliver = AsyncMock(side_effect=RuntimeError("no platform"))
    await module.AutonomousCompanion.morning_report(plugin, "test:FriendMessage:123")
    assert "morning_report" not in state


@pytest.mark.asyncio
async def test_morning_report_skips_while_paused():
    plugin, state, _ = make_plugin()
    state["pause_until"] = time.time() + 600
    await module.AutonomousCompanion.morning_report(plugin, "test:FriendMessage:123")
    plugin.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_report_waits_for_open_window():
    plugin, state, _ = make_plugin()
    state["window_end"] = time.time() + 600
    await module.AutonomousCompanion.morning_report(plugin, "test:FriendMessage:123")
    plugin.deliver.assert_not_awaited()
    assert state["window_end"] > time.time()
