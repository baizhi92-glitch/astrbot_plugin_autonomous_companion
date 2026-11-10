"""Durable, bounded background jobs with isolated model context."""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

from astrbot.api.star import StarTools


def create_task(state, title, goal):
    if (
        not all(isinstance(v, str) and v.strip() for v in (title, goal))
        or len(title) > 200
        or len(goal) > 6000
    ):
        raise ValueError("任务标题1～200字，目标1～6000字")
    jobs = state.setdefault("long_tasks", [])
    if len(jobs) >= 30:
        raise ValueError("最多保留30个长期任务，请先删除已结束任务")
    if any(
        j["goal"] == goal.strip() and j["status"] not in {"completed", "cancelled"}
        for j in jobs
    ):
        raise ValueError("相同目标已有任务")
    job = {
        "id": uuid.uuid4().hex,
        "title": title.strip(),
        "goal": goal.strip(),
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "next_due": time.time(),
        "attempts": 0,
        "failures": 0,
        "steps": 0,
        "log": [],
        "receipts": [],
        "result": "",
        "error": "",
    }
    jobs.append(job)
    return job


def control_task(state, identifier, operation):
    job = next((j for j in state.get("long_tasks", []) if j["id"] == identifier), None)
    if not job:
        raise ValueError("长期任务不存在")
    if operation == "delete":
        if job["status"] not in {"completed", "cancelled"}:
            raise ValueError("请先取消任务再删除")
        state["long_tasks"].remove(job)
    elif operation in {"pause", "cancel"}:
        if job["status"] in {"completed", "cancelled"}:
            raise ValueError("任务已经结束")
        job["status"] = "paused" if operation == "pause" else "cancelled"
    elif operation == "resume":
        if job["status"] not in {"paused", "waiting", "failed"}:
            raise ValueError("仅暂停、等待或失败任务可以恢复")
        job.update(status="queued", next_due=time.time(), error="", failures=0, steps=0)
    else:
        raise ValueError("无效操作")
    job["updated_at"] = time.time()
    return job


def recover_tasks(state):
    for job in state.get("long_tasks", []):
        if job["status"] == "running":
            job.update(
                status="waiting",
                error="进程中断，原操作可能已提交；请核对结果后恢复，不自动重放",
            )


def due_task(state):
    ready = [
        j
        for j in state.get("long_tasks", [])
        if j["status"] == "queued" and j.get("next_due", 0) <= time.time()
    ]
    return min(ready, key=lambda j: j.get("updated_at", 0)) if ready else None


async def run_task(owner, umo, job):
    state = owner.states[umo]
    job.update(status="running", updated_at=time.time(), attempts=job["attempts"] + 1)
    await owner.save()
    event = None
    try:
        event, persona, provider, _, _ = await owner.prepare(umo)
        event.quiet = True
        event.action_receipts = list(job.get("receipts", []))
        # Tool wrappers persist owner state after calls; keep their receipt list
        # attached to the durable job before any side effect can occur.
        job["receipts"] = event.action_receipts
        event.checkpoint_id = job["id"]
        event.checkpoint_task = ""
        event.checkpoint_pending = False
        event.project_id = ""
        state["window_end"] = 0
        from .daily import usage_for

        usage_for(state)["rounds"] += 1
        scope = hashlib.sha256(umo.encode()).hexdigest()[:16]
        workspace = (
            Path(StarTools.get_data_dir("astrbot_plugin_autonomous_companion"))
            / "tasks"
            / scope
            / job["id"]
        )
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        job["workspace"] = str(workspace.resolve())
        # Exclude communication and task-control tools. BudgetTool also enforces quiet.
        tools = owner.tools(event, persona)
        tools.tools = [
            t
            for t in tools.tools
            if not any(
                word in t.name.lower()
                for word in ("send", "poke", "proactive", "task_control")
            )
        ]
        # 下游插件（技能学习、记忆、好感）靠这些钩子采集本轮工具证据；不传
        # 就退回空通知器，长期任务再丰富的工具工作也一个都学不到。开关与
        # 主动轮共用 expose_plugin_hooks。任务文本用长期任务自己的目标，别
        # 让学习侧只看到占位的“自主心跳”。
        event.message_str = job["goal"][:200]
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            message_obj.message_str = event.message_str
        prompt = {
            "task_id": job["id"],
            "goal": job["goal"],
            "workspace": job["workspace"],
            "previous_steps": job["log"][-8:],
            "actual_receipts": job["receipts"][-20:],
        }
        response = await owner.context.tool_loop_agent(
            event=event,
            chat_provider_id=provider,
            tools=tools,
            contexts=[],
            system_prompt="你是后台任务执行器，不是聊天角色。只执行给定目标；在指定工作区保存中间文件。"
            "不主动发送消息、不扩展目标。已有副作用回执不得重放，先查原结果。每轮最多推进一个有价值的阶段。"
            '最终输出JSON：{"status":"continue|waiting|completed","summary":"实际进展","result":"成果或完整结论","reason":"等待原因"}。'
            "没有工具能力、需要用户补充或外部结果不确定时waiting，不无限重试。不把计划当成果。",
            prompt=json.dumps(prompt, ensure_ascii=False),
            max_steps=owner.number("max_agent_steps", 12, 1, 30),
            tool_call_timeout=owner.number("tool_timeout_seconds", 120, 5, 600),
            agent_hooks=owner.notification_hooks,
        )
        await owner.check_current(event)
        from .main import parse_object

        result = parse_object(response.completion_text or "")
        if result.get("status") not in {"continue", "waiting", "completed"} or any(
            not isinstance(result.get(k, ""), str)
            for k in ("summary", "result", "reason")
        ):
            raise ValueError("后台任务返回格式错误")
        job["steps"] += 1
        job["log"] = (
            job["log"]
            + [{"at": time.time(), "summary": result.get("summary", "")[:4000]}]
        )[-20:]
        job["result"] = result.get("result", "")[:12000]
        job["error"] = result.get("reason", "")[:1000]
        job["status"] = {
            "continue": "queued",
            "waiting": "waiting",
            "completed": "completed",
        }[result["status"]]
        if job["status"] == "completed" and (
            not job["result"].strip()
            or any(
                r.get("status") in {"failed", "pending", "attempted", "unverified"}
                for r in event.action_receipts
            )
        ):
            job.update(
                status="waiting",
                error="完成报告缺少结果，或工具存在未确认结果，请核对后恢复",
            )
        if job["steps"] >= 12 and job["status"] == "queued":
            job.update(
                status="waiting", error="已执行12个阶段，等待人工核对目标和继续必要性"
            )
        job["next_due"] = time.time() + 90
    except asyncio.CancelledError:
        if job["status"] == "running":
            job.update(
                status="waiting", error="用户消息或停止操作打断，待核对已有结果后恢复"
            )
        raise
    except Exception as exc:
        job["failures"] += 1
        job.update(status="failed", error=str(exc)[:1000])
        owner.logger.exception("Long task failed: %s", job["id"])
    finally:
        if event is not None:
            job["receipts"] = event.action_receipts[-40:]
        job["updated_at"] = time.time()
        await owner.save()
