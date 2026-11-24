"""Project lifecycle, queue isolation, and working-slot regression tests."""

import copy
import json

import pytest

from data.plugins.astrbot_plugin_autonomous_companion.checkpoint import next_checkpoint
from data.plugins.astrbot_plugin_autonomous_companion.planning import (
    apply_plan,
    blocked_project_task,
    control_project,
    resolve_project,
)


def test_completed_projects_recycle_slots_and_retain_evidence():
    state = {
        "projects": [
            {"id": "legacy", "title": "Legacy project", "status": "completed"},
            {
                "id": "report",
                "title": "Workshop report",
                "status": "active",
                "next_step": "Write conclusion",
                "progress": "First draft",
                "artifact": "workshop-report.md",
            },
        ]
    }
    apply_plan(
        state,
        {
            "project_updates": [
                # Validate capacity after the whole batch, regardless of update order.
                {"title": "New presentation"},
                {
                    "id": "report",
                    "status": "completed",
                    "next_step": "",
                    "progress": "Final draft",
                },
            ]
        },
        max_projects=1,
    )
    state = json.loads(json.dumps(state))
    report = resolve_project(state, "", "report")
    assert report["status"] == "completed"
    assert report["next_step"] == ""
    assert report["progress"] == "Final draft"
    assert report["artifact"] == "workshop-report.md"
    assert report["history"][-1]["progress"] == "First draft"
    assert blocked_project_task(state, "Continue the workshop report", "")
    assert blocked_project_task(state, "opaque reminder", "report")
    assert not blocked_project_task(state, "Prepare the new presentation")

    snapshot = copy.deepcopy(state)
    apply_plan(
        state,
        {
            "future_task": "Rewrite the Workshop report",
            "project_updates": [
                {"id": "report", "status": "active", "progress": "stale"},
                {"title": "  WORKSHOP REPORT  ", "progress": "duplicate"},
            ],
        },
        max_projects=1,
    )
    assert state == snapshot
    apply_plan(state, {"future_task": "Write another chapter", "project_id": "report"})
    assert state == snapshot
    with pytest.raises(ValueError, match="最多保留1"):
        control_project(state, "report", "resume", max_projects=1)
    assert state == snapshot


def test_pause_preserves_checkpoint_and_only_clears_associated_work():
    state = {
        "projects": [
            {
                "id": "story",
                "title": "Station story",
                "status": "active",
                "progress": "Chapter one",
            },
            {"id": "other", "title": "Other project", "status": "active"},
        ],
        "manual_plan": "Write a new conclusion",
        "manual_project_id": "story",
        "manual_plan_expires_at": 500,
        "future_task": "Prepare unrelated presentation",
        "next_task": "Write a new conclusion",
        "window_end": 500,
        "window_admitted": True,
        "execution_checkpoint": {
            "task": "Station story editing",
            "pending": True,
            "cid": "test",
        },
        "execution_checkpoints": {
            "bound": {
                "task": "Revise dialogue",
                "project_id": "story",
                "pending": True,
                "cid": "test",
            },
            "other": {
                "task": "Read feedback on Station story",
                "project_id": "other",
                "pending": True,
                "cid": "test",
            },
        },
    }
    paused = control_project(state, "story", "pause")
    assert paused["status"] == "paused"
    assert paused["progress"] == "Chapter one"
    assert not state["manual_plan"] and not state["manual_project_id"]
    assert state["manual_plan_expires_at"] == 0
    assert state["next_task"] == ""
    assert not state["window_end"] and not state["window_admitted"]
    assert state["future_task"] == "Prepare unrelated presentation"
    assert state["execution_checkpoint"]["project_id"] == "story"
    assert state["execution_checkpoints"]["bound"]["pending"]
    assert next_checkpoint(state, "test")["project_id"] == "other"
    assert blocked_project_task(state, "Continue Station story")
    assert not blocked_project_task(state, "Station story", "other")

    state = json.loads(json.dumps(state))
    with pytest.raises(ValueError, match="最多保留2"):
        apply_plan(
            state, {"project_updates": [{"title": "Third project"}]}, max_projects=2
        )
    resumed = control_project(state, "story", "resume", max_projects=2)
    assert resumed["status"] == "active"
    assert next_checkpoint(state, "test")["project_id"] == "story"


def test_cancel_releases_slot_without_reactivating_terminal_checkpoint():
    state = {
        "projects": [
            {
                "id": "build",
                "title": "Model airplane",
                "status": "active",
                "progress": "Wing drafted",
            }
        ],
        "execution_checkpoints": {
            "job": {"project_id": "build", "pending": True, "cid": "test"}
        },
    }
    cancelled = control_project(state, "build", "cancel")
    assert cancelled["status"] == "cancelled"
    assert cancelled["progress"] == "Wing drafted"
    assert not state["execution_checkpoints"]["job"]["pending"]
    assert next_checkpoint(state, "test") is None
    apply_plan(state, {"project_updates": [{"title": "New project"}]}, max_projects=1)
    assert blocked_project_task(state, "Continue model airplane")
    assert len(state["projects"]) == 2
    control_project(state, "build", "resume", max_projects=2)
    assert not blocked_project_task(state, "Continue model airplane")
    assert next_checkpoint(state, "test") is None


def test_delete_keeps_id_and_title_tombstones_until_explicit_recreation():
    state = {
        "projects": [
            {
                "id": "old",
                "title": "Moon map",
                "goal": "Draw lunar craters",
                "status": "completed",
            }
        ],
        "execution_checkpoint": {"task": "Draw lunar craters", "pending": True},
        "execution_checkpoints": {
            "old": {"project_id": "old", "task": "Scan map", "pending": True},
            "other": {
                "project_id": "other",
                "task": "Moon map research",
                "pending": True,
            },
        },
        "next_task": "Scan map",
        "future_task": "Write a letter",
    }
    control_project(state, "old", "delete")
    state = json.loads(json.dumps(state))
    assert not state["projects"]
    assert not state["next_task"]
    assert state["future_task"] == "Write a letter"
    assert "execution_checkpoint" not in state
    assert list(state["execution_checkpoints"]) == ["other"]
    assert blocked_project_task(state, "Draw lunar craters")
    assert blocked_project_task(state, "Unknown wording", "old")
    apply_plan(
        state,
        {
            "project_updates": [
                {"id": "old", "status": "active"},
                {"title": " MOON MAP "},
            ]
        },
    )
    assert not state["projects"]
    apply_plan(state, {"project_updates": [{"title": "Moon Map"}]}, explicit_edit=True)
    assert len(state["projects"]) == 1
    assert state["projects"][0]["id"] != "old"
    assert not blocked_project_task(state, "Continue the moon map")
    assert blocked_project_task(state, "Continue the moon map", "old")


def test_completion_guard_applies_to_whitespace_padded_status():
    state = {"projects": [{"id": "one", "title": "Project one", "status": "active"}]}
    apply_plan(
        state,
        {"project_updates": [{"id": "one", "status": " completed "}]},
        allow_completion=False,
    )
    assert state["projects"][0]["status"] == "active"


def test_invalid_plan_does_not_retire_a_project_or_its_pending_work():
    state = {
        "projects": [{"id": "one", "title": "Project one", "status": "active"}],
        "future_task": "Continue Project one",
        "manual_project_id": "one",
        "execution_checkpoints": {"one": {"project_id": "one", "pending": True}},
    }
    snapshot = copy.deepcopy(state)
    with pytest.raises(ValueError, match="Invalid note"):
        apply_plan(
            state,
            {
                "project_updates": [{"id": "one", "status": "completed"}],
                "note_updates": [{}],
            },
        )
    assert state == snapshot
