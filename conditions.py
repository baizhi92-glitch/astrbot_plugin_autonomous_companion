"""Bounded, fresh dependency observations before autonomous actions."""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp


def validate_conditions(items):
    """Validate persisted dependency descriptions without executing anything.

    Args:
        items: Up to three dependency descriptions.

    Returns:
        JSON-safe dependency descriptions.

    Raises:
        ValueError: A dependency is malformed.
    """
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError("最多保存 3 个待办条件")
    clean = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in {
            "http",
            "tcp",
            "file",
            "tool",
        }:
            raise ValueError("条件类型必须为 http/tcp/file/tool")
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("条件必须有稳定 ID")
        if any(
            not isinstance(item.get(k, ""), str) or len(item.get(k, "")) > 1000
            for k in ("id", "label", "url", "host", "path", "tool")
        ):
            raise ValueError("条件字段格式错误")
        if item["kind"] == "tcp" and (
            type(item.get("port")) is not int or not 1 <= item["port"] <= 65535
        ):
            raise ValueError("端口错误")
        if item["kind"] == "tool" and not isinstance(item.get("args", {}), dict):
            raise ValueError("查询参数必须为对象")
        encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) > 4000:
            raise ValueError("条件描述过长")
        clean.append(json.loads(encoded))
    if len({item["id"] for item in clean}) != len(clean):
        raise ValueError("条件 ID 不能重复")
    return clean


async def observe_condition(owner, event, persona, condition):
    """Read one dependency, never inferring job completion from reachability.

    Args:
        owner: Plugin runtime.
        event: Revision-bound current private event.
        persona: Selected persona for tool filtering.
        condition: Validated dependency description.

    Returns:
        Observation with an explicit unknown state on unsupported queries.
    """
    result = {
        "id": condition["id"],
        "label": condition.get("label", condition["id"]),
        "kind": condition["kind"],
        "checked_at": time.time(),
        "status": "unknown",
    }
    try:
        await owner.check_current(event)
        kind = condition["kind"]
        if kind == "http":
            url = condition.get("url", "")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("HTTP 查询地址无效")
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4)
            ) as session:
                async with session.get(url, allow_redirects=False) as response:
                    raw = await response.content.read(8193)
                    result.update(
                        status="observed",
                        http_status=response.status,
                        detail=raw[:8192].decode("utf-8", errors="replace"),
                        truncated=len(raw) > 8192,
                    )
        elif kind == "tcp":
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(condition.get("host", ""), condition["port"]), 3
            )
            writer.close()
            await writer.wait_closed()
            result.update(
                status="observed", detail="端口可连接；不代表服务就绪或任务完成"
            )
        elif kind == "file":
            path = Path(condition.get("path", ""))
            roots = [
                Path(root).resolve()
                for root in owner.config.get("condition_file_roots", [])
                if isinstance(root, str) and root.strip()
            ]
            if not path.is_absolute():
                raise ValueError("文件条件必须使用绝对路径")
            path = path.resolve()
            if not any(path.is_relative_to(root) for root in roots):
                raise ValueError("文件不在网页配置的允许检查目录内")

            def stat():
                if not path.exists():
                    return {"exists": False}
                info = path.stat()
                return {
                    "exists": True,
                    "size": info.st_size,
                    "modified_at": info.st_mtime,
                    "is_file": path.is_file(),
                }

            result.update(status="observed", detail=await asyncio.to_thread(stat))
        elif kind == "tool":
            name = condition.get("tool", "")
            if name not in owner.config.get("condition_query_tools", []):
                raise ValueError("此工具未被用户批准为只读状态查询工具")
            tool = owner.tools(event, persona).get_tool(name)
            if tool is None:
                raise ValueError("查询工具未启用或不在当前人格工具范围")
            if getattr(getattr(tool, "wrapped", tool), "is_background_task", False):
                raise ValueError("后台执行工具不可作为同步状态检查")
            context = SimpleNamespace(
                context=SimpleNamespace(event=event, context=owner.context),
                tool_call_timeout=5,
            )
            texts = []
            was_quiet = getattr(event, "quiet", False)
            event.quiet = True
            try:
                async for output in tool.call(context, **condition.get("args", {})):
                    texts.extend(
                        getattr(part, "text", "")
                        for part in getattr(output, "content", [])
                    )
            finally:
                event.quiet = was_quiet
            if not texts:
                raise ValueError("查询工具没有提供可用状态")
            text = "\n".join(texts)[:8000]
            if any(
                marker in text
                for marker in (
                    "budget exhausted",
                    "暂缓到期后再试",
                    "Permission denied",
                )
            ):
                raise ValueError(text[:250])
            result.update(status="observed", detail=text)
    except Exception as exc:
        result["detail"] = f"无法确认：{type(exc).__name__}: {str(exc)[:250]}"
    return result


async def refresh_conditions(owner, event, persona):
    """Refresh every saved dependency and persist comparison with prior observations.

    Args:
        owner: Plugin runtime.
        event: Current event.
        persona: Current persona.

    Returns:
        Fresh model-facing observations, never stale fallback data.
    """
    state = event.state
    conditions = validate_conditions(state.get("conditions", []))
    observations = []
    old = {item["id"]: item for item in state.get("condition_observations", [])}
    for item in conditions:
        try:
            observation = await asyncio.wait_for(
                observe_condition(owner, event, persona, item), 6
            )
        except asyncio.TimeoutError:
            observation = {
                "id": item["id"],
                "kind": item["kind"],
                "checked_at": time.time(),
                "status": "unknown",
                "detail": "查询超时，不能沿用旧结论",
            }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    k: v
                    for k, v in observation.items()
                    if k not in {"checked_at", "changed", "fingerprint"}
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        observation.update(
            fingerprint=fingerprint,
            changed=bool(
                old.get(item["id"])
                and old[item["id"]].get("fingerprint") != fingerprint
            ),
        )
        observations.append(observation)
    state["condition_observations"] = observations
    if observations:
        owner.journal(
            state,
            "condition_check",
            json.dumps(observations, ensure_ascii=False)[:2000],
        )
        await owner.save()
    return json.dumps(observations, ensure_ascii=False)
