"""Optional integration with the live, public Private Companion facade."""

import asyncio
import inspect
import uuid


class CompanionAdapter:
    def __init__(self, owner, event):
        self.owner = owner
        self.event = event
        self.api = None
        self.token = ""
        self.activity_id = ""
        self.prompt = ""
        self.sent = []
        self.blocked = False
        self.quiet = False

    async def call(self, name, *args, **kwargs):
        """Call a public optional capability within a bounded async budget.

        Args:
            name: Public facade method.
            *args: Positional capability arguments.
            **kwargs: Keyword capability arguments.

        Returns:
            The capability result, or None when not exposed.
        """
        method = getattr(self.api, name, None)
        if not callable(method):
            return None
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, 10)
        return result

    async def begin(self, preview=False):
        state = self.event.state
        state["companion_status"] = "独立运行"
        if not self.owner.config.get("companion_integration", True):
            return
        try:
            for meta in self.owner.context.get_all_stars():
                if not meta.activated:
                    continue
                if getattr(meta.star_cls, "enabled", True) is False:
                    continue
                if (
                    meta.root_dir_name != "astrbot_plugin_private_companion"
                    and meta.name != "PrivateCompanion"
                ):
                    continue
                if (
                    self.event.plugins_name is not None
                    and meta.name not in self.event.plugins_name
                ):
                    continue
                candidate = getattr(meta.star_cls, "extension_api", None)
                self.api = candidate
                status = await self.call("bridge_lifecycle_status")
                if not isinstance(status, dict) or status.get("active") is not True:
                    self.api = None
                break
            if self.api is None:
                return
            state["companion_status"] = "已连接陪伴插件"
            if not preview:
                # The host's public bridge performs recipient checks and reserves sending.
                result = await self.call(
                    "prepare_proactive_chat", self.event.unified_msg_origin
                )
                if isinstance(result, dict) and result.get("enabled"):
                    if result.get("allowed") is False:
                        reason = str(result.get("reason", "busy"))
                        # Only known message-timing limits permit quiet background work.
                        self.quiet = reason in {
                            "user_explicit_rest",
                            "private_companion_quiet_hours",
                            "expression_proactive_budget_zero",
                            "expression_proactive_budget_exhausted",
                        }
                        self.blocked = not self.quiet
                        state["companion_status"] = (
                            "陪伴插件暂缓：" + str(result.get("reason", "busy"))[:160]
                        )
                        if self.blocked:
                            return
                    self.token = str(result.get("token") or "")
                    self.prompt = str(result.get("prompt_fragment") or "")[:6000]
            if not self.prompt:
                result = await self.call(
                    "get_realtime_context",
                    self.event.get_sender_id(),
                    purpose="proactive",
                )
                if isinstance(result, dict):
                    self.prompt = str(result.get("prompt") or "")[:6000]
            if not preview:
                self.activity_id = "autonomous-" + uuid.uuid4().hex
                await self.call(
                    "notify_external_activity_started",
                    self.activity_id,
                    user_id=self.event.get_sender_id(),
                    kind="autonomous_work",
                    label="正在推进自主计划",
                    source_plugin="astrbot_plugin_autonomous_companion",
                    ttl_seconds=self.owner.number("turn_timeout_seconds", 240, 30, 1800)
                    + 30,
                )
        except Exception as exc:
            state["companion_status"] = "陪伴适配降级，独立运行：" + type(exc).__name__
            self.owner.logger.warning("Optional companion integration failed: %s", exc)

    async def finish(self):
        """Settle actual deliveries and release only this adapter's reservations."""
        if self.api is None:
            return
        try:
            if self.sent:
                text = "\n".join(self.sent)[-2000:]
                if (
                    self.token
                    and self.owner.revisions.get(self.event.unified_msg_origin, 0)
                    == self.event.revision
                ):
                    await self.call(
                        "notify_proactive_chat_sent",
                        self.event.unified_msg_origin,
                        text,
                        token=self.token,
                    )
                await self.call(
                    "record_external_realtime_continuity",
                    self.event.get_sender_id(),
                    summary="自主行动已发送的内容：" + text,
                    public_summary="最近推进过自主计划",
                    ttl_seconds=21600,
                )
        except Exception as exc:
            self.owner.logger.warning(
                "Companion delivery synchronization failed: %s", exc
            )
        finally:
            for name, args, kwargs in (
                (
                    "cancel_proactive_chat",
                    (self.event.unified_msg_origin,),
                    {"token": self.token},
                ),
                ("notify_external_activity_ended", (self.activity_id,), {}),
            ):
                if (name == "cancel_proactive_chat" and not self.token) or (
                    name == "notify_external_activity_ended" and not self.activity_id
                ):
                    continue
                try:
                    await self.call(name, *args, **kwargs)
                except Exception as exc:
                    self.owner.logger.warning(
                        "Companion reservation cleanup failed: %s", exc
                    )
