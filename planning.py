"""Bounded persistent project and scheduling updates shared by chat and actions."""

import copy
import math
import time
import uuid
from typing import Any

from .cancellations import cancelled_match
from .conditions import validate_conditions
from .lifecycle import appointment_updates
from .notes import NOTE_KINDS


def resolve_project(state, task, identifier=""):
    """Bind a known ID or a uniquely named project; never guess between matches."""
    projects = state.get("projects", [])
    if identifier:
        return next((p for p in projects if p["id"] == identifier), None)
    matches = [
        p
        for p in projects
        if len(p.get("title", "")) >= 2 and p["title"].casefold() in task.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def filter_project_updates(state, payload):
    """Drop foreign/stale model IDs without weakening explicit API validation."""
    clean = copy.deepcopy(payload)
    updates = clean.get("project_updates")
    if not isinstance(updates, list):
        return clean, []
    known = {p["id"] for p in state.get("projects", [])}
    skipped = []
    retained = []
    for update in updates:
        if isinstance(update, dict) and update.get("id") and update["id"] not in known:
            skipped.append(str(update["id"])[:200])
        else:
            retained.append(update)
    clean["project_updates"] = retained
    return clean, skipped


PLANNING_PROMPT = """
你还可以在 JSON 中使用下列可选字段（省略表示保持）：
next_check_minutes: 5~180，选择下次周期主动判断的间隔；有具体原因才改变。
next_speak_minutes: 5~1440，你自己约定多少分钟后开口说话；到点会专门为这个约定唤醒你，默认应开口而不是只做判断。
next_speak_reason: 简短说明为什么选这个时间。填 0 表示取消尚未到期的开口约定；省略表示保持原约定。
只在确实想到时候说点什么时使用，不要每轮都约定，也不要用它代替 rhythm_minutes 的长期节奏。
speak_wish: 有想说但此刻没说出口的话时，用一句话记下它（不超过300字）；下次唤醒时你会看到这句话，再决定说不说、什么时候说。
speak_wish_clear: true 表示放下之前记下的那句话（已经说了、不必说了或过时了）；没有要放下的就省略。
念头和开口约定不同：speak_wish 是"我想说这件事"，next_speak_minutes 是"我打算几点说"。两者可以同时存在，也可以只有念头。
rhythm_minutes: 希望持续采用的基础主动间隔（分钟），不是仅本次等待时间；
rhythm_reason: 简短说明为何想更频繁或更安静。两个字段一起提供，有真实动机再调整。
根据自己的未完成事项、创作兴趣、对话进展和用户明确偏好调整；
未回复不自动等于拒绝，也不要因此不断加速追问。遵循上下文给出的可用范围。
rhythm_scope: self（自己的临时节奏）/global（用户明确要求整体少联系或多联系）/topic（只是不想继续某话题，禁止改变全局节奏）。
取消画图、删除任务、不喜欢某内容不等于减少所有聊天，不得据此降低整体主动频率。
global 必须附 rhythm_evidence 为本轮用户明确频率要求的原话连续片段。
rhythm_duration_minutes: 10~1440，默认120；调频是临时状态，到期恢复默认后重新评估，不能一次判断永久冷落用户。
notify: false 表示本轮只后台工作，禁止调用发送消息的工具，message 必须为空。
project_id: 本轮推进的已有项目 ID；没有则省略，用于记录失败次数。
project_updates: 最多 3 项，每项为
{"id":"已有项目 ID，新项目留空","title":"项目名","goal":"目标",
 "progress":"实际完成部分或正文草稿","next_step":"下一步","artifact":"实际作品位置或引用",
 "status":"active|paused|completed|cancelled"}。
新建或更新项目时必须保留有用的已有进展；不能把设想记成已完成。
paused/cancelled/completed 项目不自动继续，只有用户明确要求恢复才改回 active。
连续失败或长期未推进的项目先重新判断价值、修改方法或暂停，不重复同一失败动作。
创作较长时通过实际可用的文件工具保存作品，artifact 只记录真实返回的位置；
没有文件工具时，可将短草稿写在 progress，后续接着推进。
项目可以跨多次主动持续推进，不必每次从头开始；有实际成果再决定是否分享。
deferred_outputs: 最多 3 项已完成但暂不分享的成果，格式为
[{"title":"名称","content":"可直接分享的完整文本","artifact":"真实作品位置，可空","reason":"暂不分享原因"}]。
只保存实际完成内容，不将计划放入成果箱；已发送的内容不要再次暂存。
待分享成果由用户在网页手动发送或丢弃，你不要在 message 中重新发送同一成果。
conditions: 可选，完整替换最多 3 个后续行动依赖条件；省略保留，[] 清空。
格式 [{"id":"稳定标识","label":"等待事项","kind":"http","url":"实际只读状态查询地址"}]；
还支持 kind=tcp 加 host/port；kind=file 加绝对 path；kind=tool 加 tool/args。
只记录真实存在的服务地址、文件引用和原任务 ID；tool 必须是用户批准的只读查询工具，
不准把提交生成、发送、启动、删除等动作当查询。没有查询能力保持未知，不重复执行有副作用动作。
每轮读取最新条件状态；变化后重新制定行动。旧结论不是本轮事实。
appointment_updates: 只从用户明确原话提取约定，最多3项：
[{"id":"已有约定ID，新建留空","title":"事件","due_at":绝对Unix秒时间戳,
"status":"pending|resolved|cancelled","result":"用户交代的结果","evidence":"用户原话连续片段"}]。
没有明确时间不新建；事情结果已交代则更新为 resolved，不再重复追问。
appointment_handled: 执行结果可列出本轮实际发送的约定提醒 [{"id":"ID","kind":"pre|post"}]。
只有确实提到该约定的发言才能列入，不凭普通问候消耗提醒。
项目操作必须填写 project_id，并在 project_updates 中使用相同的已有项目 ID。
project_updates 只能引用“持续项目”列表中的本插件ID；陪伴作品库的 external_ 等ID不是本插件项目ID，不得拿来更新。普通聊天无需填写项目更新；尚未发送的正文不得写成已发送。
决策和聊天整理阶段不能登记新完成的进展或宣布项目完成；执行结果阶段才可提交实际进展。
项目完成时明确清空 next_step，说明整个目标如何满足；存在阻碍或必要后续步骤时保持 active。
短篇、单幅画和一次性创作默认是有限作品，不是无限连载。先确定适量范围与收尾点，优先完成现有作品，不因还有灵感不断追加章节。只有用户明确要求连载或长期创作才保持开放式目标。
每次保存一段只表示片段保存，不代表整篇完成；但“可以再润色、等待感想、以后还可续写”不是必须保持未完成的理由。达到原目标、有完整结尾即可 completed，next_step 置空。新的续篇另作候选，不自动续写已完成作品。
progress 最多6000字符，只存摘要或短草稿；长正文先保存为文件再记录 artifact，不要用一句摘要替换唯一正文。
同名项目应更新原ID，不重复新建；达到5项时不新增。参考 last_attempt_at 兼顾久未推进的项目，retry_after 到期前不重复尝试。
"""

CHAT_PLANNING_PROMPT = (
    """你是对话后的自主计划整理器，不向用户说话，不执行工具。
根据刚才的真实用户发言和助手回复，决定是否形成值得保留的未来计划或创作项目。
不要把每句话都变成任务；不要将助手的设想当成用户偏好或已完成事实。
只输出一个 JSON 对象，可包含 future_task、project_updates、next_check_minutes、next_speak_minutes、next_speak_reason、speak_wish、speak_wish_clear。
还可包含 note_updates: [{"key":"这条的主题","value":"具体内容",
"kind":"preference 或 fact 或 interest","evidence":"用户原话中的连续片段",
"confirmed":true 或 false,"drop":可选，true 表示删掉这个主题}]，每轮最多 3 条改动。
三个 kind 的区别：preference 是"你该怎么对待用户"（作息、语气、称呼、不喜欢的话题），会约束你开口的时间、话题与语气；
fact 是关于用户的事实（身份、职业、居住、家人宠物、重要日程）；interest 是用户的爱好与口味。
拿不准算偏好还是事实时，用 fact，不要用 preference 乱加限制。
key 用稳定短标签，同一件事必须复用同一个 key，不要造近义 key。
evidence 必须逐字复制用户本轮原话的连续片段，不能改写、拼接或用自己的总结代替，否则整条会被丢弃。
preference 只能来自用户明确表达，不能由沉默、未回复或助手的话推断；它记的是以后要反复照做的约束，一次性的"这次想听个故事"属于临时计划。
confirmed 只在用户本人明确说出或明确确认过时为 true；从语气猜的、从上下文推的、助手说的，一律 false。preference 视为已确认。
只记稳定、以后还用得上的信息；不记情绪、临时状态、一次性经历、别人的隐私；身份证号、门牌号、密码、账号这类敏感信息一律不记。
不限条数。人是会变的：用户改口、搬家、换工作、分手、戒掉某个习惯时，用同一个 key 写成新说法覆盖旧的（旧说法会自动留一份做对比）；
发现某条已经彻底不对或当初就记错了，就在同一条里带 "drop": true 删掉它，不要留着自相矛盾的旧记录。
和已有记录对不上、又没有用户原话支撑时，不要改也不要删。没有值得改的就省略这个字段。
"""
    + PLANNING_PROMPT
)


def apply_plan(
    state: dict,
    payload: dict,
    *,
    user_text: str = "",
    max_projects: int = 5,
    allow_completion: bool = True,
    explicit_edit: bool = False,
) -> None:
    """Validate all updates before applying bounded persistent planning state.

    Args:
        state: Per-conversation state to update.
        payload: Model control object.
        user_text: Actual user text used to verify preference evidence.
        max_projects: Maximum number of retained projects.

    Raises:
        ValueError: A supplied field violates the planning contract.
    """
    payload = {key: value for key, value in payload.items() if value is not None}
    changes: dict[str, Any] = {}
    if "appointment_updates" in payload:
        changes["appointments"] = appointment_updates(
            state, payload["appointment_updates"], user_text
        )
    if "conditions" in payload:
        changes["conditions"] = validate_conditions(payload["conditions"])
    if "future_task" in payload:
        if not isinstance(payload["future_task"], str):
            raise ValueError("Invalid future task")
        proposed = payload["future_task"].strip()[:2000]
        if not cancelled_match(state, proposed):
            changes["future_task"] = proposed
    if "next_check_minutes" in payload:
        delay = payload["next_check_minutes"]
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
        ):
            raise ValueError("Invalid next check interval")
        changes["next_due"] = time.time() + max(5, min(180, delay)) * 60
    updates = payload.get("project_updates", [])
    if not isinstance(updates, list) or len(updates) > 3:
        raise ValueError("Invalid project updates")
    projects = copy.deepcopy(state.get("projects", []))
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("Invalid project")
        if cancelled_match(
            state, str(update.get("title", "")) + " " + str(update.get("goal", ""))
        ):
            continue
        if any(
            update.get("id") == deleted["id"]
            or (not update.get("id") and update.get("title") == deleted["title"])
            for deleted in state.get("deleted_projects", [])
        ):
            continue
        fields = {"id", "title", "goal", "progress", "next_step", "artifact", "status"}
        if any(
            key not in fields or not isinstance(value, str)
            for key, value in update.items()
        ):
            raise ValueError("Invalid project field")
        existing = next((p for p in projects if p["id"] == update.get("id")), None)
        if not update.get("id") and update.get("title"):
            existing = next(
                (
                    p
                    for p in projects
                    if p["title"].casefold() == update["title"].strip().casefold()
                ),
                None,
            )
        if update.get("id") and existing is None:
            raise ValueError("Unknown project ID")
        # Only an explicit dashboard action may resume a retired/paused project.
        # Old model context must not overwrite its status or progress.
        if (
            not explicit_edit
            and existing
            and existing.get("status") in {"paused", "completed", "cancelled"}
        ):
            continue
        if len(update.get("progress", "")) > 6000:
            raise ValueError(
                "项目进展超过6000字符，请先保存完整正文为作品文件，再记录摘要和引用"
            )
        update = dict(update)
        if update.get("status") == "completed" and not allow_completion:
            update.pop("status")
        if (
            not explicit_edit
            and update.get("status") == "completed"
            and update.get("next_step", (existing or {}).get("next_step", "")).strip()
        ):
            update["status"] = "active"
        project = dict(existing or {"id": uuid.uuid4().hex[:12], "status": "active"})
        for key, value in update.items():
            if key != "id":
                project[key] = value.strip()[: 6000 if key == "progress" else 1000]
        if project["status"] not in {"active", "paused", "completed", "cancelled"}:
            raise ValueError("Invalid project status")
        if not project.get("title"):
            raise ValueError("Project title is required")
        project["updated_at"] = time.time()
        if update.get("progress") and update.get("progress") != (existing or {}).get(
            "progress"
        ):
            project["last_progress_at"] = time.time()
            project["failure_count"] = 0
            project["last_error"] = ""
            project["retry_after"] = 0
        if existing and any(
            project.get(k, "") != existing.get(k, "")
            for k in ("title", "goal", "progress", "next_step", "artifact", "status")
        ):
            project["history"] = (
                existing.get("history", [])
                + [
                    {
                        "at": time.time(),
                        **{
                            k: existing.get(k, "")
                            for k in (
                                "title",
                                "goal",
                                "progress",
                                "next_step",
                                "artifact",
                                "status",
                            )
                        },
                    }
                ]
            )[-8:]
        if existing is not None:
            projects[projects.index(existing)] = project
        elif len(projects) < max_projects:
            projects.append(project)
        else:
            raise ValueError("最多保留5个持续项目，请先删除旧项目；未覆盖已有项目")
    if updates:
        changes["projects"] = projects
    updates = payload.get("note_updates", [])
    # 兼容旧字段名：偏好与印象曾经分开输出
    legacy = [
        (payload.get("preference_updates", []), "preference"),
        (payload.get("impression_updates", []), None),
    ]
    merged = list(updates) if isinstance(updates, list) else None
    if merged is None:
        raise ValueError("Invalid notes")
    for rows, forced_kind in legacy:
        if not rows:
            continue
        if not isinstance(rows, list):
            raise ValueError("Invalid notes")
        for row in rows:
            if isinstance(row, dict) and forced_kind:
                row = dict(row, kind="preference")
            merged.append(row)
    retained_notes = dict(state.get("notes", {}))
    seen_notes = {
        str(note.get("value", "")).strip()
        for note in retained_notes.values()
        if isinstance(note, dict)
    }
    touched = False
    for item in merged[:6]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not isinstance(item.get("evidence"), str)
        ):
            raise ValueError("Invalid note")
        # drop 只需要 key 和原话依据，其余改动必须有 value
        if item.get("drop") is not True and not isinstance(item.get("value"), str):
            raise ValueError("Invalid note")
        evidence = item["evidence"].strip()
        key = item["key"].strip()[:80]
        value = str(item.get("value") or "").strip()[:600]
        if not (key and evidence and evidence in user_text):
            # 没有逐字原话依据，或原话不在本轮发言里：整条丢弃
            continue
        if item.get("drop") is True:
            # 人变了或当初记错了：她可以主动删掉这一条
            retained_notes.pop(key, None)
            seen_notes = {
                str(note.get("value", "")).strip()
                for note in retained_notes.values()
                if isinstance(note, dict)
            }
            touched = True
            continue
        if not value:
            continue
        if key not in retained_notes and value in seen_notes:
            # 同一个值换个 key 再记一次不带来新信息
            continue
        old = retained_notes.get(key, {})
        kind = item.get("kind") if item.get("kind") in NOTE_KINDS else "fact"
        entry = {
            "value": value,
            "kind": kind,
            # 偏好来自用户明确表达，天然是已确认的；其余按模型给出的确认状态
            "confirmed": True if kind == "preference" else item.get("confirmed") is True,
            "evidence": evidence[:300],
            "created_at": old.get("created_at", time.time()),
            "updated_at": time.time(),
            "pinned": bool(old.get("pinned", False)),
            "source": "chat",
        }
        if entry["confirmed"]:
            entry["confirmed_at"] = time.time()
        previous = str(old.get("value", "")).strip()
        if previous and previous != value:
            entry["previous"] = previous[:600]
            entry["changed_at"] = time.time()
        retained_notes[key] = entry
        seen_notes.add(value)
        touched = True
    if touched:
        # 不设条数上限：人会变，新的说法覆盖旧的，旧的留一份做对比
        changes["notes"] = retained_notes
    state.update(changes)
    if state.get("manual_plan"):
        state["future_task"] = state["manual_plan"]
