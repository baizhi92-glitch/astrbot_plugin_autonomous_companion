"""Authenticated dashboard endpoints for the embedded companion page."""

import asyncio
import json
import time
from pathlib import Path

from astrbot.api.web import request

from .cancellations import (
    cancelled_match,
    clear_cancelled_work,
    related,
    remember_cancellation,
)
from .companion_library import sync_companion_library
from .compiled_work import compile_chapters
from .conditions import validate_conditions
from .daily import pool_snapshot, usage_for
from .diagnostics import inspect_runtime, memory_sample, write_export
from .library import (
    archive_library_images,
    backfill_library,
    discover_text_works,
    download_library_file,
    read_library_image,
    read_library_text,
    refresh_text_fingerprints,
)
from .library_management import add_text_work, organize_work
from .long_tasks import control_task as control_long_task
from .long_tasks import create_task as create_long_task
from .outbox import enqueue_work, output_chain
from .notes import NOTE_KINDS, migrate_user_notes
from .activity import clear as clear_activity
from .planning import apply_plan, resolve_project


class CompanionPageAPI:
    def __init__(self, owner):
        self.owner = owner
        self.schema = json.loads(
            (Path(__file__).parent / "_conf_schema.json").read_text(encoding="utf-8")
        )

    def register(self):
        for path, handler, method in (
            ("/snapshot", self.snapshot, "GET"),
            ("/config", self.config, "POST"),
            ("/action", self.action, "POST"),
            ("/diagnostics", self.diagnostics, "GET"),
        ):
            for plugin_name in (
                "AutonomousCompanion",
                "astrbot_plugin_autonomous_companion",
            ):
                self.owner.context.register_web_api(
                    f"/{plugin_name}/page" + path,
                    handler,
                    [method],
                    "Autonomous companion dashboard",
                )

    async def snapshot(self):
        owner = self.owner
        for umo, task in list(owner.running.items()):
            if isinstance(task, asyncio.Future) and task.done():
                owner.running.pop(umo, None)
        targets = owner.target_origins()
        if not hasattr(self, "library_checked"):
            self.library_checked = {}
        for umo in targets:
            if time.time() - self.library_checked.get(umo, 0) >= 60:
                self.library_checked[umo] = time.time()
                if not hasattr(owner, "library_jobs"):
                    owner.library_jobs = {}
                if umo not in owner.library_jobs or owner.library_jobs[umo].done():
                    owner.library_jobs[umo] = asyncio.create_task(
                        self.refresh_library(umo)
                    )
        for state in owner.states.values():
            usage_for(state)
            backfill_library(state)
        return {
            "status": "ok",
            "data": {
                "ui_revision": "ui-3.0",
                "enabled": owner.config.get("enabled", True),
                "config": {
                    key: owner.config.get(key, spec.get("default"))
                    for key, spec in self.schema.items()
                },
                "schema": self.schema,
                "targets": targets,
                "sessions": [
                    {
                        "umo": umo,
                        "active": umo in targets,
                        "running": umo in owner.running,
                        # 当前用的是白天池还是夜间池、各用掉多少，供页面显示。
                        "pool": pool_snapshot(owner, state),
                        "state": {
                            **state,
                            "work_library": [
                                {k: v for k, v in work.items() if k != "inline_content"}
                                for work in state.get("work_library", [])
                            ],
                        },
                    }
                    for umo, state in owner.states.items()
                ],
                "platforms": [
                    {"id": p.meta().id, "name": p.meta().name}
                    for p in owner.context.platform_manager.platform_insts
                ],
            },
        }

    async def diagnostics(self):
        return {"status": "ok", "data": await inspect_runtime(self.owner)}

    async def refresh_library(self, umo):
        try:
            await asyncio.wait_for(discover_text_works(self.owner, umo), 8)
            await asyncio.wait_for(sync_companion_library(self.owner, umo), 20)
            await asyncio.to_thread(refresh_text_fingerprints, self.owner.states[umo])
            await archive_library_images(
                self.owner.states[umo],
                str(self.owner.config.get("comfyui_url", "http://127.0.0.1:8188")),
            )
            await self.owner.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.owner.states.get(umo, {})["companion_library_status"] = (
                "作品同步暂不可用：" + type(exc).__name__
            )
            self.owner.logger.warning("Library refresh failed for %s: %s", umo, exc)

    async def config(self):
        body = await request.json()
        try:
            return await self.update_config(body)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception:
            self.owner.logger.exception("Dashboard configuration save failed")
            return {
                "status": "error",
                "message": "配置保存失败，请查看后台日志；内存配置已回退",
            }

    async def update_config(self, body):
        """Validate the complete patch before persisting dashboard configuration.

        Args:
            body: Partial configuration object.

        Returns:
            API acknowledgement.

        Raises:
            ValueError: A value is unknown or has an invalid type or size.
        """
        if not isinstance(body, dict):
            raise ValueError("配置必须是对象")
        for key, value in body.items():
            spec = self.schema.get(key)
            if spec is None:
                raise ValueError("未知配置项：" + key)
            kind = spec["type"]
            valid = (
                (kind == "bool" and type(value) is bool)
                or (kind == "int" and type(value) is int and 0 <= value <= 100000)
                or (
                    kind in {"string", "text"}
                    and isinstance(value, str)
                    and len(value) <= 10000
                )
                or (
                    kind == "list"
                    and isinstance(value, list)
                    and len(value) <= 100
                    and all(isinstance(x, str) and len(x) <= 300 for x in value)
                )
            )
            if not valid:
                raise ValueError("配置格式不正确：" + key)
        old = dict(self.owner.config)
        merged = {**old, **body}
        minimum = merged.get("frequency_min_minutes", 10)
        maximum = merged.get("frequency_max_minutes", 180)
        if minimum < 1 or maximum < minimum or maximum > 1440:
            raise ValueError("自主频率范围必须满足 1 ≤ 最短 ≤ 最长 ≤ 1440 分钟")
        speak_min = merged.get("next_speak_min_minutes", 5)
        speak_max = merged.get("next_speak_max_minutes", 1440)
        if speak_min < 1 or speak_max < speak_min or speak_max > 1440:
            raise ValueError("开口约定范围必须满足 1 ≤ 最短 ≤ 最长 ≤ 1440 分钟")
        bounds = {
            "interval_minutes": (1, 1440),
            "jitter_minutes": (0, 120),
            "continuation_window_minutes": (1, 120),
            "max_rounds": (1, 30),
            "max_tool_calls": (1, 100),
            "max_agent_steps": (1, 30),
            "turn_timeout_seconds": (30, 1800),
            "tool_timeout_seconds": (5, 600),
            "recent_chat_grace_seconds": (0, 3600),
            "history_messages": (2, 200),
            "history_chars": (1000, 100000),
            "memory_timeout_seconds": (1, 120),
            "next_speak_min_minutes": (1, 1440),
            "next_speak_max_minutes": (1, 1440),
            "speak_wish_ttl_hours": (1, 168),
            "preference_stale_days": (1, 3650),
            "activity_half_life_days": (7, 365),
        }
        for key, (low, high) in bounds.items():
            if key in body and not low <= body[key] <= high:
                raise ValueError(f"{key} 必须在 {low}～{high} 之间")
        self.owner.config.update(body)
        try:
            self.owner.config.save_config()
        except Exception:
            self.owner.config.clear()
            self.owner.config.update(old)
            raise
        targets = self.owner.target_origins()
        for umo, task in list(self.owner.running.items()):
            if not self.owner.config.get("enabled", True) or umo not in targets:
                task.cancel()
        for umo, task in list(self.owner.chat_tasks.items()):
            if not self.owner.config.get("enabled", True) or umo not in targets:
                task.cancel()
        return {"status": "ok", "data": {"saved": True}}

    async def action(self):
        body = await request.json()
        try:
            return await self.perform(body)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

        except Exception as exc:
            self.owner.logger.exception("Dashboard action failed")
            return {
                "status": "error",
                "message": f"操作未完成：{type(exc).__name__}：{str(exc)[:250]}",
            }

    async def perform(self, body):
        """Apply one bounded per-session dashboard operation.

        Args:
            body: Origin, operation and optional edited fields.

        Returns:
            API acknowledgement.

        Raises:
            ValueError: The target, action or edit is invalid.
        """
        if not isinstance(body, dict):
            raise ValueError("请求格式错误")
        umo, action = body.get("umo"), body.get("action")
        owner = self.owner
        if action == "export_diagnostics":
            # 一键导出脱敏诊断包，出问题直接把文件发出来；不指定对象就导出全部。
            target = umo if umo in owner.target_origins() else None
            extra = {}
            try:
                checks = await asyncio.wait_for(inspect_runtime(owner), 20)
                extra["自检"] = {
                    "targets": checks.get("targets", []),
                    "note": checks.get("note", ""),
                }
            except Exception as exc:
                extra["自检"] = {"targets": [], "note": "自检失败：" + type(exc).__name__}
            if target:
                try:
                    extra["记忆采样"] = await memory_sample(owner, target)
                except Exception as exc:
                    extra["记忆采样"] = {"结论": "采样失败：" + type(exc).__name__}
            result = await asyncio.to_thread(write_export, owner, target, extra)
            return {"status": "ok", "data": result}
        for origin, task in list(owner.running.items()):
            if isinstance(task, asyncio.Future) and task.done():
                owner.running.pop(origin, None)
        if not isinstance(umo, str) or umo not in owner.target_origins():
            raise ValueError("请先在设置中配置该对象")
        state = owner.states.setdefault(umo, owner.new_state())
        migrate_user_notes(state)
        if action == "long_task":
            operation = body.get("operation")
            if operation == "create":
                job = create_long_task(state, body.get("title"), body.get("goal"))
            else:
                job = control_long_task(state, body.get("id"), operation)
            if operation in {"pause", "cancel"}:
                running = owner.running.get(umo)
                if isinstance(running, asyncio.Task):
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            if operation in {"create", "resume"}:
                state["next_due"] = min(
                    state.get("next_due", time.time()), time.time() + 5
                )
            await owner.save()
            return {"status": "ok", "data": {"task": job}}
        if action == "companion_sync":
            count = await asyncio.wait_for(sync_companion_library(owner, umo), 30)
            await owner.save()
            return {
                "status": "ok",
                "data": {
                    "synced": count,
                    "message": state.get("companion_library_status", ""),
                },
            }
        if action == "work_rescan":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后补录")
            count = await discover_text_works(owner, umo)
            count += await asyncio.wait_for(sync_companion_library(owner, umo), 20)
            await owner.save()
            return {"status": "ok", "data": {"indexed": count}}
        if action in {"cancel_plan", "project_delete"} or (
            action == "plan" and body.get("value") == ""
        ):
            owner.revisions[umo] = owner.revisions.get(umo, 0) + 1
            tasks = [
                task
                for task in (owner.running.get(umo), owner.chat_tasks.get(umo))
                if isinstance(task, asyncio.Task)
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            owner.running.pop(umo, None)
            owner.chat_tasks.pop(umo, None)
        if action == "cancel_plan":
            remember_cancellation(
                state,
                [
                    state.get("manual_plan", ""),
                    state.get("future_task", ""),
                    state.get("execution_checkpoint", {}).get("task", ""),
                ],
            )
            state.update(
                manual_plan="",
                future_task="",
                next_task="",
                window_end=0,
                window_admitted=False,
                plan_status="临时任务已取消，不再自动恢复",
                continuation_status="用户取消临时任务",
                next_due=time.time() + owner.interval(state),
            )
            state.pop("execution_checkpoint", None)
            for checkpoint in state.get("execution_checkpoints", {}).values():
                if cancelled_match(state, checkpoint.get("task", "")):
                    checkpoint["pending"] = False
            state["manual_plan_expires_at"] = 0
            state["plan_cancelled_at"] = time.time()
            await owner.save()
            return {"status": "ok", "data": {"saved": True}}
        if action in {
            "work_archive",
            "work_restore",
            "work_add_text",
            "work_archive_chapters",
            "work_finalize_draft",
        }:
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后管理作品")
            if action == "work_add_text":
                add_text_work(state, body.get("title"), body.get("content"))
            elif action == "work_finalize_draft":
                work = next(
                    (
                        w
                        for w in state.get("work_library", [])
                        if w["id"] == body.get("id")
                    ),
                    None,
                )
                if not work:
                    raise ValueError("未找到该作品")
                if work.get("producer") == "private_companion":
                    raise ValueError("陪伴作品库条目只读，不可直接修改；请调用 companion API")
                if not work.get("path"):
                    raise ValueError("该草稿无本地文件，无法定稿")
                # Mark as finished
                state["work_library"] = [
                    w if w["id"] != work["id"] else {**w, "status": "finished"}
                    for w in state["work_library"]
                ]
            elif action == "work_archive_chapters":
                work = next(
                    (
                        w
                        for w in state.get("work_library", [])
                        if w["id"] == body.get("id")
                    ),
                    None,
                )
                if not work or not work.get("chapter_ids"):
                    raise ValueError("该作品没有已核验的合订章节关联，请先选择章节合订")
                organize_work(state, work["chapter_ids"], True)
            elif action == "work_finalize_draft":
                # Already handled above; just save
                pass
            else:
                organize_work(state, body.get("ids"), action == "work_archive")
            await owner.save()
            return {"status": "ok", "data": {"saved": True}}
        if action == "work_compile":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后合并")
            receipt = await asyncio.to_thread(
                compile_chapters, state, umo, body.get("title"), body.get("ids")
            )
            await owner.save()
            return {"status": "ok", "data": receipt}
        if action == "work_archive_image":
            item = next(
                (
                    w
                    for w in state.get("work_library", [])
                    if w["id"] == body.get("id") and w.get("kind") == "image"
                ),
                None,
            )
            if item is None:
                raise ValueError("图片作品不存在")
            await archive_library_images(
                state,
                str(owner.config.get("comfyui_url", "http://127.0.0.1:8188")),
                {body.get("id")},
            )
            await owner.save()
            if not item.get("path"):
                raise ValueError(
                    "图片未保存：" + item.get("archive_error", "无法读取原图")
                )
            return {"status": "ok", "data": {"saved": True}}
        if action == "work_download":
            file = await asyncio.to_thread(download_library_file, state, body.get("id"))
            return {"status": "ok", "data": file}
        elif action == "work_delete":
            work_id = body.get("id")
            # Find and remove the work from library
            state["work_library"] = [
                w for w in state.get("work_library", []) if w["id"] != work_id
            ]
            self.owner.logger.info(f"Deleted work: {work_id}")
            await owner.save()
            return {"status": "ok", "data": {"deleted": True}}
        elif action == "work_image":
            image = await read_library_image(
                state,
                body.get("id"),
                str(owner.config.get("comfyui_url", "http://127.0.0.1:8188")),
            )
            return {"status": "ok", "data": {"image": image}}
        elif action == "work_read":
            text = await asyncio.to_thread(read_library_text, state, body.get("id"))
            return {"status": "ok", "data": {"content": text}}
        elif action == "work_to_outbox":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后加入成果箱")
            enqueue_work(state, body.get("id"))
        elif action in {"note_save", "preference_save", "impression_save"}:
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后修改关于你的记录")
            key = str(body.get("key") or "").strip()[:80]
            value = str(body.get("value") or "").strip()[:600]
            replace_key = str(body.get("replace_key") or "").strip()[:80]
            kind = body.get("kind")
            if action == "preference_save":
                kind = "preference"
            if kind in (None, ""):
                kind = "fact"
            if kind not in NOTE_KINDS:
                raise ValueError("类型只能是偏好、事实或爱好")
            if not key or not value:
                raise ValueError("主题和内容都不能为空")
            notes = state.get("notes")
            if not isinstance(notes, dict):
                notes = {}
                state["notes"] = notes
            old = notes.get(replace_key or key) or {}
            renaming = bool(replace_key) and replace_key != key and replace_key in notes
            # 偏好只来自用户明确表达，天然是已确认的
            confirmed = True if kind == "preference" else body.get(
                "confirmed", old.get("confirmed", False)
            )
            if type(confirmed) is not bool:
                raise ValueError("确认状态必须为布尔值")
            pinned = body.get("pinned", old.get("pinned", False))
            if type(pinned) is not bool:
                raise ValueError("固定状态必须为布尔值")
            entry = {
                "value": value,
                "kind": kind,
                "confirmed": confirmed,
                "evidence": str(body.get("evidence") or "").strip()[:300]
                or "用户手动添加",
                "created_at": old.get("created_at") or time.time(),
                "updated_at": time.time(),
                "pinned": pinned,
                "source": "manual",
            }
            if confirmed:
                entry["confirmed_at"] = time.time()
            previous = str(old.get("value") or "").strip()
            if previous and previous != value:
                entry["previous"] = previous[:600]
                entry["changed_at"] = time.time()
            notes[key] = entry
            if renaming:
                notes.pop(replace_key, None)
        elif action in {
            "note_confirm",
            "note_pin",
            "note_delete",
            "preference_pin",
            "preference_delete",
            "impression_confirm",
            "impression_delete",
        }:
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后修改关于你的记录")
            key = body.get("key")
            notes = state.get("notes")
            if not isinstance(notes, dict):
                notes = {}
                state["notes"] = notes
            if not isinstance(key, str) or key not in notes:
                raise ValueError("这条记录不存在，请刷新")
            if action.endswith("_delete"):
                notes.pop(key)
            elif action.endswith("_confirm"):
                confirmed = body.get("confirmed")
                if type(confirmed) is not bool:
                    raise ValueError("确认状态必须为布尔值")
                notes[key]["confirmed"] = confirmed
                if confirmed:
                    notes[key]["confirmed_at"] = time.time()
                else:
                    notes[key].pop("confirmed_at", None)
            else:
                pinned = body.get("pinned")
                if type(pinned) is not bool:
                    raise ValueError("固定状态必须为布尔值")
                notes[key]["pinned"] = pinned
        elif action == "pending_message_discard":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍候再丢弃")
            if not state.get("pending_message"):
                raise ValueError("没有待补发的后台消息")
            state.pop("pending_message", None)
        elif action == "rhythm_reset":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在行动或整理计划，请稍后重置")
            for key in ("rhythm_minutes", "rhythm_reason", "rhythm_updated_at"):
                state.pop(key, None)
            for key in ("rhythm_expires_at", "rhythm_scope", "rhythm_ignored_reason"):
                state.pop(key, None)
            if not state.get("next_task") and not state.get("events"):
                state["next_due"] = max(
                    time.time() + owner.interval(),
                    state.get("pause_until", 0)
                    if state.get("pause_mode") == "all"
                    else 0,
                )
        elif action == "activity_note":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后修改作息备注")
            entry = state.get("activity")
            if not isinstance(entry, dict):
                entry = {}
            entry["note"] = str(body.get("note") or "").strip()[:160]
            state["activity"] = entry
        elif action == "activity_reset":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后清空活跃统计")
            clear_activity(state)
        elif action == "appointment_status":
            status = body.get("status")
            if status not in {"resolved", "cancelled"}:
                raise ValueError("仅支持标记已了结或取消")
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍候")
            item = next(
                (
                    item
                    for item in state.get("appointments", [])
                    if item["id"] == body.get("id")
                ),
                None,
            )
            if item is None:
                raise ValueError("约定不存在")
            item.update(
                status=status,
                result=str(body.get("result", "用户在网页标记"))[:500],
                updated_at=time.time(),
            )
        elif action == "conditions":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在执行或整理，请稍后编辑")
            state["conditions"] = validate_conditions(body.get("value"))
            state["condition_observations"] = []
        elif action == "pause":
            minutes, mode = body.get("minutes"), body.get("mode")
            if (
                type(minutes) is not int
                or not 1 <= minutes <= 10080
                or mode not in {"all", "messages"}
            ):
                raise ValueError("暂停需为 1～10080 分钟，模式为 all 或 messages")
            state.update(pause_until=time.time() + minutes * 60, pause_mode=mode)
            if mode == "all":
                owner.revisions[umo] = owner.revisions.get(umo, 0) + 1
                if umo in owner.running:
                    owner.running[umo].cancel()
                state["next_due"] = state["pause_until"]
                slots = owner.speak_schedule(state)
                if slots:
                    for item in slots:
                        item["at"] = max(item["at"], state["pause_until"])
                    owner.store_speak_schedule(state, slots)
                state.update(next_task="", window_end=0, window_admitted=False)
        elif action == "resume":
            state.update(pause_until=0, pause_mode="", next_due=time.time() + 30)
        elif action == "outbox":
            item = next(
                (
                    item
                    for item in state.get("outbox", [])
                    if item["id"] == body.get("id")
                ),
                None,
            )
            if item is None:
                raise ValueError("成果不存在")
            operation = body.get("operation")
            if operation == "discard":
                if item["status"] == "sending":
                    raise ValueError("正在发送，请稍候")
                item["status"] = "discarded"
            elif operation == "retry":
                if item["status"] != "failed":
                    raise ValueError("只能重试明确未提交的失败；不确定发送不可直接重试")
                item.update(status="pending", error="")
            elif operation == "send":
                if item["status"] != "pending":
                    raise ValueError("此成果已处理或发送结果不确定，不能自动重复发送")
                if (
                    umo in owner.running
                    or not owner.config.get("enabled", True)
                    or state.get("pause_until", 0) > time.time()
                ):
                    raise ValueError("本对象正在行动、已关闭或处于暂停中")
                task = asyncio.create_task(self.send_output(umo, item))
                owner.running[umo] = task
                try:
                    await asyncio.wait_for(task, 60)
                finally:
                    owner.running.pop(umo, None)
            else:
                raise ValueError("成果操作不支持")
        elif action == "now":
            if state.get("pause_until", 0) > time.time():
                raise ValueError("请先恢复本对象")
            if not owner.config.get("enabled", True):
                raise ValueError("请先启用总开关")
            if umo in owner.running:
                raise ValueError("本对象正在行动，请稍后再试")
            state["last_inbound"] = 0
            state["next_due"] = 0
        elif action == "stop":
            owner.revisions[umo] = owner.revisions.get(umo, 0) + 1
            task = owner.running.get(umo)
            if task:
                task.cancel()
            state.update(
                next_task="",
                window_end=0,
                window_admitted=False,
                next_due=time.time() + owner.interval(state),
            )
        elif action == "plan":
            value = body.get("value")
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError("计划最多 2000 字符")
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在整理或执行，请稍后编辑")
            if body.get("project_id") and not resolve_project(
                state, value, body["project_id"]
            ):
                raise ValueError("项目不存在")
            project = resolve_project(state, value, body.get("project_id", ""))
            if project and project.get("status") != "active":
                raise ValueError("关联项目已暂停或结束，请先将项目恢复为进行中")
            if not value.strip():
                remember_cancellation(
                    state, [state.get("manual_plan", ""), state.get("future_task", "")]
                )
            state["future_task"] = value.strip()
            state["manual_plan"] = value.strip()
            state["manual_project_id"] = (
                project["id"] if project and value.strip() else ""
            )
            if value.strip():
                state["cancelled_tasks"] = [
                    item
                    for item in state.get("cancelled_tasks", [])
                    if not related(item["text"], value)
                ]
            state.pop("execution_checkpoint", None)
            state["plan_status"] = "等待下一次主动判断" if value.strip() else "已清空"
            state["plan_saved_at"] = time.time()
            state["manual_plan_expires_at"] = (
                time.time() + owner.number("manual_task_ttl_minutes", 60, 1, 1440) * 60
                if value.strip()
                else 0
            )
            if not value.strip():
                state.update(next_task="", window_end=0, window_admitted=False)
        elif action == "project_edit":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在整理或执行，请稍后编辑")
            update = {
                key: body[key]
                for key in ("id", "title", "goal", "progress", "next_step", "artifact")
                if key in body
            }
            if not update.get("id"):
                raise ValueError("缺少项目ID")
            apply_plan(state, {"project_updates": [update]}, explicit_edit=True)
        elif action == "project":
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在整理或执行，请稍后编辑")
            status = body.get("status")
            if status not in {"active", "paused", "completed", "cancelled"}:
                raise ValueError("项目状态错误")
            project = next(
                (p for p in state.get("projects", []) if p["id"] == body.get("id")),
                None,
            )
            if project is None:
                raise ValueError("项目不存在")
            project["status"] = status
            project["updated_at"] = time.time()
            if status != "active":
                if state.get("manual_project_id") == project["id"]:
                    state.update(
                        manual_plan="", manual_project_id="", manual_plan_expires_at=0
                    )
                # Stop queued work too, not just the project card's status.
                for checkpoint in [
                    state.get("execution_checkpoint", {}),
                    *state.get("execution_checkpoints", {}).values(),
                ]:
                    if checkpoint.get("project_id") == project["id"] or any(
                        related(checkpoint.get("task", ""), text)
                        for text in (project["title"], project.get("goal", ""))
                    ):
                        checkpoint["project_id"] = project["id"]
                for key in ("future_task", "next_task", "manual_plan"):
                    if any(
                        related(state.get(key, ""), text)
                        for text in (project["title"], project.get("goal", ""))
                    ):
                        state[key] = ""
                        if key == "manual_plan":
                            state["manual_plan_expires_at"] = 0
                            state["plan_status"] = "关联项目已暂停或结束"
                if not state.get("next_task"):
                    state["window_end"] = 0
        elif action in {"project_add", "project_delete"}:
            if umo in owner.running or umo in owner.chat_tasks:
                raise ValueError("正在整理或执行，请稍后编辑")
            if action == "project_delete":
                project = next(
                    (p for p in state.get("projects", []) if p["id"] == body.get("id")),
                    None,
                )
                if project is None:
                    raise ValueError("项目不存在或已删除，请刷新")
                remember_cancellation(
                    state, [project["title"], project.get("goal", "")]
                )
                state["deleted_projects"] = (
                    state.get("deleted_projects", [])
                    + [
                        {
                            "id": project["id"],
                            "title": project["title"],
                            "at": time.time(),
                        }
                    ]
                )[-50:]
                state["projects"] = [
                    p for p in state.get("projects", []) if p["id"] != body.get("id")
                ]
                if state.get("manual_project_id") == project["id"]:
                    state.update(
                        manual_plan="", manual_project_id="", manual_plan_expires_at=0
                    )
                state["execution_checkpoints"] = {
                    key: checkpoint
                    for key, checkpoint in state.get(
                        "execution_checkpoints", {}
                    ).items()
                    if checkpoint.get("project_id") != project["id"]
                }
                if (
                    state.get("execution_checkpoint", {}).get("project_id")
                    == project["id"]
                ):
                    state.pop("execution_checkpoint", None)
                if state.get("last_turn", {}).get("project_id") == project["id"]:
                    state.pop("execution_checkpoint", None)
                    state.update(next_task="", window_end=0, future_task="")
                clear_cancelled_work(state)
            else:
                if len(state.get("projects", [])) >= 5:
                    raise ValueError("最多保留 5 个项目，请先删除旧项目")
                title, goal = body.get("title"), body.get("goal", "")
                if (
                    not isinstance(title, str)
                    or not title.strip()
                    or not isinstance(goal, str)
                ):
                    raise ValueError("请填写项目名称和目标")
                # A manual add is explicit permission to recreate this project.
                title, goal = title.strip()[:1000], goal.strip()[:1000]
                state["deleted_projects"] = [
                    item
                    for item in state.get("deleted_projects", [])
                    if item.get("title") != title
                ]
                state["cancelled_tasks"] = [
                    item
                    for item in state.get("cancelled_tasks", [])
                    if not any(related(item["text"], text) for text in (title, goal))
                ]
                apply_plan(
                    state,
                    {"project_updates": [{"title": title[:1000], "goal": goal[:1000]}]},
                )
        elif action == "preview":
            if umo in owner.running:
                raise ValueError("正在执行，请稍后预览")
            task = asyncio.create_task(owner.turn(umo, preview=True))
            owner.running[umo] = task
            try:
                decision = await asyncio.wait_for(task, 120)
                return {"status": "ok", "data": {"decision": decision}}
            finally:
                owner.running.pop(umo, None)
        else:
            raise ValueError("不支持的操作")
        await owner.save()
        return {"status": "ok", "data": {"saved": True}}

    async def send_output(self, umo, item):
        """Dispatch a selected pending text once through normal delivery tracking.

        Args:
            umo: Target conversation.
            item: Pending outbox item; uncertain sends are never replayed.

        Raises:
            ValueError: Optional host coordination deferred delivery.
        """
        from .companion_adapter import CompanionAdapter

        owner = self.owner
        try:
            event, *_ = await owner.prepare(umo)
        except Exception as exc:
            item.update(status="failed", error=str(exc)[:300])
            await owner.save()
            raise
        event.state["window_end"] = 0
        adapter = CompanionAdapter(owner, event)
        event.companion_adapter = adapter
        try:
            await adapter.begin()
            if adapter.blocked or adapter.quiet:
                raise ValueError("陪伴插件当前暂缓发送")
            try:
                chain = await output_chain(owner, event.state, item)
            except Exception as exc:
                item.update(status="failed", error=str(exc)[:300])
                await owner.save()
                raise
            item["status"] = "sending"
            await owner.save()
            try:
                await owner.deliver(event, chain)
            except BaseException:
                item["status"] = "uncertain"
                await owner.save()
                raise
            item.update(status="sent", sent_at=time.time())
            await owner.save()
        finally:
            await adapter.finish()
