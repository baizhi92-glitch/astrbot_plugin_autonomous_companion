"""Load the same skill catalogue used by normal AstrBot conversations."""

from astrbot.core.skills.skill_manager import SkillManager, build_skills_prompt


async def load_skill_context(owner, event, persona):
    """Resolve active persona/plugin/workspace skills without selecting a new workflow.

    Args:
        owner: Autonomous runtime.
        event: Current scoped conversation event.
        persona: Selected persona with optional skill allowlist.

    Returns:
        Native skill catalogue prompt and names exposed to the current round.
    """
    from astrbot.core.astr_main_agent import (
        _filter_skills_for_current_config,
        _get_workspace_path_for_umo,
    )

    config = owner.context.get_config(event.unified_msg_origin)
    settings = dict(config.get("provider_settings", {}))
    settings["plugin_set"] = config.get("plugin_set", ["*"])
    runtime = settings.get("computer_use_runtime", "none")
    manager = SkillManager()
    skills = _filter_skills_for_current_config(
        manager.list_skills(active_only=True, runtime=runtime), settings
    )
    allowed = persona.get("skills")
    if allowed is not None:
        skills = [skill for skill in skills if skill.name in allowed]
    if runtime == "local" and allowed != []:
        root = await _get_workspace_path_for_umo(
            event.unified_msg_origin, owner.context
        )
        merged = {skill.name: skill for skill in skills}
        for skill in manager.list_workspace_skills(root):
            merged[skill.name] = skill
        skills = [merged[name] for name in sorted(merged)]
    if not skills:
        return "", []
    limitation = (
        "\n当前 AstrBot 未启用 Computer Use，不可仅凭技能路径声称具备 shell/Python 执行权限。"
        if runtime == "none"
        else ""
    )
    return (
        build_skills_prompt(skills)
        + limitation
        + "\n主动回合也必须先按任务匹配已有技能，读取适用技能的说明后沿用已学步骤。不要因为是主动触发就重新探索或改换为其他工作流。技能必须与实际任务及可用工具匹配。",
        [skill.name for skill in skills],
    )
