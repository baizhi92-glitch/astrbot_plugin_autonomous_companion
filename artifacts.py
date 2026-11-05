"""Task-scoped generated image references and explicit delivery."""

import hashlib
import json
import time

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain


def register_artifacts(event, tool, text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return []
    if (
        not isinstance(value, dict)
        or value.get("status") != "completed"
        or not isinstance(value.get("outputs"), dict)
    ):
        return []
    retained = event.state.setdefault("task_artifacts", [])
    task = getattr(event, "checkpoint_task", "")
    added = []
    for output in value["outputs"].values():
        if not isinstance(output, dict) or output.get("type") != "image":
            continue
        url = output.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        task_id = getattr(event, "checkpoint_id", "")
        identifier = hashlib.sha256(
            (event.cid + "\0" + task_id + "\0" + task + "\0" + url).encode()
        ).hexdigest()[:16]
        item = next((item for item in retained if item["id"] == identifier), None)
        if item is None:
            item = {
                "id": identifier,
                "task": task,
                "task_id": task_id,
                "cid": event.cid,
                "url": url,
                "producer": tool,
                "status": "pending",
                "registered_at": time.time(),
            }
            retained.append(item)
        added.append(dict(item))
    event.state["task_artifacts"] = retained[-20:]
    return added


class SendArtifactTool(FunctionTool):
    def __init__(self, event):
        super().__init__(
            name="autonomous_send_artifact",
            description="发送当前任务生成回执登记的图片。传 artifact_id，不猜路径，不用其他任务的历史图片；发送一次后不要重复投递。已投递媒体后又重新生成的产物会被拦截，确需多张请一次性生成后再依次投递。",
            parameters={
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
            },
        )
        self.event = event

    async def call(self, context, artifact_id):
        event = self.event
        await event.owner.check_current(event)
        item = next(
            (
                item
                for item in event.state.get("task_artifacts", [])
                if item["id"] == artifact_id
                and item.get("task_id", "") == getattr(event, "checkpoint_id", "")
                and item["task"] == getattr(event, "checkpoint_task", "")
                and item["cid"] == event.cid
            ),
            None,
        )
        if item is None or item["status"] != "pending":
            return json.dumps(
                {"success": False, "reason": "产物不属于当前任务或已处理"},
                ensure_ascii=False,
            )
        first_delivery = event.state.get("first_media_delivery_at", 0)
        registered_at = item.get("registered_at", 0)
        if first_delivery and registered_at and registered_at > first_delivery:
            # The task already delivered media; this artifact was regenerated
            # afterwards (typically an interrupted extra call). Sending it would
            # append an unexpected, possibly unrelated image to the conversation.
            return json.dumps(
                {
                    "success": False,
                    "reason": "本任务已成功投递过媒体；该产物是投递之后重新生成的，已拦截重复投递，避免追加与用户预期不符的图片。确需多张时，请一次性生成后再依次投递。",
                },
                ensure_ascii=False,
            )
        item["status"] = "sending"
        await event.owner.save()
        try:
            await event.send(MessageChain(chain=[Image.fromURL(item["url"])]))
        except BaseException:
            item["status"] = "uncertain"
            await event.owner.save()
            raise
        item["status"] = "sent"
        await event.owner.save()
        return json.dumps({"success": True, "sent": True, "artifact_id": artifact_id})
