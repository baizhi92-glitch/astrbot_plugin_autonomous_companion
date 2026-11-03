"""Fresh ComfyUI observations for each relevant autonomous round."""

import asyncio
import json
import time
from urllib.parse import urlsplit

import aiohttp


async def probe_comfyui(base_url: str) -> dict:
    """Inspect ComfyUI's read-only API rather than assuming an open port is ready.

    Args:
        base_url: User-configured ComfyUI server root.

    Returns:
        Timestamped observation; failures mean unknown, not proof of shutdown.
    """
    result = {"checked_at": time.time(), "url": base_url, "status": "unknown"}
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        return {
            **result,
            "detail": "ComfyUI 地址无效，请填写不含账号、查询或片段的 HTTP 服务根地址",
        }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(
                base_url.rstrip("/") + "/system_stats", allow_redirects=False
            ) as response:
                if response.status != 200:
                    return {
                        **result,
                        "detail": f"system_stats 返回 HTTP {response.status}，无法确认就绪",
                    }
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    return {**result, "detail": "服务响应过大，无法确认就绪"}
                payload = json.loads(raw)
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("system"), dict)
                    or not isinstance(payload.get("devices"), list)
                ):
                    return {
                        **result,
                        "detail": "响应不是预期 ComfyUI 状态，无法确认就绪",
                    }
                return {
                    **result,
                    "status": "ready",
                    "detail": "ComfyUI system_stats 本轮实际响应正常；不保证工作流、模型文件或绘图一定成功",
                }
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        return {
            **result,
            "detail": f"本轮探测未成功：{type(exc).__name__}；不能据此断言用户没有启动服务",
        }


async def refresh_services(owner, state, history) -> str:
    """Refresh relevant service observations; never reuse old availability as fresh.

    Args:
        owner: Autonomous plugin instance.
        state: Current target state.
        history: Recent textual conversation messages.

    Returns:
        Model-facing fresh observations and instructions about changed conditions.
    """
    if not owner.config.get("check_comfyui_each_round", True):
        return "本轮服务探测已关闭，不可将历史状态当作当前事实。"
    context = json.dumps(
        {
            key: state.get(key)
            for key in (
                "manual_plan",
                "future_task",
                "next_task",
                "projects",
                "journal",
            )
        },
        ensure_ascii=False,
    )
    context += json.dumps(history[-6:], ensure_ascii=False)
    if "comfy" not in context.lower() and "补画" not in context:
        return "本轮未检测到 ComfyUI 相关任务，未探测服务。"
    observed = await probe_comfyui(
        str(owner.config.get("comfyui_url", "http://127.0.0.1:8188")).strip()
    )
    old = state.get("comfyui_observation", {})
    observed["changed"] = old.get("status") != observed["status"] if old else False
    state["comfyui_observation"] = observed
    owner.journal(state, "service_check", json.dumps(observed, ensure_ascii=False))
    await owner.save()
    return (
        json.dumps(observed, ensure_ascii=False)
        + "\n这是本轮实际探测，高于历史摘要中的服务状态。"
        "ready 时不要再催用户开启 ComfyUI，应转而尝试原绘图任务的可用工具；"
        "unknown 时只能说明无法确认，不许断言‘仍没开’。探测成功不等于绘图已完成。"
    )
