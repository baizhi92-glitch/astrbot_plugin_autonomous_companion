"""Read-only runtime checks without model calls or outgoing messages."""

import asyncio
import time

from astrbot.core.platform.platform import PlatformStatus


async def inspect_runtime(owner):
    """Inspect configured routes and their live session dependencies.

    Args:
        owner: Active autonomous companion plugin.

    Returns:
        Timestamped per-target checks with explicit unknown states.
    """
    rows = []
    targets = owner.target_origins()
    for raw in owner.config.get("targets", []):
        raw = str(raw).strip()
        matches = [umo for umo in targets if umo == raw or umo.split(":", 2)[-1] == raw]
        if not matches:
            rows.append(
                {
                    "target": raw,
                    "checks": [
                        {
                            "name": "平台映射",
                            "status": "error",
                            "detail": "无法识别平台，请在设置选择 QQ 平台实例",
                        }
                    ],
                }
            )
    for umo in targets:
        checks = []
        platform = owner.context.get_platform_inst(umo.split(":", 1)[0])
        checks.append(
            {
                "name": "平台",
                "status": "ok"
                if platform and platform.status == PlatformStatus.RUNNING
                else "error",
                "detail": "运行中"
                if platform and platform.status == PlatformStatus.RUNNING
                else "实例不存在或未运行",
            }
        )
        try:
            manager = owner.context.conversation_manager
            cid = await asyncio.wait_for(manager.get_curr_conversation_id(umo), 5)
            conv = (
                await asyncio.wait_for(manager.get_conversation(umo, cid), 5)
                if cid
                else None
            )
            checks.append(
                {
                    "name": "会话",
                    "status": "ok" if conv else "error",
                    "detail": "已有当前会话" if conv else "请先与 bot 聊天",
                }
            )
            if conv and platform:
                persona_id, persona, _, _ = await asyncio.wait_for(
                    owner.context.persona_manager.resolve_selected_persona(
                        umo=umo,
                        conversation_persona_id=conv.persona_id,
                        platform_name=platform.meta().name,
                    ),
                    5,
                )
                checks.append(
                    {
                        "name": "人格",
                        "status": "ok"
                        if persona or persona_id in {None, "default", "[%None]"}
                        else "error",
                        "detail": str(persona_id or "默认人格"),
                    }
                )
        except Exception as exc:
            checks.append(
                {"name": "会话与人格", "status": "error", "detail": type(exc).__name__}
            )
        try:
            provider = await asyncio.wait_for(
                owner.context.get_current_chat_provider_id(umo), 5
            )
            checks.append(
                {
                    "name": "模型",
                    "status": "ok",
                    "detail": str(provider) + "（已配置，尚未调用）",
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "模型", "status": "error", "detail": type(exc).__name__}
            )
        state = owner.states.get(umo, {})
        for name, key in (
            ("记忆读取", "memory_status"),
            ("陪伴适配", "companion_status"),
        ):
            checks.append(
                {
                    "name": name,
                    "status": "waiting",
                    "detail": state.get(key, "尚未执行，首次主动或预览后可确认"),
                }
            )
        schedule_detail = (
            "正在行动"
            if umo in owner.running
            else "下次判断时间戳：" + str(state.get("next_due", "等待初始化"))
        )
        if state.get("next_speak_at", 0):
            schedule_detail += "；自己约定开口时间戳：" + str(state["next_speak_at"])
        checks.append(
            {
                "name": "调度",
                "status": "ok" if owner.config.get("enabled", True) else "waiting",
                "detail": schedule_detail,
            }
        )
        rows.append({"target": umo, "checks": checks})
    return {
        "checked_at": time.time(),
        "targets": rows,
        "note": "只读自检：未调用模型、未发送消息；记忆及陪伴状态为上次实际读取结果。",
    }
