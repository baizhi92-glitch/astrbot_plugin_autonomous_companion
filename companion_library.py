"""Read-only synchronization of the optional Companion persona bookshelf."""

import hashlib

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession

from .library import text_content_hash


async def sync_companion_library(owner, umo):
    """Synchronize scoped work copies without changing the source bookshelf.

    Args:
        owner: Autonomous plugin runtime.
        umo: Exact private target session.

    Returns:
        Number of synchronized works.
    """
    state = owner.states.setdefault(umo, owner.new_state())
    if not owner.config.get("companion_integration", True):
        state["companion_library_status"] = "未启用陪伴适配"
        return 0
    for meta in owner.context.get_all_stars():
        if (
            not meta.activated
            or meta.root_dir_name != "astrbot_plugin_private_companion"
        ):
            continue
        api = getattr(meta.star_cls, "extension_api", None)
        reader = getattr(api, "export_text_work_library", None)
        if not callable(reader):
            state["companion_library_status"] = (
                "陪伴作品导出接口未加载，请重载我会永远陪着你"
            )
            return 0
        session = MessageSession.from_str(umo)
        platform = owner.context.get_platform_inst(session.platform_id)
        if platform is None:
            state["companion_library_status"] = "对应平台未加载，无法确定作品人格"
            return 0
        event = CronMessageEvent(
            context=owner.context, session=session, message="只读同步作品库"
        )
        event.platform_meta = platform.meta()
        event.platform = platform.meta()
        event.message_obj.self_id = (
            state.get("identity", {}).get("bot_id") or platform.client_self_id
        )
        event.message_obj.sender.user_id = (
            state.get("identity", {}).get("user_id") or session.session_id
        )
        selected = owner.context.get_config(umo).get("plugin_set", ["*"])
        event.plugins_name = None if "*" in selected else selected
        if event.plugins_name is not None and meta.name not in event.plugins_name:
            state["companion_library_status"] = "本会话未启用陪伴插件"
            return 0
        result = await reader(event)
        if not isinstance(result, dict) or result.get("available") is not True:
            state["companion_library_status"] = "陪伴作品库暂不可用"
            return 0
        works = []
        for work in result.get("works", []):
            if not isinstance(work, dict) or not isinstance(work.get("content"), str):
                continue
            identifier = hashlib.sha256(
                (umo + "\0companion\0" + str(work.get("id"))).encode()
            ).hexdigest()[:20]
            works.append(
                {
                    "id": identifier,
                    "kind": "text",
                    "title": work.get("title", "未命名作品"),
                    "inline_content": work["content"],
                    "content_hash": text_content_hash(work["content"], work.get("title", "")),
                    "producer": "private_companion",
                    "source_id": work.get("id"),
                    "status": work.get("status"),
                    "modified_at": work.get("updated_at", 0),
                    "shared": "unknown",
                }
            )
        state["work_library"] = [
            w
            for w in state.get("work_library", [])
            if w.get("producer") != "private_companion"
        ] + works
        state["companion_library_status"] = (
            f"已同步陪伴作品 {len(works)} 份（含有正文的草稿与已完成作品）"
        )
        return len(works)
    state["companion_library_status"] = "独立模式，未连接陪伴作品库"
    return 0
