"""Recover configured template MCP discovery after its service starts late."""

import asyncio
import time


async def recover_template_tools(owner, state):
    """Reconnect only an enabled, explicitly selected MCP server when tools are absent.

    Args:
        owner: Plugin runtime using AstrBot's shared tool manager.
        state: Current target state with latest service observation.

    Returns:
        A readable recovery outcome; never submits an image generation request.
    """
    manager = owner.context.get_llm_tool_manager()
    required = {"list_templates", "get_template", "run_template"}
    if required.issubset(
        {tool.name for tool in manager.get_full_tool_set().tools if tool.active}
    ):
        return "模板工具已加载"
    name = str(owner.config.get("template_mcp_server", "ComfyUI生图")).strip()
    if not name:
        return "未指定模板 MCP，未自动重连"
    if state.get("comfyui_observation", {}).get("status") != "ready":
        return "模板工具缺失，尚无本轮 ComfyUI 就绪结果，未重连"
    if not hasattr(owner, "template_recovery_lock"):
        owner.template_recovery_lock = asyncio.Lock()
        owner.template_recovery_at = 0
    async with owner.template_recovery_lock:
        if required.issubset(
            {tool.name for tool in manager.get_full_tool_set().tools if tool.active}
        ):
            return "模板工具已加载"
        if time.time() - owner.template_recovery_at < 60:
            return "模板 MCP 重连冷却中"
        owner.template_recovery_at = time.time()
        servers = manager.load_mcp_config().get("mcpServers", {})
        config = servers.get(name)
        if not isinstance(config, dict) or not config.get("active", True):
            return "指定模板 MCP 未配置或已停用，不自动启用"
        try:
            # An active runtime without required tools can be stale after restart.
            if name in manager.mcp_client_dict:
                await manager.disable_mcp_server(name, timeout=5)
            await manager.enable_mcp_server(name, config, timeout=15)
            found = {
                tool.name for tool in manager.get_full_tool_set().tools if tool.active
            }
            missing = sorted(required - found)
            return (
                "模板 MCP 已重连，工具齐全"
                if not missing
                else "MCP 已连接但未发现工具：" + ", ".join(missing)
            )
        except Exception as exc:
            owner.logger.warning("Template MCP recovery failed for %s: %s", name, exc)
            return (
                "模板 MCP 重连失败："
                + type(exc).__name__
                + "；保留任务，不把查询节点当作已学工作流"
            )
