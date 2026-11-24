"""Exercise real dispatch boundaries with offline transports and temporary snapshots."""

import asyncio
import hashlib
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.astrbot_plugin_autonomous_companion import main, outbox

UMO = "test:FriendMessage:123"


@pytest.fixture
def transport():
    """Build a plugin with an in-memory store and replaceable platform transport."""
    owner = object.__new__(main.AutonomousCompanion)
    owner.config = {
        "companion_integration": False,
        "split_night_budget": False,
        "proactive_segment_interval_ms": 0,
        "morning_report_hour": 0,
    }
    owner.logger = logging.getLogger(__name__)
    owner.revisions = {}
    owner.running = {}
    owner.reporting = {}
    owner.chat_tasks = {}
    owner.busy = {}
    owner.stopping = False
    owner.save = AsyncMock()
    owner.memory_bridge = lambda _: None
    owner.target_origins = lambda: [UMO]
    owner.quiet_hour_now = lambda _: False
    owner.context = SimpleNamespace(
        get_config=lambda _: {},
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="Saved a draft.")
        ),
        send_message=AsyncMock(return_value=True),
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=None),
        ),
    )
    state = owner.new_state()
    owner.states = {UMO: state}
    event = SimpleNamespace(
        owner=owner,
        state=state,
        unified_msg_origin=UMO,
        session=UMO,
        revision=0,
        cid="cid",
        deliveries=0,
        quiet=False,
        set_extra=lambda *_: None,
    )
    event.send = main.AutonomousEvent.send.__get__(event)
    owner.prepare = AsyncMock(return_value=(event, {"prompt": ""}, "provider", [], ""))
    return owner, event, state


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "revision"])
async def test_last_lookup_rechecks_state_before_transport(transport, change):
    owner, event, state = transport
    manager = owner.context.conversation_manager

    async def current(_):
        if manager.get_curr_conversation_id.await_count == 3:
            if change == "pause":
                state.update(pause_mode="messages", pause_until=time.time() + 600)
            else:
                owner.revisions[UMO] = 1
        return "cid"

    manager.get_curr_conversation_id.side_effect = current
    outcome = outbox.DispatchOutcome()
    error = RuntimeError if change == "pause" else asyncio.CancelledError
    with pytest.raises(error):
        await owner.deliver(
            event, main.MessageChain().message("hello"), outcome=outcome
        )
    owner.context.send_message.assert_not_awaited()
    assert outcome.sent == [] and not outcome.uncertain
    assert not any(row["action"] == "dispatch_attempt" for row in state["journal"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_quiet_draft_retry_only_keeps_known_unsent_tail(transport, ambiguous):
    owner, event, state = transport
    state["pending_message"] = {
        "content": "first\n\nsecond\n\nthird",
        "saved_at": time.time(),
        "expires_at": time.time() + 600,
    }
    owner.context.send_message.side_effect = [
        True,
        RuntimeError("transport broke") if ambiguous else False,
    ]
    await owner.flush_pending_message(state, event)
    expected = ["third"] if ambiguous else ["second", "third"]
    assert state["pending_message"]["remaining_segments"] == expected
    owner.context.send_message.reset_mock(side_effect=True)
    await owner.flush_pending_message(state, event)
    assert [
        c.args[1].get_plain_text() for c in owner.context.send_message.call_args_list
    ] == expected
    assert "pending_message" not in state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["guarded", "no_route", "partial", "transport_error", "post_send_error"]
)
async def test_outbox_receipt_matches_transport(transport, case):
    owner, _, state = transport
    item = {
        "content": "first\n\nsecond" if case == "partial" else "body",
        "status": "pending",
    }
    if case == "guarded":
        owner._guard_user_state_claims = AsyncMock(return_value="")
        expected, error = "failed", ValueError
    elif case == "partial":
        owner.context.send_message.side_effect = [True, False]
        expected, error = "uncertain", RuntimeError
    elif case == "no_route":
        owner.context.send_message.return_value = False
        expected, error = "failed", RuntimeError
    elif case == "transport_error":
        owner.context.send_message.side_effect = RuntimeError("transport failed")
        expected, error = "uncertain", RuntimeError
    else:

        def broken_memory(_):
            raise RuntimeError("post-send")

        owner.memory_bridge = broken_memory
        expected, error = "sent", RuntimeError
    with pytest.raises(error):
        await main.CompanionPageAPI(owner).send_output(UMO, item)
    assert item["status"] == expected
    assert ("sent_at" in item) == (expected == "sent")
    if case == "guarded":
        owner.context.send_message.assert_not_awaited()
    assert state["window_end"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,suffix,raw",
    [
        ("text", ".txt", b"short"),
        ("image", ".png", b"\x89PNG\r\n\x1a\nimage"),
        ("code", ".py", b"print('example')"),
    ],
)
async def test_snapshot_digest_protects_every_file_output(
    transport, tmp_path, kind, suffix, raw
):
    owner, _, state = transport
    snapshot_path = tmp_path / ("snapshot" + suffix)
    snapshot_path.write_bytes(raw)
    item = {
        "work_id": "original",
        "snapshot": {
            "kind": kind,
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    assert (await outbox.output_chain(owner, state, item)).chain
    snapshot_path.write_bytes(raw + b"!changed")
    with pytest.raises(ValueError, match="校验失败"):
        await outbox.output_chain(owner, state, item)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["chat", "busy", "quiet", "guarded", "host_quiet"])
async def test_morning_report_never_marks_a_filtered_or_deferred_report_sent(
    transport, blocker, monkeypatch
):
    owner, _, state = transport
    state["outbox"] = [{"title": "pending work", "status": "pending"}]
    if blocker == "chat":
        state["last_inbound"] = time.time()
    elif blocker == "busy":
        owner.busy[UMO] = time.time() + 600
    elif blocker == "quiet":
        owner.quiet_hour_now = lambda _: True
    elif blocker == "guarded":
        owner._guard_user_state_claims = AsyncMock(return_value="")
    else:

        async def defer(adapter):
            adapter.quiet = True

        monkeypatch.setattr(main.CompanionAdapter, "begin", defer)
    await owner.morning_report(UMO)
    owner.context.send_message.assert_not_awaited()
    assert owner.running == owner.reporting == {}
    if blocker == "guarded":
        assert state["morning_report"]["status"] == "suppressed"
        assert not state["morning_report"]["text"]
    else:
        assert "morning_report" not in state
        owner.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_report_holds_target_reservation_until_finished(transport):
    owner, _, state = transport
    state["outbox"] = [{"title": "pending work", "status": "pending"}]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(**_):
        entered.set()
        await release.wait()
        return SimpleNamespace(completion_text="Saved a draft.")

    owner.context.llm_generate.side_effect = generate
    task = asyncio.create_task(owner.morning_report(UMO))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        assert owner.running[UMO] is owner.reporting[UMO] is task
        await owner.morning_report(UMO)
        owner.prepare.assert_awaited_once()
    finally:
        release.set()
        await task
    assert state["morning_report"]["status"] == "sent"
    assert owner.running == owner.reporting == {}
