"""Bounded persistent project and scheduling updates shared by chat and actions."""

import copy
import math
import re
import time
import uuid
from typing import Any

from .action_context import completed_action_match
from .cancellations import cancelled_match, related
from .conditions import validate_conditions
from .lifecycle import appointment_updates
from .notes import NOTE_KINDS

PROJECT_SLOT_STATUSES = frozenset({"active", "paused"})
_NOTE_CLAUSE = re.compile(r"[^，。！？；,!?;\n]+[！？!?]?")
_NOTE_TRANSIENT = re.compile(
    r"今天|今晚|今早|明天|昨天|刚才|目前|暂时|这次|这一回|这周|最近|此刻|现在|开玩笑|演戏|角色扮演|假设|比方说|举个例子|不是说我|这句|这句话|台词|转述"
)
_NOTE_PREFERENCE = re.compile(
    r"^(?:我(?:希望|更喜欢|不喜欢)你|以后|今后|从今往后|往后|平时|每次|一直|"
    r"请(?:叫我|称呼我|不要叫我|别叫我)|(?:别|不要)再叫我|from now on\b|always\b|please call me\b)",
    re.IGNORECASE,
)
_NOTE_FACT = re.compile(
    r"^(?:我(?:的)?(?:(?:一位|一个|那位)?朋友|家人|伴侣|对象|室友|家里人|父母|兄弟姐妹|同事|家)?"
    r"(?:养了|养着|有|收养了|领养了)\s*(?:[一两二三四五六七八九十\d]+)?(?:只|条|匹|对)\S{0,70}(?:猫|狗|兔|鹦鹉|仓鼠|宠物|乌龟|金鱼|蜥蜴|刺猬)(?:[！!]|$)|"
    r"我(?:叫(?!他|她|你|他们|它|别人|大家|朋友|同事)|的名字是)\s*[\w·-]{1,32}[！!]?$)"
    r"|^(?:I (?:have|adopted)|my (?:friend|partner|family|roommate) (?:has|adopted)) (?:a|an|two) (?:pet|cat|dog|rabbit|bird)\b",
    re.IGNORECASE,
)
_NOTE_STORY = re.compile(
    r"开玩笑|演戏|角色扮演|人设|假设|比方说|举个例子|台词|转述|虚构|小说里|故事里|如果|要是|说过|听说"
)
_PROJECT_OPERATIONS = {
    "pause": "paused",
    "resume": "active",
    "cancel": "cancelled",
    "complete": "completed",
    "delete": "deleted",
}


def resolve_project(state, task, identifier=""):
    """Bind a known ID or a uniquely named project.

    Args:
        state: Per-conversation planning state.
        task: Task text to match against project titles.
        identifier: Exact project ID, if specified.

    Returns:
        The matching project, or None if missing or ambiguous.
    """
    projects = state.get("projects", [])
    if identifier:
        return next((p for p in projects if p["id"] == identifier), None)
    text = task.strip().casefold()
    matches = [
        p
        for p in projects
        if p["title"].casefold() == text
        or (len(p["title"]) >= 2 and p["title"].casefold() in text)
    ]
    if len(matches) > 1 and len({p["title"].casefold() for p in matches}) == 1:
        # An explicit restart can legitimately share an older title.
        working = [p for p in matches if p.get("status") in PROJECT_SLOT_STATUSES]
        if len(working) == 1:
            return working[0]
    return matches[0] if len(matches) == 1 else None


def _matches_project(task, project):
    """Check a queue entry against a project's title or goal.

    Args:
        task: Queued work description.
        project: A live record or deletion marker.

    Returns:
        Whether the text matches, including short exact names.
    """
    text = task.strip().casefold()
    return bool(text) and any(
        value and (text == value.casefold() or related(text, value))
        for value in (project.get("title", ""), project.get("goal", ""))
    )


def blocked_project_task(state, task, identifier=""):
    """Recognize work targeting a paused, terminal, or deleted project.

    Args:
        state: Per-conversation planning state.
        task: Candidate work description.
        identifier: Exact project ID, if already bound.

    Returns:
        Whether the work must not proceed automatically.
    """
    project = resolve_project(state, task, identifier)
    deleted = state.get("deleted_projects", [])
    if identifier:
        return (
            project.get("status") != "active"
            if project is not None
            else any(row["id"] == identifier for row in deleted)
        )
    if project is not None:
        return project.get("status") != "active"
    return any(
        row.get("status") != "active" and _matches_project(task, row)
        for row in (*state.get("projects", []), *deleted)
    )


def _stop_project_work(state, project, status):
    """Detach queued plans and deactivate or delete only this project's checkpoints.

    Args:
        state: Per-conversation planning state.
        project: Project whose pending work must stop.
        status: Its destination status (including deleted).
    """
    identifier = project["id"]
    legacy = state.get("execution_checkpoint")
    checkpoints = state.get("execution_checkpoints", {})
    linked_tasks = set()
    for key, checkpoint in [(None, legacy), *checkpoints.items()]:
        if not checkpoint:
            continue
        bound = checkpoint.get("project_id")
        if bound != identifier and (
            bound or not _matches_project(checkpoint.get("task", ""), project)
        ):
            continue
        if checkpoint.get("task"):
            linked_tasks.add(checkpoint["task"])
        if status == "deleted":
            if key is None:
                state.pop("execution_checkpoint", None)
            else:
                checkpoints.pop(key)
        else:
            # Paused work can still be resumed explicitly with its receipts.
            checkpoint["project_id"] = identifier
            if status != "paused":
                checkpoint["pending"] = False
    manual = state.get("manual_plan", "")
    manual_id = state.get("manual_project_id")
    if manual_id == identifier or (
        not manual_id and (manual in linked_tasks or _matches_project(manual, project))
    ):
        state.update(manual_plan="", manual_project_id="", manual_plan_expires_at=0)
        if manual:
            linked_tasks.add(manual)
    for key in ("future_task", "next_task"):
        task = state.get(key, "")
        if task in linked_tasks or (
            not (manual_id and manual_id != identifier and task == manual)
            and _matches_project(task, project)
        ):
            state[key] = ""
            if key == "next_task":
                state.update(window_end=0, window_admitted=False)


def control_project(state, identifier, operation, *, max_projects=5):
    """Apply an explicit user lifecycle action to a persisted project.

    Args:
        state: Per-conversation planning state to update.
        identifier: Exact ID of the user-selected project.
        operation: One of pause, resume, cancel, complete, or delete.
        max_projects: Maximum number of active and paused projects.

    Returns:
        The updated project or, for deletion, the removed project.

    Raises:
        ValueError: The action or project ID is invalid, or resuming exceeds capacity.
    """
    if not isinstance(operation, str) or operation not in _PROJECT_OPERATIONS:
        raise ValueError("Invalid project operation")
    project = resolve_project(state, "", identifier) if identifier else None
    if project is None:
        raise ValueError("项目不存在或已删除，请刷新")
    status = _PROJECT_OPERATIONS[operation]
    if status != "deleted":
        apply_plan(
            state,
            {"project_updates": [{"id": identifier, "status": status}]},
            explicit_edit=True,
            max_projects=max_projects,
        )
        return resolve_project(state, "", identifier)

    _stop_project_work(state, project, status)
    state["deleted_projects"] = [
        *state.get("deleted_projects", []),
        {
            "id": identifier,
            "title": project["title"],
            "goal": project.get("goal", ""),
            "at": time.time(),
        },
    ]
    state["projects"] = [p for p in state["projects"] if p["id"] != identifier]
    return project


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
next_speak_minutes: 5~1440，你自己约定多少分钟后开口说话；到点会专门为这个约定唤醒你。填 0 取消全部未到期的约定。
next_speak_reason: 简短说明为什么选这个时间。同一件事再约一次会自动和旧那条合并，不会叠成几个闹钟，不必自己避让。
cancel_speak_reason: 想撤掉某个约定时，用一句话写下那件事（例如"五点半问他下工"），系统按意思匹配并取消它；这是你自己的约定，自己决定，不用征求用户同意。
只在确实想到时候说点什么时使用，不要每轮都约定，也不要用它代替 rhythm_minutes 的长期节奏。
有工具能实际推进的事，本轮就动手做，不要只写进 future_task 当设想；真做不动才留给下次，并把卡点写清楚。
调用工具前先想清这一步要得到什么；没有实际推进就不要为了填满一轮而调用。
speak_wish: 有想说但此刻没说出口的话时，用一句话记下它（不超过300字）；下次唤醒时你会看到这句话，再决定说不说、什么时候说。
speak_wish_clear: true 表示放下之前记下的那句话（已经说了、不必说了或过时了）；没有要放下的就省略。
念头和开口约定不同：speak_wish 是"我想说这件事"，next_speak_minutes 是"我打算几点说"。两者可以同时存在，也可以只有念头。
rhythm_minutes: 希望持续采用的基础主动间隔（分钟），不是仅本次等待时间；
rhythm_reason: 简短说明为何想更频繁或更安静。两个字段一起提供，有真实动机再调整。
按未完成事项、创作兴趣、对话进展和用户明确偏好调整；未回复不等于拒绝，别因此加速追问，遵循上下文给出的可用范围。
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
paused/cancelled/completed 项目不自动继续，不新建同名项目替代；只有用户在网页明确恢复才改回 active。
连续失败或长期未推进的项目先重新判断价值、修改方法或暂停，不重复同一失败动作。
创作形式不限于文章和图片。按灵感和可用工具选择，例如剧本、组诗、漫画分镜、世界设定集、桌游规则或关卡数据、源码原型、乐谱草图、或者图文和声音结合的作品；这些只是例子，不要求每轮创作或凑齐媒介。Markdown、JSON／CSV、纯文本源码可以直接保存；确有对应创作工具时，也可尝试音频或视频，以工具实际生成的文件回执为准。乐谱或分镜文字不是已生成的音视频。需要留存时在创作执行轮显式保存完整自产内容；artifact 只记录真实回执的位置或 work_id，不填猜测路径。普通聊天不作为创作产物自动入库。
没有合适的保存工具时，可将短草稿写在 progress，后续接着推进。
项目可以跨多次主动持续推进，不必每次从头开始。是否分享由你按作品和当下语境决定：可以只把完整正文保存到本地作品库而不发消息，也可以分享正文，或只自然提一句作品已存入作品库供对方自行查看；保存不等于必须发送。选择不分享时，要用作品保存工具确认文件真实存在，并在项目记录中留下作品路径；不要把未保存的正文只放在 progress、summary 或 artifact，也不要把全文塞进 message。
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
短篇、单幅画和一次性创作默认是有限作品，不是无限连载。你也可以因自己的兴趣主动选择长篇或长期创作，不需要等用户明确要求；先判断这个故事适合多长、准备写到哪里，并确定阶段与收尾点。长篇可以按卷或阶段持续推进，但不能因为还有灵感就无限追加；每个阶段都应服务于整体故事，最终按计划收束。
作品没有默认文风、题材、叙事人称、情绪基调或固定体裁。让每个项目依据自己的兴趣、人格和故事需要形成独特表达；人格体现在观察、选择和价值取舍中，不是把所有作品都写成相同口吻。
开始长篇、连载或跨多轮的长期创作前，先建立可保存的大纲：至少写明核心设定、主要阶段/章节、预期冲突或变化，以及计划中的结尾和收束条件；不要求每章细纲，但必须知道故事准备如何结束。把大纲作为作品资料保存，并在持续项目的 progress/artifact/next_step 中记录其真实位置与当前阶段。用户明确要即兴写作时可不预先锁定情节，但仍要随进展补出方向和收尾目标。
推进长篇时按阶段或章节写，章节统一用「作品名·第N章 章名」保存；更新同一章沿用该章的 work_id，新章节另存，不用当前章覆盖前面的章节或整部合稿。每次只更新真实完成的部分和下一步；段落、章节或中途草稿即使已保存，也只是局部完成，整部作品保持 active，不能标成 completed。结尾阶段应回收主要冲突与重要伏笔，交代核心人物/事件的结果，并落到清楚的终止点；不得只因暂时没灵感、达到单次回合上限或保存成功就宣称长篇结束。只有对照大纲的收尾条件确认主要线索已闭合、预定结局已写出，或用户明确决定停止/改结局，才将整个项目标记 completed/cancelled，并清空 next_step。若长期暂停，标 paused 并留下可恢复的下一步，不伪装成完结。
每次保存一段只表示片段保存，不代表整篇完成；但“可以再润色、等待感想、以后还可续写”不是必须保持未完成的理由。短篇达到原目标、有完整结尾即可 completed，next_step 置空。新的续篇另作候选，不自动续写已完成作品。
progress 最多6000字符，只存摘要或短草稿；长正文先保存为文件再记录 artifact，不要用一句摘要替换唯一正文。
同名进行中项目应更新原ID，不重复新建。进行中和暂停项目合计最多5项；已完成和已取消项目保留记录，但不占名额。参考 last_attempt_at 兼顾久未推进的项目，retry_after 到期前不重复尝试。
"""

CHAT_PLANNING_PROMPT = (
    """你是对话后的自主计划整理器，不向用户说话，不执行工具。
根据刚才的真实用户发言和助手回复，决定是否形成值得保留的未来计划或创作项目。
不要把每句话都变成任务；不要将助手的设想当成用户偏好或已完成事实。
只输出一个 JSON 对象，可包含 future_task、project_updates、next_check_minutes、next_speak_minutes、next_speak_reason、speak_wish、speak_wish_clear。
还可包含 note_updates: [{"key":"这条的主题","value":"具体内容",
"kind":"preference 或 fact 或 interest","evidence":"用户原话中的连续片段",
"confirmed":true 或 false,"drop":可选，true 表示删掉这个主题}]，每轮最多 3 条改动。
自动记事只用 preference（用户明确要求长期如何互动）与 fact（有助于陪伴的、用户明确陈述的事实）。用户是陪伴对象，不是助手。
例如：『以后叫我白』『平时聊天别用敬语』是长期互动偏好；『我朋友养了只叫团子的猫』是关于用户身边关系的事实，必须保留“朋友”，不能改为用户本人养猫，也不能推成用户喜欢猫。
『那个插件不再用了，也不打算继续修』是特定事项的决定，不是长期偏好或个人事实；只有明确关联现有事项时才走项目／取消逻辑。临时请求、偶遇、角色扮演、引用和问题不入库，日期约定走约定字段。
key 用稳定短标签，同类同主题才复用；evidence 必须完整逐字复制本轮原话中的一段独立陈述，保留主语、否定和时间限定，不能只截关键词。value 使用原话，不补人物、对象、细节或推断。
不要自动删除记录；不要替用户改写手动添加、固定或不同类别的条目。拿不准、只说了一次要做什么、没有值得记的内容时，省略 note_updates。
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
        max_projects: Maximum number of active and paused projects.
        allow_completion: Whether the current phase may report completion.
        explicit_edit: Whether a person explicitly requested the change.

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
    projects = copy.deepcopy(state.get("projects", [])) if updates else []
    stopped = {}
    cancellations = state.get("cancelled_tasks", [])
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("Invalid project")
        fields = {"id", "title", "goal", "progress", "next_step", "artifact", "status"}
        if any(
            key not in fields or not isinstance(value, str)
            for key, value in update.items()
        ):
            raise ValueError("Invalid project field")
        identifier = update.get("id", "")
        title = update.get("title", "").strip()[:1000].casefold()
        existing = next((p for p in projects if p["id"] == identifier), None)
        if not identifier and title:
            same_title = [p for p in projects if p["title"].casefold() == title]
            existing = next(
                (p for p in same_title if p.get("status") in PROJECT_SLOT_STATUSES),
                None if explicit_edit else next(iter(same_title), None),
            )
        if identifier and existing is None:
            if not explicit_edit and any(
                row["id"] == identifier for row in state.get("deleted_projects", [])
            ):
                continue
            raise ValueError("Unknown project ID")
        if not explicit_edit and (
            cancelled_match(
                state, update.get("title", "") + " " + update.get("goal", "")
            )
            or (
                existing is None
                and any(
                    title == row["title"].casefold()
                    for row in state.get("deleted_projects", [])
                )
            )
        ):
            continue
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
        if "status" in update:
            update["status"] = update["status"].strip()
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
        status = project["status"]
        if status not in {"active", "paused", "completed", "cancelled"}:
            raise ValueError("Invalid project status")
        if status == "completed":
            project["next_step"] = ""
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
        old_status = (existing or {}).get("status")
        if status != "active" and status != old_status:
            stopped[project["id"]] = (existing or project, status)
        elif status == "active":
            stopped.pop(project["id"], None)
            if explicit_edit and (not identifier or old_status != "active"):
                cancellations = [
                    row
                    for row in cancellations
                    if not _matches_project(row["text"], project)
                ]
        if existing is not None:
            projects[projects.index(existing)] = project
        else:
            projects.append(project)
    if updates:
        occupied_before = sum(
            p.get("status") in PROJECT_SLOT_STATUSES for p in state.get("projects", [])
        )
        occupied_after = sum(p.get("status") in PROJECT_SLOT_STATUSES for p in projects)
        if occupied_after > max(max_projects, occupied_before):
            raise ValueError(
                f"最多保留{max_projects}个进行中或暂停项目，请先结束或删除旧项目"
            )
        changes["projects"] = projects
    if cancellations != state.get("cancelled_tasks", []):
        changes["cancelled_tasks"] = cancellations
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
    clauses = (
        []
        if _NOTE_STORY.search(user_text)
        or re.search(
            r"(?:朋友|同事|对象|家人|别人|网友).{0,12}(?:说|问|写)|(?:他|她|他们|有人)说",
            user_text,
        )
        or any(mark in user_text for mark in '“”「」『』"`')
        else [clause.strip() for clause in _NOTE_CLAUSE.findall(user_text)]
    )
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
        old = retained_notes.get(key, {})
        kind = item.get("kind") if item.get("kind") in NOTE_KINDS else "fact"
        if not explicit_edit:
            if (
                item.get("kind") not in ("preference", "fact")
                or item.get("drop") is True
                or (
                    old
                    and (
                        key not in evidence
                        or old.get("kind") != kind
                        or old.get("pinned")
                        or old.get("source") == "manual"
                    )
                )
                or not 3 <= len(evidence) <= 240
                or evidence not in clauses
                or evidence.endswith(("?", "？"))
                or _NOTE_TRANSIENT.search(evidence)
                or re.search(
                    r"测试一下|帮我|能不能|能否|可以吗|身份证|密码|验证码|门牌号",
                    evidence,
                )
            ):
                continue
            if kind == "preference" and (
                not _NOTE_PREFERENCE.match(evidence)
                or not re.search(
                    r"叫我|称呼我|发消息|发语音|回复|跟我|和我|聊天|语气|敬语|称呼|开口|说话|感叹号|打扰|提起|话题|联系我|\b(?:call me|message me|reply|address me|contact me)\b",
                    evidence,
                    re.IGNORECASE,
                )
            ):
                continue
            if kind == "fact" and not _NOTE_FACT.match(evidence):
                continue
            # Persist the literal statement rather than a model-expanded claim.
            value = evidence
            if not old:
                # A model-generated title must not introduce a new person or fact.
                key = key if key in evidence else evidence[:80]
                if key in retained_notes:
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
            "confirmed": True
            if kind == "preference"
            else item.get("confirmed") is True,
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
    for project, status in stopped.values():
        _stop_project_work(state, project, status)
    if "future_task" in payload:
        proposed = payload["future_task"].strip()[:2000]
        if not proposed or (
            not cancelled_match(state, proposed)
            and not completed_action_match(state, proposed)
            and not blocked_project_task(state, proposed, payload.get("project_id", ""))
        ):
            state["future_task"] = proposed
    if state.get("manual_plan"):
        state["future_task"] = state["manual_plan"]
