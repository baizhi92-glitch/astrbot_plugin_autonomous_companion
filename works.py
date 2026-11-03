"""Explicit, verifiable text-work persistence for autonomous creation."""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

from astrbot.api.star import StarTools
from astrbot.core.agent.tool import FunctionTool

from .library import catalogue_receipt
from .lifecycle import record_evidence


class SaveWorkTool(FunctionTool):
    def __init__(self, event):
        super().__init__(
            name="autonomous_save_work",
            description="保存完整正文为真实文本文件；分轮创作时可用 mode=draft 创建/更新同一草稿（作品库显示“创作中”），完成后用 mode=final 定稿。drafting 状态的作品不算完成，不得当作成品汇报或投递。以返回的本地及陪伴作品库回执为准，不得虚构同步成功。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "作品标题"},
                    "content": {
                        "type": "string",
                        "description": "实际完整正文，不是内容摘要或保存承诺",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["draft", "final"],
                        "default": "final",
                        "description": "mode=draft 创建或更新草稿（多轮继续写）；mode=final 定稿并可选同步到我会永远陪着你",
                    },
                    "work_id": {
                        "type": "string",
                        "description": "要更新的现有草稿 id（mode=draft 时必需）；mode=final 时可传同一条草稿的 id 来定稿它",
                    },
                },
                "required": ["title", "content"],
            },
        )
        self.event = event

    async def call(self, context, title: str, content: str, mode="final", work_id=None):
        """Write and verify a uniquely named work scoped to this private origin.

        Args:
            context: AstrBot tool execution context.
            title: Display title retained in the work file.
            content: Complete text to persist.
            mode: "draft" creates/updates a draft (status=drafting); "final" writes a finished work.
            work_id: Existing draft id to update/finalize; required for mode=draft without new draft creation.

        Returns:
            JSON receipt including the verified file path, character count, status and work_id.

        Raises:
            ValueError: Content is empty or exceeds the text-work limit; or work_id references invalid work.
        """
        if not title.strip() or not content.strip() or len(content) > 200000:
            raise ValueError("作品标题及正文不能为空，正文最多 200000 字符")
        owner = self.event.owner
        await owner.check_current(self.event)
        root = Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
        scope = hashlib.sha256(self.event.unified_msg_origin.encode()).hexdigest()[:16]

        def resolve_draft_path():
            drafts_dir = root / "works" / scope / "drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)
            # Stable name so register_local_work updates same entry on re-save
            return drafts_dir / f"draft-{uuid.uuid4().hex[:8]}.md"

        if mode == "draft":
            # Find existing draft by work_id
            state = self.event.state
            target = None
            if work_id:
                work_item = next(
                    (w for w in state.get("work_library", []) if w["id"] == work_id),
                    None,
                )
                if work_item:
                    if work_item.get("producer") == "private_companion":
                        raise ValueError("陪伴作品库条目只读，不可直接修改；请调用 companion API")
                    if not work_item.get("path"):
                        raise ValueError("该草稿无本地文件，无法更新")
                    target = Path(work_item["path"])
                    # Overwrite existing draft
                    text = title.strip() + "\n\n" + content

                    def write():
                        with target.open("w", encoding="utf-8") as stream:
                            stream.write(text)
                        if target.read_text(encoding="utf-8") != text:
                            raise OSError("Work verification failed")

                    await asyncio.to_thread(write)
                    receipt = {
                        "saved": True,
                        "title": title[:200],
                        "path": str(target.resolve()),
                        "characters": len(content),
                        "status": "drafting",
                        "work_id": work_id,
                    }
                    catalogue_receipt(state, "autonomous_save_work", json.dumps(receipt))
                    record_evidence(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
                    state["saved_works"] = (state.get("saved_works", []) + [receipt])[-30:]
                    owner.journal(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
                    await owner.save()
                    return json.dumps(receipt, ensure_ascii=False)
                else:
                    raise ValueError(f"未找到 id={work_id} 的现有草稿")
            # Create new draft
            path = resolve_draft_path()
            text = title.strip() + "\n\n" + content

            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as stream:
                    stream.write(text)
                if path.read_text(encoding="utf-8") != text:
                    raise OSError("Work verification failed")

            await asyncio.to_thread(write)
            receipt = {
                "saved": True,
                "title": title[:200],
                "path": str(path.resolve()),
                "characters": len(content),
                "status": "drafting",
                "work_id": None,
            }
            # Register with explicit status
            from .library import register_local_work

            register_local_work(state, str(path.resolve()), "autonomous_save_work", title, status="drafting")
            record_evidence(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
            state["saved_works"] = (state.get("saved_works", []) + [receipt])[-30:]
            owner.journal(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
            await owner.save()
            return json.dumps(receipt, ensure_ascii=False)

        # mode == "final"
        state = self.event.state
        receipt = {
            "saved": True,
            "title": title[:200],
            "path": "",
            "characters": len(content),
            "status": "finished",
            "work_id": None,
        }
        
        async def sync_companion_if_needed(title, content, scope):
            """Async companion sync helper."""
            receipt["companion"] = {"saved": False, "reason": "standalone"}
            if owner.config.get("companion_integration", True):
                try:
                    for meta in owner.context.get_all_stars():
                        if not meta.activated or (
                            meta.root_dir_name != "astrbot_plugin_private_companion"
                            and meta.name != "PrivateCompanion"
                        ):
                            continue
                        if (
                            self.event.plugins_name is not None
                            and meta.name not in self.event.plugins_name
                        ):
                            continue
                        api = getattr(meta.star_cls, "extension_api", None)
                        writer = getattr(api, "import_completed_text_work", None)
                        if not callable(writer):
                            receipt["companion"] = {
                                "saved": False,
                                "reason": "companion_import_api_unavailable_reload_host",
                            }
                            break
                        result = await asyncio.wait_for(
                            writer(
                                self.event,
                                title=title,
                                content=content,
                                source_id=hashlib.sha256(
                                    (scope + title + content).encode()
                                ).hexdigest(),
                            ),
                            30,
                        )
                        receipt["companion"] = (
                            result
                            if isinstance(result, dict)
                            else {"saved": False, "reason": "invalid_receipt"}
                        )
                        break
                except Exception as exc:
                    receipt["companion"] = {
                        "saved": False,
                        "reason": type(exc).__name__ + ": " + str(exc)[:200],
                    }
                    owner.logger.warning(
                        "Local work saved but Companion import failed: %s", exc
                    )
        
        if work_id:
            # Finalize an existing draft
            work_item = next(
                (w for w in state.get("work_library", []) if w["id"] == work_id),
                None,
            )
            if not work_item:
                raise ValueError(f"未找到 id={work_id} 的现有草稿")
            if work_item.get("producer") == "private_companion":
                raise ValueError("陪伴作品库条目只读，不可直接修改；请调用 companion API")
            if not work_item.get("path"):
                raise ValueError("该草稿无本地文件，无法定稿")
            target = Path(work_item["path"])
            # Overwrite with final content
            text = title.strip() + "\n\n" + content

            def write():
                with target.open("w", encoding="utf-8") as stream:
                    stream.write(text)
                if target.read_text(encoding="utf-8") != text:
                    raise OSError("Work verification failed")

            await asyncio.to_thread(write)
            # Re-register with updated metadata
            from .library import register_local_work

            register_local_work(state, str(target.resolve()), "autonomous_save_work", title, status="finished")
            receipt["path"] = str(target.resolve())
            # Mark as finished
            state["work_library"] = [
                w if w["id"] != work_item["id"] else {**w, "status": "finished"}
                for w in state["work_library"]
            ]
            await sync_companion_if_needed(title, content, scope)
        else:
            # New final work (timestamped like before)
            path = (
                root
                / "works"
                / scope
                / (
                    datetime.now().strftime("%Y%m%d-%H%M%S-")
                    + uuid.uuid4().hex[:8]
                    + ".txt"
                )
            )
            text = title.strip() + "\n\n" + content

            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(text)
                if path.read_text(encoding="utf-8") != text:
                    raise OSError("Work verification failed")

            await asyncio.to_thread(write)
            receipt["path"] = str(path.resolve())
            # Register with explicit status
            from .library import register_local_work

            register_local_work(state, str(path.resolve()), "autonomous_save_work", title, status="finished")
            await sync_companion_if_needed(title, content, scope)

        record_evidence(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
        state["saved_works"] = (state.get("saved_works", []) + [receipt])[-30:]
        owner.journal(state, "work_saved", json.dumps(receipt, ensure_ascii=False))
        await owner.save()
        return json.dumps(receipt, ensure_ascii=False)
