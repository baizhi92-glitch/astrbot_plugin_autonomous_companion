"""第四批补做：不依赖 dsh 的本地文件工具，以及默认关闭的命令执行。"""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "data.plugins.astrbot_plugin_autonomous_companion.main",
    Path(__file__).parents[1] / "main.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

from data.plugins.astrbot_plugin_autonomous_companion import file_tools, guard  # noqa: E402


def make_workspace(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    return file_tools.Workspace(root, [root])


def test_read_inside_root(tmp_path):
    workspace = make_workspace(tmp_path)
    (workspace.write_root / "a.md").write_text("你好世界", encoding="utf-8")
    payload = file_tools.read_file(workspace, "a.md", 8000, 0)
    assert payload["ok"] and payload["content"] == "你好世界"
    assert payload["total_chars"] == 4


def test_read_refuses_outside_root(tmp_path):
    workspace = make_workspace(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("不该被读到", encoding="utf-8")
    payload = file_tools.read_file(workspace, str(outside), 8000, 0)
    assert payload["ok"] is False
    assert "不在允许读取的目录" in payload["error"]


def test_write_refuses_escape(tmp_path):
    workspace = make_workspace(tmp_path)
    payload = file_tools.write_file(
        workspace, tmp_path / "snaps", "../escape.txt", "x", "overwrite", 3, True
    )
    assert payload["ok"] is False
    assert "不在可写目录" in payload["error"]
    assert not (tmp_path / "escape.txt").exists()


def test_write_creates_file_and_snapshot(tmp_path):
    workspace = make_workspace(tmp_path)
    store = tmp_path / "snaps"
    first = file_tools.write_file(
        workspace, store, "sub/note.md", "第一版", "overwrite", 3, True
    )
    assert first["ok"] and first["mode"] == "overwrite"
    assert first["total_chars"] == 3
    second = file_tools.write_file(
        workspace, store, "sub/note.md", "追加", "append", 3, True
    )
    assert second["ok"] and second["mode"] == "append"
    assert (workspace.write_root / "sub" / "note.md").read_text(encoding="utf-8") == "第一版追加"
    assert guard.list_snapshots(store)


def test_list_and_search(tmp_path):
    workspace = make_workspace(tmp_path)
    (workspace.write_root / "第一章.md").write_text("开头", encoding="utf-8")
    (workspace.write_root / "第二章.md").write_text("里面有 关键词 啊", encoding="utf-8")
    (workspace.write_root / "note.txt").write_text("别的", encoding="utf-8")

    listing = file_tools.list_dir(workspace, ".", 1)
    assert listing["ok"] and any("第一章.md" in line for line in listing["entries"])

    found = file_tools.search_files(workspace, ".", "*.md", "", 20)
    assert found["count"] == 2
    matched = file_tools.search_files(workspace, ".", "*.md", "关键词", 20)
    assert matched["count"] == 1
    assert "关键词" in matched["results"][0]["hit"]


def test_command_blocked_by_red_lines(tmp_path):
    workspace = make_workspace(tmp_path)
    payload = asyncio.run(
        file_tools.run_command(
            "rm -rf /", workspace.write_root, 10, tmp_path / "snaps", 3, False
        )
    )
    assert payload["ok"] is False
    assert "不可逆" in payload["error"] or "拒绝" in payload["error"]


def test_command_runs_when_allowed(tmp_path):
    workspace = make_workspace(tmp_path)
    payload = asyncio.run(
        file_tools.run_command(
            "echo hello-local", workspace.write_root, 30, tmp_path / "snaps", 3, True
        )
    )
    assert payload["ok"] is True, payload
    assert "hello-local" in payload["stdout"]
    assert payload["backup"]


def make_plugin(tmp_path, **config):
    plugin = object.__new__(module.AutonomousCompanion)
    plugin.config = {
        "file_tools_enabled": True,
        "file_tools_root": str(tmp_path / "files"),
        "allow_shell_commands": False,
        "shell_command_timeout": 30,
        "file_tools_snapshot_before_write": True,
        "dsh_snapshot_keep": 3,
        "work_library_roots": [],
    }
    plugin.config.update(config)
    plugin.logger = logging.getLogger("test-files")
    plugin.dsh = SimpleNamespace(
        default_working_dir=lambda: tmp_path / "files",
        _plugin_data_dir=lambda: tmp_path / "data",
    )

    def number(key, default, low, high):
        try:
            return max(low, min(high, int(plugin.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    plugin.number = number
    (tmp_path / "files").mkdir(exist_ok=True)
    return plugin


@pytest.mark.asyncio
async def test_command_tool_is_off_by_default(tmp_path):
    plugin = make_plugin(tmp_path)
    raw = await module.AutonomousCompanion.autonomous_run_command(
        plugin, None, "echo 不该执行"
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "allow_shell_commands" in payload["error"]


@pytest.mark.asyncio
async def test_command_tool_runs_when_switched_on(tmp_path):
    plugin = make_plugin(tmp_path, allow_shell_commands=True)
    raw = await module.AutonomousCompanion.autonomous_run_command(
        plugin, None, "echo 打开了"
    )
    payload = json.loads(raw)
    assert payload["ok"] is True, payload
    assert "打开了" in payload["stdout"]


@pytest.mark.asyncio
async def test_write_tool_roundtrip_through_plugin(tmp_path):
    plugin = make_plugin(tmp_path)
    raw = await module.AutonomousCompanion.autonomous_write_file(
        plugin, None, "草稿.md", "她写的字"
    )
    assert json.loads(raw)["ok"] is True
    raw = await module.AutonomousCompanion.autonomous_read_file(plugin, None, "草稿.md")
    assert json.loads(raw)["content"] == "她写的字"


@pytest.mark.asyncio
async def test_file_tools_switch_off(tmp_path):
    plugin = make_plugin(tmp_path, file_tools_enabled=False)
    raw = await module.AutonomousCompanion.autonomous_list_files(plugin, None, ".")
    assert json.loads(raw)["ok"] is False


def test_new_tools_are_registered():
    from astrbot.core.star.star_handler import star_handlers_registry

    names = {
        handler.handler_name
        for handler in star_handlers_registry
        if handler.handler_name.startswith("autonomous_")
    }
    for name in (
        "autonomous_read_file",
        "autonomous_write_file",
        "autonomous_list_files",
        "autonomous_search_files",
        "autonomous_run_command",
    ):
        assert name in names, name


def test_default_install_hides_hazardous_tools(tmp_path):
    """默认配置下：命令执行与 dsh 通道都不在工具清单里，文件工具在。"""
    plugin = make_plugin(tmp_path)
    plugin.config["dsh_enabled"] = False
    hidden = module.AutonomousCompanion.hidden_tool_names(plugin)
    assert "autonomous_run_command" in hidden
    assert "autonomous_dsh_task" in hidden
    assert "autonomous_dsh_cli" in hidden
    assert "autonomous_read_file" not in hidden
    assert "autonomous_write_file" not in hidden


def test_switching_everything_on_exposes_all(tmp_path):
    plugin = make_plugin(
        tmp_path,
        allow_shell_commands=True,
        dsh_enabled=True,
        file_tools_enabled=True,
    )
    assert module.AutonomousCompanion.hidden_tool_names(plugin) == set()


def test_schema_defaults_are_conservative():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8-sig")
    )
    assert schema["allow_shell_commands"]["default"] is False
    assert schema["dsh_enabled"]["default"] is False
    assert schema["file_tools_enabled"]["default"] is True
